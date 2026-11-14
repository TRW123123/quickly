"""Blocklist helpers: global do-not-contact list of exact emails and whole domains.

Entries live in the ``blocklist`` table (``app.models.BlocklistEntry``). Values are
always stored lowercase; a domain entry blocks every address whose part after the
last ``@`` equals the domain exactly (no implicit subdomain matching).
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BlocklistEntry

KINDS = ("email", "domain")


def normalize(value: str | None) -> str:
    """Lowercase and trim a blocklist value; domains lose a leading ``@``."""
    v = (value or "").strip().lower()
    if v.startswith("@"):
        v = v[1:]
    return v


def detect_kind(value: str) -> str:
    """``email`` when the normalized value contains ``@``, otherwise ``domain``."""
    return "email" if "@" in normalize(value) else "domain"


def email_domain(email: str) -> str:
    e = normalize(email)
    return e.rsplit("@", 1)[1] if "@" in e else ""


def _candidates(email: str) -> list[str]:
    e = normalize(email)
    if not e:
        return []
    out = [e]
    dom = email_domain(e)
    if dom:
        out.append(dom)
    return out


async def is_blocked(session: AsyncSession, email: str) -> bool:
    """True when the exact email or its domain (after ``@``) is on the blocklist."""
    cands = _candidates(email)
    if not cands:
        return False
    res = await session.execute(
        select(BlocklistEntry.id).where(BlocklistEntry.value.in_(cands)).limit(1)
    )
    return res.first() is not None


async def blocked_emails(session: AsyncSession, emails: Iterable[str]) -> set[str]:
    """Return the subset of ``emails`` (normalized) that are blocked. One query per batch."""
    normed = {normalize(e) for e in emails if normalize(e)}
    if not normed:
        return set()
    values: set[str] = set(normed)
    values.update(d for d in (email_domain(e) for e in normed) if d)
    hits: set[str] = set()
    vals = list(values)
    # Chunk to stay well below SQL parameter limits (SQLite: 999 on old builds).
    for i in range(0, len(vals), 500):
        res = await session.execute(
            select(BlocklistEntry.value).where(BlocklistEntry.value.in_(vals[i:i + 500]))
        )
        hits.update(r[0] for r in res.all())
    if not hits:
        return set()
    return {e for e in normed if e in hits or email_domain(e) in hits}


async def add_email(session: AsyncSession, email: str, reason: str | None = None) -> bool:
    """Put one exact address on the blocklist; False when it was already there.

    Flushes so a second call in the same session sees the new row.
    """
    value = normalize(email)
    if not value or "@" not in value:
        return False
    existing = await session.execute(
        select(BlocklistEntry.id).where(BlocklistEntry.value == value).limit(1)
    )
    if existing.first() is not None:
        return False
    session.add(BlocklistEntry(value=value, kind="email", reason=(reason or None)))
    await session.flush()
    return True
