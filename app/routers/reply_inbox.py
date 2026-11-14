"""Reply inbox API: read and triage the bot-facing ``reply_inbox`` table.

Read-only apart from the triage fields.  Nothing here sends or answers mail.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import CampaignLead, ReplyInbox
from app.time import utcnow

router = APIRouter(prefix="/api/reply-inbox", tags=["reply-inbox"])

StatusValue = Literal["neu", "gesehen", "erledigt", "auto"]


class ReplyInboxItem(BaseModel):
    id: int
    lead_reply_id: int
    lead_id: int
    campaign_id: int
    inbox_id: Optional[int] = None
    inbox_email: Optional[str] = None
    from_email: str
    from_name: str
    company: Optional[str] = None
    subject: str
    snippet: str
    received_at: Optional[datetime] = None
    classification: Optional[str] = None
    classification_source: Optional[str] = None
    is_positive: Optional[bool] = None
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ReplyInboxList(BaseModel):
    items: list[ReplyInboxItem]
    total: int


class ReplyInboxPatch(BaseModel):
    status: Optional[StatusValue] = None
    is_positive: Optional[bool] = None


@router.get("", response_model=ReplyInboxList)
async def list_reply_inbox(
    status: Optional[StatusValue] = Query(default=None),
    is_positive: Optional[bool] = Query(default=None),
    since: Optional[datetime] = Query(
        default=None, description="received_at (fallback created_at) >= since, UTC"
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    conds = []
    if status is not None:
        conds.append(ReplyInbox.status == status)
    if is_positive is not None:
        conds.append(ReplyInbox.is_positive.is_(is_positive))
    if since is not None:
        if since.tzinfo is not None:
            # stored timestamps are naive UTC
            since = since.astimezone(timezone.utc).replace(tzinfo=None)
        conds.append(func.coalesce(ReplyInbox.received_at, ReplyInbox.created_at) >= since)
    where = and_(*conds) if conds else None

    count_q = select(func.count(ReplyInbox.id))
    list_q = select(ReplyInbox)
    if where is not None:
        count_q = count_q.where(where)
        list_q = list_q.where(where)
    total = (await db.execute(count_q)).scalar() or 0
    rows = (
        await db.execute(
            list_q.order_by(
                func.coalesce(ReplyInbox.received_at, ReplyInbox.created_at).desc(),
                ReplyInbox.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return ReplyInboxList(items=[ReplyInboxItem.model_validate(r) for r in rows], total=total)


@router.patch("/{item_id}", response_model=ReplyInboxItem)
async def patch_reply_inbox(
    item_id: int,
    body: ReplyInboxPatch,
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(select(ReplyInbox).where(ReplyInbox.id == item_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="reply_inbox entry not found")
    if body.status is None and body.is_positive is None:
        raise HTTPException(status_code=422, detail="nothing to update")
    if body.status is not None:
        row.status = body.status
    if body.is_positive is not None:
        row.is_positive = body.is_positive
        if body.is_positive:
            # A bot/human confirmed the reply as positive: mirror it in Quickly.
            cl = (
                await db.execute(
                    select(CampaignLead).where(
                        CampaignLead.lead_id == row.lead_id,
                        CampaignLead.campaign_id == row.campaign_id,
                    )
                )
            ).scalar_one_or_none()
            if cl is not None:
                cl.interest_status = "interested"
    row.updated_at = utcnow()
    await db.commit()
    await db.refresh(row)
    return ReplyInboxItem.model_validate(row)
