"""Campaign MCP tools (registered on the existing ``/api/mcp`` server from ``app.mcp_leads``).

Unlike the lead tools, which proxy to the REST API over HTTP, these tools talk to the
database directly; MCP authentication is already enforced by ``_MCPAuthASGI`` before
any tool runs. Business logic lives in ``*_core`` functions that take a session so it
can be tested without an HTTP round trip.

Safety rules baked in here:
- ``create_campaign`` always creates a paused campaign (draft), whatever is passed.
- ``start_campaign`` / ``resume_campaign`` only act when ``approved_by_safak`` is
  literally ``true``; every attempt (approved or rejected) is written to
  ``campaign_start_log``.
- Every lead import path skips addresses on the blocklist.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from mcp.server.fastmcp import Context
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app import database as _adb
from app.blocklist import blocked_emails
from app.mcp_leads import leads_mcp
from app.models import (
    APIKey,
    Campaign,
    CampaignInbox,
    CampaignLead,
    CampaignStartLog,
    EmailLog,
    GmailMessage,
    Inbox,
    Lead,
    LeadReply,
    Office365Message,
    Sequence,
    SmtpMessage,
    User,
)

log = logging.getLogger("quickly.mcp_campaigns")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SNIPPET_LEN = 300
_background_tasks: set[asyncio.Task] = set()


class ToolError(Exception):
    """Expected, user-facing failure; rendered as ``{"error": ...}``."""


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _chunks(seq: list, n: int = 500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------------------
# Caller identity (for the start log)
# ---------------------------------------------------------------------------

async def _caller_identity(ctx: Context | None, db: AsyncSession) -> dict:
    """Best-effort ``{"api_key_id", "user"}`` for the MCP request; never raises."""
    out: dict = {"api_key_id": None, "user": None}
    if ctx is None:
        return out
    try:
        req = ctx.request_context.request
    except Exception:
        return out
    if not isinstance(req, Request):
        return out
    try:
        from app.auth import decode_token, hash_api_key

        raw_key = (req.headers.get("x-api-key") or "").strip()
        bearer = (req.headers.get("authorization") or "").strip()
        if not raw_key and bearer.lower().startswith("bearer "):
            tok = bearer[7:].strip()
            if tok and "." not in tok:
                raw_key = tok
            elif tok:
                payload = decode_token(tok)
                uid = payload.get("sub")
                if uid:
                    user = await db.get(User, int(uid))
                    out["user"] = getattr(user, "email", None) or f"user:{uid}"
                return out
        if raw_key:
            res = await db.execute(select(APIKey).where(APIKey.key_hash == hash_api_key(raw_key)))
            ak = res.scalar_one_or_none()
            if ak is not None:
                out["api_key_id"] = ak.id
                user = await db.get(User, ak.user_id)
                out["user"] = getattr(user, "email", None) or f"user:{ak.user_id}"
    except Exception:
        log.debug("could not resolve MCP caller identity", exc_info=True)
    return out


def _schedule_global_recalc() -> None:
    """Rebuild the queue after a pause-state change (same as PATCH /api/campaigns/{id})."""
    from app.routers.schedule import run_recalculate_all_in_new_session

    try:
        task = asyncio.get_running_loop().create_task(run_recalculate_all_in_new_session())
    except RuntimeError:
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ---------------------------------------------------------------------------
# Core logic (session-based, testable)
# ---------------------------------------------------------------------------

async def _count_by_campaign(db: AsyncSession, stmt) -> dict[int, int]:
    return {cid: cnt for cid, cnt in (await db.execute(stmt)).all()}


async def list_campaigns_core(db: AsyncSession) -> list[dict]:
    campaigns = (
        await db.execute(select(Campaign).order_by(Campaign.priority, Campaign.id))
    ).scalars().all()
    if not campaigns:
        return []
    ids = [c.id for c in campaigns]
    leads = await _count_by_campaign(
        db,
        select(CampaignLead.campaign_id, func.count(CampaignLead.id))
        .where(CampaignLead.campaign_id.in_(ids))
        .group_by(CampaignLead.campaign_id),
    )
    sent = await _count_by_campaign(
        db,
        select(EmailLog.campaign_id, func.count(EmailLog.id))
        .where(EmailLog.campaign_id.in_(ids))
        .group_by(EmailLog.campaign_id),
    )
    steps = await _count_by_campaign(
        db,
        select(Sequence.campaign_id, func.count(Sequence.id))
        .where(Sequence.campaign_id.in_(ids))
        .group_by(Sequence.campaign_id),
    )
    inbox_rows = (
        await db.execute(
            select(CampaignInbox.campaign_id, CampaignInbox.inbox_id)
            .where(CampaignInbox.campaign_id.in_(ids))
            .order_by(CampaignInbox.position, CampaignInbox.inbox_id)
        )
    ).all()
    inboxes: dict[int, list[int]] = {}
    for cid, iid in inbox_rows:
        inboxes.setdefault(cid, []).append(iid)
    return [
        {
            "id": c.id,
            "name": c.name,
            "status": "paused" if c.paused else "active",
            "paused": bool(c.paused),
            "leads": leads.get(c.id, 0),
            "sent": sent.get(c.id, 0),
            "sequence_steps": steps.get(c.id, 0),
            "inbox_ids": inboxes.get(c.id, []),
            "created_at": c.created_at,
        }
        for c in campaigns
    ]


async def create_campaign_core(
    db: AsyncSession,
    name: str,
    steps: list[dict] | None = None,
    inbox_ids: list[int] | None = None,
) -> dict:
    name = (name or "").strip()
    if not name:
        raise ToolError("name is required")
    inbox_ids = [int(i) for i in (inbox_ids or [])]
    if inbox_ids:
        found = {
            r[0] for r in (await db.execute(select(Inbox.id).where(Inbox.id.in_(inbox_ids)))).all()
        }
        missing = [i for i in inbox_ids if i not in found]
        if missing:
            raise ToolError(f"unknown inbox ids: {missing}")
    parsed_steps: list[tuple[str | None, str, int]] = []
    for idx, raw in enumerate(steps or []):
        if not isinstance(raw, dict):
            raise ToolError(f"step {idx}: expected object with subject, body, delay_days")
        body = str(raw.get("body") or "").strip()
        if not body:
            raise ToolError(f"step {idx}: body is required")
        subject = raw.get("subject")
        subject = str(subject).strip() if subject is not None else None
        try:
            delay = int(raw.get("delay_days") or 0)
        except (TypeError, ValueError):
            raise ToolError(f"step {idx}: delay_days must be an integer")
        if delay < 0:
            raise ToolError(f"step {idx}: delay_days must be >= 0")
        parsed_steps.append((subject or None, body, delay))

    # Always a draft: paused regardless of anything the caller sent.
    campaign = Campaign(name=name, paused=True)
    db.add(campaign)
    await db.flush()
    seen_inboxes: list[int] = []
    for iid in inbox_ids:
        if iid not in seen_inboxes:
            seen_inboxes.append(iid)
    for pos, iid in enumerate(seen_inboxes):
        db.add(CampaignInbox(campaign_id=campaign.id, inbox_id=iid, position=pos))
    for pos, (subject, body, delay) in enumerate(parsed_steps):
        db.add(
            Sequence(
                campaign_id=campaign.id,
                position=pos,
                subject=subject,
                body=body,
                wait_days_after_previous=delay,
            )
        )
    await db.flush()
    return {
        "id": campaign.id,
        "name": campaign.name,
        "status": "paused",
        "paused": True,
        "sequence_steps": len(parsed_steps),
        "inbox_ids": seen_inboxes,
        "note": "Created as paused draft. Use start_campaign with approved_by_safak=true to start sending.",
    }


def _parse_csv_leads(text: str) -> list[dict]:
    text = (text or "").lstrip("﻿")
    if not text.strip():
        return []
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return []
    header = [(h or "").strip() for h in rows[0]]
    lower = [h.lower() for h in header]
    if "email" not in lower:
        # Headerless: first column is the email address.
        return [{"email": r[0]} for r in rows if r and (r[0] or "").strip()]
    out: list[dict] = []
    for r in rows[1:]:
        if not any((c or "").strip() for c in r):
            continue
        item: dict = {"custom_data": {}}
        for h, hl, val in zip(header, lower, r):
            val = (val or "").strip()
            if not h:
                continue
            if hl == "email":
                item["email"] = val
            elif hl == "name":
                item["name"] = val
            elif val:
                item["custom_data"][h] = val
        out.append(item)
    return out


def _coerce_leads_input(leads: Any, csv_text: str | None) -> list[dict]:
    items: list[dict] = []
    if isinstance(leads, str) and leads.strip():
        s = leads.strip()
        if s[:1] in "[{":
            try:
                leads = json.loads(s)
            except json.JSONDecodeError:
                raise ToolError("leads is not valid JSON")
            if isinstance(leads, dict):
                leads = [leads]
        else:
            items.extend(_parse_csv_leads(s))
            leads = None
    if isinstance(leads, list):
        for x in leads:
            if isinstance(x, str):
                items.append({"email": x})
            elif isinstance(x, dict):
                items.append(x)
            else:
                items.append({"email": str(x)})
    elif leads not in (None, ""):
        raise ToolError("leads must be a JSON list or CSV text")
    if csv_text:
        items.extend(_parse_csv_leads(csv_text))
    return items


async def add_leads_core(
    db: AsyncSession,
    campaign_id: int,
    leads: Any = None,
    csv_text: str | None = None,
) -> dict:
    from app.routers.campaigns import CampaignLeadAdd, bulk_add_leads_to_campaign

    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise ToolError(f"campaign {campaign_id} not found")
    raw_items = _coerce_leads_input(leads, csv_text)
    if not raw_items:
        raise ToolError("no leads provided (pass leads as JSON list or csv text)")

    invalid: list[str] = []
    duplicates: list[dict] = []
    candidates: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for item in raw_items:
        raw_email = str(item.get("email") or "")
        email = raw_email.strip().lower()
        if not email or len(email) > 255 or not _EMAIL_RE.match(email):
            invalid.append(raw_email)
            continue
        if email in seen:
            duplicates.append({"email": email, "reason": "duplicate_in_input"})
            continue
        seen.add(email)
        candidates.append((email, item))

    blocked_set = await blocked_emails(db, [e for e, _ in candidates])
    blocked = [e for e, _ in candidates if e in blocked_set]
    candidates = [(e, it) for e, it in candidates if e not in blocked_set]

    # Duplicate check against ALL existing leads (any campaign or none).
    existing: set[str] = set()
    for chunk in _chunks([e for e, _ in candidates]):
        res = await db.execute(select(func.lower(Lead.email)).where(func.lower(Lead.email).in_(chunk)))
        existing.update(r[0] for r in res.all())
    for e, _ in candidates:
        if e in existing:
            duplicates.append({"email": e, "reason": "existing_lead"})
    candidates = [(e, it) for e, it in candidates if e not in existing]

    added: list[str] = []
    errors: list[dict] = []
    disposable: list[str] = []
    check_report: dict = {}
    if candidates:
        payload = []
        for e, it in candidates:
            custom = it.get("custom_data")
            custom = dict(custom) if isinstance(custom, dict) else {}
            for k, v in it.items():
                if k not in ("email", "name", "custom_data") and v not in (None, ""):
                    custom.setdefault(str(k), v)
            payload.append(CampaignLeadAdd(email=e, name=str(it.get("name") or ""), custom_data=custom))
        result = await bulk_add_leads_to_campaign(
            campaign_id=campaign_id,
            leads_data=payload,
            skip_duplicates=True,
            verify_emails=False,
            confirm_only=False,
            db=db,
        )
        for r in result.get("results", []):
            st = r.get("status")
            if st == "added":
                added.append(r["email"])
            elif st == "already_enrolled":
                duplicates.append({"email": r["email"], "reason": "already_enrolled"})
            elif st == "blocked":
                blocked.append(r["email"])
            elif st == "invalid_email":
                invalid.append(r["email"])
            elif st == "disposable":
                disposable.append(r["email"])
            else:
                errors.append({"email": r.get("email"), "detail": r.get("detail")})
        check_report = result.get("email_check") or {}
    await db.flush()
    return {
        "campaign_id": campaign_id,
        "campaign_paused": bool(campaign.paused),
        "added": {"count": len(added), "emails": added},
        "duplicates": {"count": len(duplicates), "emails": [d["email"] for d in duplicates], "details": duplicates},
        "blocked": {"count": len(blocked), "emails": blocked},
        "invalid": {"count": len(invalid), "emails": invalid},
        # Free address check (app/email_check.py): disposable skipped, role imported
        # but flagged, no_mx = no MX record (A fallback or dead domain), unknown = DNS failed.
        "disposable": {"count": len(disposable), "emails": disposable},
        "role": check_report.get("role") or {"count": 0, "emails": []},
        "no_mx": check_report.get("no_mx") or {"count": 0, "emails": []},
        "unknown": check_report.get("unknown") or {"count": 0, "emails": []},
        "errors": errors,
    }


async def get_stats_core(db: AsyncSession, campaign_id: int | None = None) -> dict:
    if campaign_id is not None and await db.get(Campaign, campaign_id) is None:
        raise ToolError(f"campaign {campaign_id} not found")

    def _scope(stmt, col):
        return stmt.where(col == campaign_id) if campaign_id is not None else stmt

    async def _scalar(stmt) -> int:
        return int((await db.execute(stmt)).scalar() or 0)

    sent = await _scalar(_scope(select(func.count(EmailLog.id)), EmailLog.campaign_id))
    opened = await _scalar(
        _scope(select(func.count(EmailLog.id)).where(EmailLog.opened == True), EmailLog.campaign_id)  # noqa: E712
    )
    clicked = await _scalar(
        _scope(select(func.count(EmailLog.id)).where(EmailLog.clicked == True), EmailLog.campaign_id)  # noqa: E712
    )
    replies = await _scalar(_scope(select(func.count(LeadReply.id)), LeadReply.campaign_id))
    replies_human = await _scalar(
        _scope(
            select(func.count(LeadReply.id))
            .join(
                CampaignLead,
                (LeadReply.lead_id == CampaignLead.lead_id)
                & (LeadReply.campaign_id == CampaignLead.campaign_id),
            )
            .where(
                (CampaignLead.interest_status.is_(None))
                | (CampaignLead.interest_status.notin_(["out_of_office", "auto_reply"]))
            ),
            LeadReply.campaign_id,
        )
    )
    bounces = await _scalar(
        _scope(
            select(func.count(CampaignLead.id)).where(CampaignLead.enrollment_status == "bounced"),
            CampaignLead.campaign_id,
        )
    )
    leads = await _scalar(_scope(select(func.count(CampaignLead.id)), CampaignLead.campaign_id))

    def _rate(n: int) -> float:
        return round(n / sent, 4) if sent else 0.0

    return {
        "scope": f"campaign:{campaign_id}" if campaign_id is not None else "all",
        "campaign_id": campaign_id,
        "leads": leads,
        "sent": sent,
        "opens": opened,
        "clicks": clicked,
        "replies": replies,
        "replies_excluding_auto": replies_human,
        "bounces": bounces,
        "open_rate": _rate(opened),
        "click_rate": _rate(clicked),
        "reply_rate": _rate(replies),
        "bounce_rate": _rate(bounces),
        "note": "opens/clicks count emails with at least one open/click; only tracked when enabled on the campaign.",
    }


def _parse_since(since: str | None) -> datetime | None:
    if not since or not str(since).strip():
        return None
    s = str(since).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError("since must be an ISO date or datetime, e.g. 2026-11-01 or 2026-11-01T08:00:00Z")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _gmail_header(headers_json: str, name: str) -> str:
    try:
        for h in json.loads(headers_json or "[]"):
            if str(h.get("name", "")).lower() == name:
                return str(h.get("value") or "")
    except Exception:
        pass
    return ""


async def _reply_message(db: AsyncSession, email: str, replied_at: datetime | None) -> dict:
    """Best-effort subject/snippet of the lead's inbound message closest to ``replied_at``."""
    pat = f"%{email}%"
    best: tuple[datetime, str, str] | None = None

    def _consider(when: datetime | None, subject: str, text: str):
        nonlocal best
        when = when or datetime.min
        if replied_at is not None and when > replied_at + timedelta(days=1):
            return
        if best is None or when > best[0]:
            best = (when, subject or "", text or "")

    res = await db.execute(
        select(SmtpMessage.received_at, SmtpMessage.subject, SmtpMessage.body_plain)
        .where(func.lower(SmtpMessage.from_address).like(pat), SmtpMessage.direction == "received")
        .order_by(SmtpMessage.received_at.desc())
        .limit(5)
    )
    for when, subj, body in res.all():
        _consider(when, subj, body)
    res = await db.execute(
        select(Office365Message.received_at, Office365Message.subject, Office365Message.body_plain)
        .where(func.lower(Office365Message.from_address).like(pat))
        .order_by(Office365Message.received_at.desc())
        .limit(5)
    )
    for when, subj, body in res.all():
        _consider(when, subj, body)
    res = await db.execute(
        select(GmailMessage.internal_date, GmailMessage.headers_json, GmailMessage.snippet)
        .where(func.lower(GmailMessage.headers_json).like(pat))
        .order_by(GmailMessage.internal_date.desc())
        .limit(10)
    )
    for internal_date, headers_json, snippet in res.all():
        if email not in _gmail_header(headers_json, "from").lower():
            continue
        when = (
            datetime.fromtimestamp(internal_date / 1000, timezone.utc).replace(tzinfo=None)
            if internal_date
            else None
        )
        _consider(when, _gmail_header(headers_json, "subject"), snippet)
    if best is None:
        return {"subject": None, "snippet": None}
    text = " ".join((best[2] or "").split())
    return {"subject": best[1] or None, "snippet": text[:_SNIPPET_LEN] or None}


async def list_replies_core(
    db: AsyncSession,
    campaign_id: int | None = None,
    since: str | None = None,
    limit: int = 20,
) -> dict:
    limit = max(1, min(int(limit or 20), 200))
    since_dt = _parse_since(since)
    stmt = (
        select(LeadReply, Lead, Campaign, CampaignLead)
        .join(Lead, Lead.id == LeadReply.lead_id)
        .join(Campaign, Campaign.id == LeadReply.campaign_id)
        .outerjoin(
            CampaignLead,
            (CampaignLead.lead_id == LeadReply.lead_id)
            & (CampaignLead.campaign_id == LeadReply.campaign_id),
        )
    )
    if campaign_id is not None:
        stmt = stmt.where(LeadReply.campaign_id == campaign_id)
    if since_dt is not None:
        stmt = stmt.where(LeadReply.replied_at >= since_dt)
    stmt = stmt.order_by(LeadReply.replied_at.desc(), LeadReply.id.desc()).limit(limit)
    items = []
    for reply, lead, campaign, cl in (await db.execute(stmt)).all():
        msg = await _reply_message(db, (lead.email or "").lower(), reply.replied_at)
        items.append(
            {
                "reply_id": reply.id,
                "replied_at": reply.replied_at,
                "lead": {"id": lead.id, "email": lead.email, "name": lead.name or ""},
                "campaign": {"id": campaign.id, "name": campaign.name},
                "subject": msg["subject"],
                "snippet": msg["snippet"],
                "classification": getattr(cl, "interest_status", None) if cl is not None else None,
                "enrollment_status": getattr(cl, "enrollment_status", None) if cl is not None else None,
            }
        )
    return {"count": len(items), "replies": items}


async def pause_campaign_core(db: AsyncSession, campaign_id: int) -> dict:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise ToolError(f"campaign {campaign_id} not found")
    changed = not campaign.paused
    campaign.paused = True
    await db.flush()
    return {"id": campaign.id, "name": campaign.name, "status": "paused", "paused": True, "changed": changed}


async def _start_preconditions(db: AsyncSession, campaign: Campaign) -> str | None:
    n_seq = (
        await db.execute(select(func.count(Sequence.id)).where(Sequence.campaign_id == campaign.id))
    ).scalar() or 0
    if not n_seq:
        return "campaign has no sequence steps"
    n_inbox = (
        await db.execute(
            select(func.count(CampaignInbox.id)).where(CampaignInbox.campaign_id == campaign.id)
        )
    ).scalar() or 0
    if not n_inbox:
        return "campaign has no inboxes assigned"
    return None


async def start_or_resume_core(
    db: AsyncSession,
    campaign_id: int,
    action: str,
    approved_by_safak: Any,
    caller: dict | None = None,
) -> dict:
    """Unpause a campaign (Quickly's only start mechanism) if explicitly approved.

    Always writes one ``campaign_start_log`` row. Returns ``{"ok": bool, ...}``;
    ``changed`` tells the caller whether a queue recalculation is needed.
    """
    assert action in ("start", "resume")
    caller = caller or {}
    approved = approved_by_safak is True
    campaign = await db.get(Campaign, campaign_id) if isinstance(campaign_id, int) else None

    ok = False
    changed = False
    if not approved:
        result = f"rejected: approved_by_safak must be true (got {json.dumps(approved_by_safak, default=str)})"
    elif campaign is None:
        result = "rejected: campaign not found"
    else:
        problem = await _start_preconditions(db, campaign)
        if problem:
            result = f"rejected: {problem}"
        elif not campaign.paused:
            ok = True
            result = "unchanged: campaign already active"
        else:
            campaign.paused = False
            ok = True
            changed = True
            result = "started" if action == "start" else "resumed"

    db.add(
        CampaignStartLog(
            campaign_id=campaign_id if isinstance(campaign_id, int) else None,
            action=action,
            approved_by_safak=approved_by_safak if isinstance(approved_by_safak, bool) else None,
            api_key_id=caller.get("api_key_id"),
            user=caller.get("user"),
            result=result,
        )
    )
    await db.flush()
    out: dict = {"ok": ok, "action": action, "campaign_id": campaign_id, "result": result, "changed": changed}
    if campaign is not None:
        out.update({"name": campaign.name, "status": "paused" if campaign.paused else "active", "paused": bool(campaign.paused)})
    if not ok:
        out["error"] = result
    return out


# ---------------------------------------------------------------------------
# MCP tool wrappers
# ---------------------------------------------------------------------------

async def _exec(fn, *args, **kwargs) -> dict:
    """Run ``fn(db, ...)`` in its own session and commit; errors become ``{"error": ...}``."""
    async with _adb.AsyncSessionLocal() as db:
        try:
            data = await fn(db, *args, **kwargs)
            await db.commit()
            return data
        except ToolError as exc:
            await db.rollback()
            return {"error": str(exc)}
        except Exception:
            await db.rollback()
            log.exception("MCP tool %s failed", getattr(fn, "__name__", fn))
            return {"error": "internal error"}


async def _run(fn, *args, **kwargs) -> str:
    return _dump(await _exec(fn, *args, **kwargs))


@leads_mcp.tool()
async def list_campaigns(ctx: Context) -> str:
    """List all campaigns with id, name, status (active/paused), lead count, emails sent, steps and inbox ids."""
    return await _run(list_campaigns_core)


@leads_mcp.tool()
async def create_campaign(
    ctx: Context,
    name: str,
    steps: list[dict[str, Any]] | None = None,
    inbox_ids: list[int] | None = None,
    paused: Any = None,
) -> str:
    """Create a campaign as a PAUSED draft. steps: [{subject, body, delay_days}] in send order
    (empty subject = reply in same thread). inbox_ids: sending mailboxes. The `paused` argument is
    ignored — campaigns are always created paused; use start_campaign with approval to send."""
    return await _run(create_campaign_core, name, steps, inbox_ids)


@leads_mcp.tool()
async def add_leads(
    ctx: Context,
    campaign_id: int,
    leads: list[dict[str, Any] | str] | str | None = None,
    csv: str | None = None,
) -> str:
    """Add leads to a campaign. leads: JSON list of {email, name?, custom_data?} (extra keys go to
    custom_data) or plain emails; or csv: CSV text with an `email` header column (name + custom columns).
    Skips duplicates against ALL existing leads, blocklisted addresses, invalid emails (syntax or
    domain without MX/A record) and disposable domains; role addresses (info@, kontakt@ ...) are
    added but flagged. Returns added/duplicates/blocked/invalid/disposable/role/no_mx/unknown
    with counts and addresses."""
    return await _run(add_leads_core, campaign_id, leads, csv)


@leads_mcp.tool()
async def get_stats(ctx: Context, campaign_id: int | None = None) -> str:
    """Sent, opens, clicks, replies and bounces for one campaign (campaign_id) or all campaigns."""
    return await _run(get_stats_core, campaign_id)


@leads_mcp.tool()
async def list_replies(
    ctx: Context,
    campaign_id: int | None = None,
    since: str | None = None,
    limit: int = 20,
) -> str:
    """Latest replies (newest first) with lead, campaign, date, subject, snippet and AI classification.
    Filters: campaign_id, since (ISO date/datetime, UTC). limit max 200."""
    return await _run(list_replies_core, campaign_id, since, limit)


@leads_mcp.tool()
async def pause_campaign(ctx: Context, campaign_id: int) -> str:
    """Pause a campaign (stops sending, frees its queue slots). No approval needed."""
    data = await _exec(pause_campaign_core, campaign_id)
    if isinstance(data, dict) and data.get("changed"):
        _schedule_global_recalc()
    return _dump(data)


_APPROVAL_FIELD = Field(
    description="Must be exactly true (explicit approval by Safak). Anything else is rejected and logged.",
    json_schema_extra={"type": "boolean"},
)


async def _start_tool(ctx: Context, campaign_id: int, action: str, approved_by_safak: Any) -> str:
    async with _adb.AsyncSessionLocal() as db:
        try:
            caller = await _caller_identity(ctx, db)
            data = await start_or_resume_core(db, campaign_id, action, approved_by_safak, caller)
            await db.commit()
        except Exception:
            await db.rollback()
            log.exception("MCP %s_campaign failed", action)
            return _dump({"error": "internal error"})
    if data.get("changed"):
        _schedule_global_recalc()
    return _dump(data)


@leads_mcp.tool()
async def start_campaign(
    ctx: Context,
    campaign_id: int,
    approved_by_safak: Annotated[Any, _APPROVAL_FIELD],
) -> str:
    """Start sending a draft/paused campaign (sets paused=false and rebuilds the queue). Requires
    approved_by_safak=true; requires at least one sequence step and one inbox. Every attempt is logged."""
    return await _start_tool(ctx, campaign_id, "start", approved_by_safak)


@leads_mcp.tool()
async def resume_campaign(
    ctx: Context,
    campaign_id: int,
    approved_by_safak: Annotated[Any, _APPROVAL_FIELD],
) -> str:
    """Resume a paused campaign (paused=false, queue rebuilt). Requires approved_by_safak=true.
    Every attempt is logged."""
    return await _start_tool(ctx, campaign_id, "resume", approved_by_safak)
