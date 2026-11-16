"""Shared email tracking side effects (open, click, unsubscribe).

Used by the public tracking router and the Beacon ingest endpoint.
"""
from __future__ import annotations

# // BEACON SYNC: This file handles tracking logic that Beacon mirrors.
# // Any changes to tracking behavior, event types, metadata collected, or URL structure
# // must also be reflected in the Beacon service (/beacon or the separate Beacon repo).
# // If you are an AI agent, add a TODO at the end of your response listing what needs to be updated in Beacon.

import logging
from html import escape as html_escape
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import time as time_provider
from app.models import (
    BlocklistEntry,
    CampaignLead,
    EmailClick,
    EmailLog,
    EmailOpen,
    KnownIP,
    Lead,
    LeadUnsubscribeToken,
    QueueSlot,
    TrackedLink,
    UnsubscribeEvent,
)
from app.webhooks import fire_webhook_event

if TYPE_CHECKING:
    pass

log = logging.getLogger("quickly.tracking_events")

_UNSUBSCRIBE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Unsubscribed</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           display: flex; align-items: center; justify-content: center;
           min-height: 100vh; margin: 0; background: #f9fafb; }
    .card { background: #fff; border-radius: 12px; padding: 48px 40px;
            box-shadow: 0 4px 24px rgba(0,0,0,.08); text-align: center; max-width: 420px; }
    .icon { font-size: 48px; margin-bottom: 16px; }
    h1 { margin: 0 0 8px; font-size: 24px; color: #111; }
    p  { color: #555; margin: 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✓</div>
    <h1>You've been unsubscribed</h1>
    <p>You will no longer receive emails from us.<br>
       If this was a mistake please contact the sender directly.</p>
  </div>
</body>
</html>"""

ALREADY_UNSUBSCRIBED_HTML = _UNSUBSCRIBE_HTML.replace(
    "You've been unsubscribed",
    "Already unsubscribed",
).replace(
    "You will no longer receive emails from us.<br>If this was a mistake please contact the sender directly.",
    "You are already unsubscribed and will not receive any more emails from us.",
)

INVALID_UNSUBSCRIBE_HTML = "<p>Invalid or expired unsubscribe link.</p>"


async def is_known_ip(db: AsyncSession, ip: str | None) -> bool:
    if not ip:
        return False
    now = time_provider.utcnow()
    result = await db.execute(
        select(KnownIP).where(
            KnownIP.ip_address == ip,
            (KnownIP.permanent == True) | (KnownIP.expires_at > now),  # noqa: E712
        )
    )
    return result.scalar_one_or_none() is not None


async def resolve_email_log_for_open(db: AsyncSession, token: str) -> EmailLog | None:
    result = await db.execute(select(EmailLog).where(EmailLog.open_token == token))
    email_log = result.scalar_one_or_none()
    if email_log is None:
        try:
            log_id = int(token)
            result = await db.execute(select(EmailLog).where(EmailLog.id == log_id))
            email_log = result.scalar_one_or_none()
        except (ValueError, TypeError):
            pass
    return email_log


async def record_email_open(db: AsyncSession, token: str, ip: str | None) -> bool:
    """Persist open event if applicable. Returns True if an open was recorded."""
    email_log = await resolve_email_log_for_open(db, token)
    if not email_log:
        return False
    if await is_known_ip(db, ip):
        log.debug("open: skipping known IP %s for log_id=%s", ip, email_log.id)
        return False
    if not email_log.opened:
        email_log.opened = True
    db.add(
        EmailOpen(
            email_log_id=email_log.id,
            ip_address=ip,
            opened_at=time_provider.utcnow(),
        )
    )
    await db.commit()
    await fire_webhook_event(
        db,
        "email.opened",
        {
            "email_log_id": email_log.id,
            "lead_id": email_log.lead_id,
            "campaign_id": email_log.campaign_id,
            "ip_address": ip,
            "timestamp": time_provider.utcnow().isoformat() + "Z",
        },
    )
    return True


async def resolve_click_redirect_url(db: AsyncSession, token: str) -> str | None:
    result = await db.execute(select(TrackedLink).where(TrackedLink.token == token))
    tracked = result.scalar_one_or_none()
    if not tracked:
        return None
    return tracked.original_url


async def record_email_click(db: AsyncSession, token: str, ip: str | None) -> bool:
    """Record click when TrackedLink exists and rules allow. Returns True if recorded."""
    result = await db.execute(select(TrackedLink).where(TrackedLink.token == token))
    tracked = result.scalar_one_or_none()
    if not tracked:
        return False
    email_log_result = await db.execute(select(EmailLog).where(EmailLog.id == tracked.email_log_id))
    email_log = email_log_result.scalar_one_or_none()
    if not email_log:
        return False
    if await is_known_ip(db, ip):
        log.debug("click: skipping known IP %s for log_id=%s", ip, email_log.id)
        return False
    if not email_log.clicked:
        email_log.clicked = True
    db.add(
        EmailClick(
            email_log_id=tracked.email_log_id,
            ip_address=ip,
            clicked_at=time_provider.utcnow(),
        )
    )
    await db.commit()
    await fire_webhook_event(
        db,
        "email.clicked",
        {
            "email_log_id": tracked.email_log_id,
            "lead_id": email_log.lead_id,
            "campaign_id": email_log.campaign_id,
            "original_url": tracked.original_url,
            "ip_address": ip,
            "timestamp": time_provider.utcnow().isoformat() + "Z",
        },
    )
    return True


async def _blocklist_unsubscribed(db: AsyncSession, email: str | None) -> None:
    """Add *email* to the blocklist unless already there (exact address only)."""
    from app.blocklist import normalize

    value = normalize(email)
    if not value or "@" not in value:
        return
    exists = await db.execute(select(BlocklistEntry.id).where(BlocklistEntry.value == value).limit(1))
    if exists.first() is not None:
        return
    try:
        async with db.begin_nested():
            db.add(BlocklistEntry(value=value, kind="email", reason="unsubscribed"))
    except IntegrityError:
        pass  # concurrent unsubscribe already inserted it


def _page(title: str, body: str, form: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="robots" content="noindex">
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           display: flex; align-items: center; justify-content: center;
           min-height: 100vh; margin: 0; background: #f9fafb; }}
    .card {{ background: #fff; border-radius: 12px; padding: 48px 40px;
            box-shadow: 0 4px 24px rgba(0,0,0,.08); text-align: center; max-width: 420px; }}
    button {{ margin-top: 16px; padding: 12px 24px; border: 0; border-radius: 8px;
             background: #111827; color: #fff; font-size: 16px; cursor: pointer; }}
  </style>
</head>
<body><div class="card"><h1>{title}</h1><p>{body}</p>{form}</div></body>
</html>"""


def unsubscribe_confirm_html(token: str) -> str:
    form = (
        f'<form method="post" action="/u/{html_escape(token, quote=True)}">'
        '<input type="hidden" name="confirm" value="1">'
        '<button type="submit">Abmeldung bestätigen</button></form>'
    )
    return _page("Abmelden", "Möchten Sie keine weiteren E-Mails erhalten?", form)


UNSUBSCRIBED_DE_HTML = _page(
    "Abgemeldet", "Sie wurden abgemeldet und erhalten keine weiteren E-Mails von uns."
)
ALREADY_UNSUBSCRIBED_DE_HTML = _page(
    "Bereits abgemeldet", "Sie sind bereits abgemeldet. Es sind keine weiteren Schritte nötig."
)
INVALID_UNSUBSCRIBE_DE_HTML = _page(
    "Ungültiger Link", "Dieser Abmeldelink ist ungültig oder abgelaufen."
)


async def peek_unsubscribe(db: AsyncSession, token: str):
    """Read-only lookup: return (token_row | None, already_unsubscribed)."""
    row = (
        await db.execute(select(LeadUnsubscribeToken).where(LeadUnsubscribeToken.token == token))
    ).scalar_one_or_none()
    if not row:
        return None, False
    cl = (
        await db.execute(
            select(CampaignLead).where(
                CampaignLead.lead_id == row.lead_id,
                CampaignLead.campaign_id == row.campaign_id,
            )
        )
    ).scalar_one_or_none()
    return row, (cl is None or cl.enrollment_status == "unsubscribed")


def _record_unsub_event(db, row, *, method, action, ip, user_agent, one_click) -> None:
    db.add(
        UnsubscribeEvent(
            token_id=getattr(row, "id", None),
            lead_id=row.lead_id,
            campaign_id=row.campaign_id,
            created_at=time_provider.utcnow(),
            method=method,
            action=action,
            ip=ip,
            user_agent=(user_agent or "")[:512] or None,
            one_click=bool(one_click),
        )
    )
    log.info(
        "unsubscribe method=%s action=%s ip=%s ua=%s lead_id=%s campaign_id=%s",
        method, action, ip, (user_agent or "")[:200], row.lead_id, row.campaign_id,
    )


async def log_unsubscribe_view(db: AsyncSession, row, *, ip=None, user_agent=None) -> None:
    _record_unsub_event(db, row, method="GET", action="view", ip=ip,
                        user_agent=user_agent, one_click=False)
    await db.commit()


async def process_unsubscribe(
    db: AsyncSession,
    token: str,
    *,
    method: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    one_click: bool = False,
    german: bool = False,
) -> tuple[str, int]:
    """Run unsubscribe flow; return (html_body, http_status).

    When *method* is given, the attempt is logged to ``unsubscribe_event``.
    """
    result = await db.execute(select(LeadUnsubscribeToken).where(LeadUnsubscribeToken.token == token))
    row = result.scalar_one_or_none()
    if not row:
        return (INVALID_UNSUBSCRIBE_DE_HTML if german else INVALID_UNSUBSCRIBE_HTML), 404

    lead_res = await db.execute(select(Lead).where(Lead.id == row.lead_id))
    lead = lead_res.scalar_one_or_none()

    already_done = False
    cl_res = await db.execute(
        select(CampaignLead).where(
            CampaignLead.lead_id == row.lead_id,
            CampaignLead.campaign_id == row.campaign_id,
        )
    )
    cl = cl_res.scalar_one_or_none()
    if not cl:
        already_done = True
    else:
        if cl.enrollment_status == "unsubscribed":
            already_done = True
        else:
            cl.enrollment_status = "unsubscribed"
            cl.interest_status = None
            log.info(
                "Unsubscribed lead_id=%s via campaign_id=%s token=%s",
                row.lead_id,
                row.campaign_id,
                token,
            )
        await db.execute(delete(QueueSlot).where(QueueSlot.campaign_lead_id == cl.id))

    if lead is not None:
        # Global opt-out: lead status + blocklist entry (idempotent), so no
        # other campaign can mail this address again.
        if lead.status not in ("unsubscribed", "bounced", "invalid"):
            lead.status = "unsubscribed"
        await _blocklist_unsubscribed(db, lead.email)

    if method:
        _record_unsub_event(
            db, row, method=method, action="already" if already_done else "unsubscribed",
            ip=ip, user_agent=user_agent, one_click=one_click,
        )
    await db.commit()

    if not already_done and lead and cl:
        await fire_webhook_event(
            db,
            "lead.unsubscribed",
            {
                "lead_id": row.lead_id,
                "lead_email": lead.email if lead else "",
                "campaign_id": row.campaign_id,
                "timestamp": time_provider.utcnow().isoformat() + "Z",
            },
        )
        await fire_webhook_event(
            db,
            "lead.status_changed",
            {
                "lead_id": row.lead_id,
                "lead_email": lead.email if lead else "",
                "campaign_id": row.campaign_id,
                "old_enrollment_status": "active",
                "new_enrollment_status": "unsubscribed",
                "timestamp": time_provider.utcnow().isoformat() + "Z",
            },
        )

    if german:
        return (ALREADY_UNSUBSCRIBED_DE_HTML if already_done else UNSUBSCRIBED_DE_HTML), 200
    html = ALREADY_UNSUBSCRIBED_HTML if already_done else _UNSUBSCRIBE_HTML
    return html, 200
