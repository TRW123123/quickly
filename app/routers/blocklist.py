"""Blocklist API: global do-not-contact list (exact emails and whole domains)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.blocklist import KINDS, detect_kind, normalize
from app.database import get_db
from app.models import BlocklistEntry

router = APIRouter(prefix="/api/blocklist", tags=["blocklist"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


class BlocklistAdd(BaseModel):
    value: str
    kind: Optional[str] = None  # email | domain; detected from value when omitted
    reason: Optional[str] = None


class BlocklistEntryResponse(BaseModel):
    id: int
    value: str
    kind: str
    reason: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


def validate_value(value: str, kind: str | None) -> tuple[str, str] | None:
    """Return ``(normalized_value, kind)`` or ``None`` when the value is unusable."""
    v = normalize(value)
    if not v:
        return None
    k = (kind or "").strip().lower() or detect_kind(v)
    if k not in KINDS:
        return None
    if k == "email" and not _EMAIL_RE.match(v):
        return None
    if k == "domain" and not _DOMAIN_RE.match(v):
        return None
    return v, k


async def add_entries(db: AsyncSession, items: list[BlocklistAdd]) -> dict:
    """Insert entries, skipping existing values. Shared by REST and MCP-facing code."""
    added: list[BlocklistEntry] = []
    existing: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    parsed: list[tuple[str, str, str | None]] = []
    for it in items:
        ok = validate_value(it.value, it.kind)
        if ok is None:
            invalid.append(it.value)
            continue
        v, k = ok
        if v in seen:
            existing.append(v)
            continue
        seen.add(v)
        parsed.append((v, k, (it.reason or None)))
    if parsed:
        res = await db.execute(
            select(BlocklistEntry.value).where(BlocklistEntry.value.in_([p[0] for p in parsed]))
        )
        already = {r[0] for r in res.all()}
        for v, k, reason in parsed:
            if v in already:
                existing.append(v)
                continue
            row = BlocklistEntry(value=v, kind=k, reason=reason)
            db.add(row)
            added.append(row)
        await db.flush()
    return {
        "added": [BlocklistEntryResponse.model_validate(r).model_dump(mode="json") for r in added],
        "existing": existing,
        "invalid": invalid,
    }


@router.get("")
async def list_blocklist(
    q: str = "",
    kind: str = "",
    limit: int = Query(500, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(BlocklistEntry)
    count_stmt = select(func.count(BlocklistEntry.id))
    if q.strip():
        pat = f"%{normalize(q)}%"
        stmt = stmt.where(BlocklistEntry.value.like(pat))
        count_stmt = count_stmt.where(BlocklistEntry.value.like(pat))
    if kind.strip():
        stmt = stmt.where(BlocklistEntry.kind == kind.strip().lower())
        count_stmt = count_stmt.where(BlocklistEntry.kind == kind.strip().lower())
    total = (await db.execute(count_stmt)).scalar() or 0
    rows = (
        await db.execute(stmt.order_by(BlocklistEntry.id.desc()).offset(offset).limit(limit))
    ).scalars().all()
    return {
        "total": total,
        "items": [BlocklistEntryResponse.model_validate(r).model_dump(mode="json") for r in rows],
    }


@router.post("")
async def add_blocklist(
    data: Union[BlocklistAdd, List[Union[BlocklistAdd, str]]],
    db: AsyncSession = Depends(get_db),
):
    """Add one entry (object) or many (list of objects or plain strings)."""
    if isinstance(data, BlocklistAdd):
        items = [data]
    else:
        items = [BlocklistAdd(value=x) if isinstance(x, str) else x for x in data]
    if not items:
        raise HTTPException(400, "No entries provided")
    result = await add_entries(db, items)
    await db.commit()
    return result


@router.delete("/{entry_id}")
async def delete_blocklist(entry_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(BlocklistEntry, entry_id)
    if row is None:
        raise HTTPException(404, "Blocklist entry not found")
    value = row.value
    await db.delete(row)
    await db.commit()
    return {"ok": True, "id": entry_id, "value": value}
