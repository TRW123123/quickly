"""CRM leads: positive replies copied from ``reply_inbox`` into ``crm_leads``.

The ``sync_crm_leads`` job (every 10 minutes, registered in app/main.py only when
the scheduler runs) inserts one ``crm_leads`` row per ``reply_inbox`` row with
``is_positive = TRUE`` that is not copied yet (unique ``reply_inbox_id``).  Name
and company come from the lead record.  Existing CRM rows are never overwritten,
so a status set by the owner (``neu`` -> ``kontaktiert`` ...) stays.

SAFETY: this module only READS ``reply_inbox`` / ``lead`` / ``campaign`` and
WRITES ``crm_leads``.  It never sends, answers or queues an email.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import database as _database
from app.models import Campaign, CrmLead, Lead, ReplyInbox
from app.time import utcnow

log = logging.getLogger("quickly.crm_leads")

SYNC_INTERVAL_MINUTES = 10
BATCH_SIZE = 500

_COMPANY_KEYS = (
    "company", "Company", "firma", "Firma", "company_name", "companyName",
    "Company Name", "unternehmen", "Unternehmen", "organization", "organisation",
)
_FIRST_KEYS = ("first_name", "firstName", "firstname", "First Name", "vorname", "Vorname")
_LAST_KEYS = ("last_name", "lastName", "lastname", "Last Name", "nachname", "Nachname")


def _first(data: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = data.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def lead_name(lead: Lead | None, fallback: str = "") -> str:
    if lead is not None:
        data = lead.custom_data if isinstance(lead.custom_data, dict) else {}
        full = " ".join(p for p in (_first(data, _FIRST_KEYS), _first(data, _LAST_KEYS)) if p)
        if full:
            return full[:255]
        if lead.name:
            return lead.name.strip()[:255]
    return (fallback or "")[:255]


def lead_company(lead: Lead | None, fallback: str | None = None) -> str | None:
    if lead is not None:
        data = lead.custom_data if isinstance(lead.custom_data, dict) else {}
        c = _first(data, _COMPANY_KEYS)
        if c:
            return c[:255]
    return fallback


def _insert_stmt(db: AsyncSession, values: dict[str, Any]):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:  # pragma: no cover - only Postgres and SQLite are supported
        raise RuntimeError(f"crm_leads insert not supported on {dialect}")
    return _insert(CrmLead).values(**values).on_conflict_do_nothing(index_elements=["reply_inbox_id"])


async def sync_crm_leads(db: AsyncSession) -> dict[str, int]:
    """Copy positive ``reply_inbox`` rows into ``crm_leads``. Idempotent, never sends."""
    stats = {"inserted": 0}
    rows = await db.execute(
        select(ReplyInbox, Lead, Campaign)
        .outerjoin(Lead, Lead.id == ReplyInbox.lead_id)
        .outerjoin(Campaign, Campaign.id == ReplyInbox.campaign_id)
        .outerjoin(CrmLead, CrmLead.reply_inbox_id == ReplyInbox.id)
        .where(ReplyInbox.is_positive.is_(True), CrmLead.id.is_(None))
        .order_by(ReplyInbox.id.asc())
        .limit(BATCH_SIZE)
    )
    now = utcnow()
    for ri, lead, campaign in rows.all():
        values = {
            "reply_inbox_id": ri.id,
            "lead_id": ri.lead_id,
            "name": lead_name(lead, ri.from_name or ""),
            "company": lead_company(lead, ri.company),
            "email": ((lead.email if lead is not None else "") or ri.from_email or "").lower()[:255],
            "campaign_id": ri.campaign_id,
            "campaign_name": (campaign.name[:255] if campaign is not None and campaign.name else None),
            "replied_at": ri.received_at or ri.created_at,
            "status": "neu",
            "created_at": now,
        }
        result = await db.execute(_insert_stmt(db, values))
        if result.rowcount:
            stats["inserted"] += 1
    await db.commit()
    return stats


async def run_crm_leads_sync_job() -> None:
    """APScheduler entry point (``sync_crm_leads``, every 10 minutes)."""
    try:
        async with _database.AsyncSessionLocal() as db:
            stats = await sync_crm_leads(db)
        if stats["inserted"]:
            log.info("crm_leads sync: %s", stats)
    except Exception:
        log.exception("crm_leads sync failed")
