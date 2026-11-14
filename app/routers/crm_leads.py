"""CRM leads API: read positive replies copied by app/crm_leads.py, set their status.

Read-only apart from ``status``.  Nothing here sends or answers mail.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import CrmLead

router = APIRouter(prefix="/api/crm-leads", tags=["crm-leads"])


class CrmLeadItem(BaseModel):
    id: int
    reply_inbox_id: int
    lead_id: Optional[int] = None
    name: str
    company: Optional[str] = None
    email: str
    campaign_id: Optional[int] = None
    campaign_name: Optional[str] = None
    replied_at: Optional[datetime] = None
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class CrmLeadList(BaseModel):
    items: list[CrmLeadItem]
    total: int


class CrmLeadPatch(BaseModel):
    # Free text on purpose (neu, kontaktiert, termin, angebot, gewonnen, verloren ...)
    status: str = Field(..., min_length=1, max_length=32)


@router.get("", response_model=CrmLeadList)
async def list_crm_leads(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(CrmLead)
    count = select(func.count(CrmLead.id))
    if status:
        stmt = stmt.where(CrmLead.status == status)
        count = count.where(CrmLead.status == status)
    total = (await db.execute(count)).scalar() or 0
    rows = await db.execute(
        stmt.order_by(CrmLead.replied_at.desc().nullslast(), CrmLead.id.desc()).limit(limit).offset(offset)
    )
    return CrmLeadList(items=[CrmLeadItem.model_validate(r) for r in rows.scalars().all()], total=total)


@router.patch("/{crm_lead_id}", response_model=CrmLeadItem)
async def patch_crm_lead(crm_lead_id: int, body: CrmLeadPatch, db: AsyncSession = Depends(get_db)):
    row = await db.get(CrmLead, crm_lead_id)
    if row is None:
        raise HTTPException(404, "CRM lead not found")
    row.status = body.status.strip()
    await db.flush()
    return CrmLeadItem.model_validate(row)
