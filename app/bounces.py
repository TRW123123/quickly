"""Bounce and complaint processing.

* ``classify_bounce`` sorts a delivery failure into ``hard`` (5.x.x, user
  unknown, permanent), ``soft`` (4.x.x, mailbox full, temporary) or ``delay``
  (a "still trying" DSN, not a failure).
* ``record_bounce`` stores a ``bounce_event`` row and applies the blocklist
  policy: a hard bounce blocklists the address immediately, the third soft
  bounce of an address blocklists it as well.
* ``record_complaint`` / ``parse_complaint_report`` handle ARF abuse reports
  (``Feedback-Type: abuse``) that arrive in a synced inbox.  Gmail and most
  providers send no ARF to the sender, so this source is usually empty; the
  complaint rate then stays 0 until an FBL feeds reports into a synced inbox.

Callers are the send job (rejections at send time) and the Unibox sync
(mailer-daemon NDRs).  Nothing here sends mail.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import time as time_provider

log = logging.getLogger("quickly.bounces")

SOFT_BOUNCE_BLOCK_THRESHOLD = 3
# A re-synced mailbox message (e.g. Office 365 delta after a read-flag change)
# must not count twice: identical DSN/ARF events inside this window are dropped.
DUPLICATE_WINDOW = timedelta(hours=12)

_DELAY_RE = re.compile(
    r"action:\s*delayed|delivery (has been |is )?delayed|delay(ed)? (notification|warning)"
    r"|will (continue to )?(retry|try)|still (trying|be retried)|verz(ö|oe)gert",
    re.IGNORECASE,
)
_FULL_RE = re.compile(
    r"mailbox (is )?full|over ?quota|quota exceeded|exceeded (the |its )?(storage|quota)"
    r"|insufficient (system )?storage|postfach (ist )?voll|speicher(platz)? (ist )?(voll|ersch(ö|oe)pft)",
    re.IGNORECASE,
)
_ENHANCED_RE = re.compile(r"(?<![\d.])([245])\.\d{1,3}\.\d{1,3}(?![\d.])")
_HARD_RE = re.compile(
    r"user unknown|unknown user|no such (user|mailbox|recipient|address)"
    r"|does not exist|doesn't exist|address (rejected|not found|unknown)"
    r"|recipient (address )?(rejected|unknown|not found)|invalid (recipient|address|mailbox)"
    r"|mailbox (unavailable|not found|does not exist|disabled)|account (disabled|has been disabled)"
    r"|user not found|resource not found|unbekannt|existiert nicht|ung(ü|ue)ltige (adresse|empf)",
    re.IGNORECASE,
)
_SOFT_RE = re.compile(
    r"temporar|try again later|greylist|rate limit|too many (connections|messages)"
    r"|vor(ü|ue)bergehend|sp(ä|ae)ter erneut",
    re.IGNORECASE,
)
_SMTP_CODE_RE = re.compile(r"(?<!\d)(4[2-5]\d|5[0-5]\d)(?!\d)")


def classify_bounce(text: str, default: str = "hard") -> str:
    """``hard``, ``soft`` or ``delay`` for a DSN body or SMTP error message."""
    t = text or ""
    if _DELAY_RE.search(t) and not re.search(r"action:\s*failed", t, re.IGNORECASE):
        return "delay"
    if _FULL_RE.search(t):
        return "soft"
    m = _ENHANCED_RE.search(t)
    if m:
        return "hard" if m.group(1) == "5" else "soft" if m.group(1) == "4" else default
    if _HARD_RE.search(t):
        return "hard"
    if _SOFT_RE.search(t):
        return "soft"
    m = _SMTP_CODE_RE.search(t)
    if m:
        return "hard" if m.group(1).startswith("5") else "soft"
    return default


async def _is_duplicate(
    db: AsyncSession, addr: str, inbox_id, kind: str, source: str, reason: str, now: datetime
) -> bool:
    from app.models import BounceEvent

    if source == "send":
        return False  # every send attempt is a distinct event
    res = await db.execute(
        select(BounceEvent.id).where(
            BounceEvent.email == addr, BounceEvent.kind == kind, BounceEvent.source == source,
            BounceEvent.inbox_id == inbox_id if inbox_id is not None else BounceEvent.inbox_id.is_(None),
            BounceEvent.reason == reason if reason else BounceEvent.reason.is_(None),
            BounceEvent.created_at >= now - DUPLICATE_WINDOW,
        ).limit(1)
    )
    return res.first() is not None


async def soft_bounce_count(db: AsyncSession, email: str) -> int:
    from app.models import BounceEvent

    return (await db.execute(
        select(func.count(BounceEvent.id)).where(
            BounceEvent.email == (email or "").strip().lower(), BounceEvent.kind == "soft",
        )
    )).scalar() or 0


async def record_bounce(
    db: AsyncSession,
    email: str,
    kind: str,
    *,
    inbox_id: Optional[int],
    source: str,
    reason: str = "",
    now: Optional[datetime] = None,
) -> dict:
    """Store one hard/soft bounce and apply the blocklist policy.

    Returns ``{"kind", "blocklisted", "soft_count"}``.  The caller commits.
    """
    from app.blocklist import add_email
    from app.models import BounceEvent

    addr = (email or "").strip().lower()
    out = {"kind": kind, "blocklisted": False, "soft_count": 0}
    if kind not in ("hard", "soft") or not addr:
        return out
    now = now or time_provider.utcnow()
    reason = (reason or "")[:1000]
    if await _is_duplicate(db, addr[:255], inbox_id, kind, source, reason, now):
        out["duplicate"] = True
        return out
    db.add(BounceEvent(
        email=addr[:255], inbox_id=inbox_id, kind=kind, source=source,
        reason=reason or None, created_at=now,
    ))
    await db.flush()
    if kind == "hard":
        out["blocklisted"] = await add_email(db, addr, f"hard bounce ({source}): {(reason or '')[:200]}")
    else:
        out["soft_count"] = await soft_bounce_count(db, addr)
        if out["soft_count"] >= SOFT_BOUNCE_BLOCK_THRESHOLD:
            out["blocklisted"] = await add_email(
                db, addr, f"{out['soft_count']} soft bounces, last: {(reason or '')[:200]}"
            )
    if out["blocklisted"]:
        log.info("Blocklisted %s after %s bounce (%s)", addr, kind, source)
    return out


async def record_send_rejection(
    db: AsyncSession, email: str, inbox_id: Optional[int], message: str
) -> dict:
    """Bounce bookkeeping for a permanent SendFailure (bounce / invalid_recipient).

    Never raises: bookkeeping must not break the send job.
    """
    try:
        return await record_bounce(
            db, email, classify_bounce(message, default="hard"),
            inbox_id=inbox_id, source="send", reason=message,
        )
    except Exception:
        log.exception("Bounce bookkeeping failed for %s", email)
        return {"kind": None, "blocklisted": False, "soft_count": 0}


async def record_dsn_bounce(
    db: AsyncSession, email: str, inbox_id: Optional[int], text: str
) -> str:
    """Classify and store an NDR from mailer-daemon; returns hard/soft/delay.

    Only ``hard`` should mark the lead bounced; a soft bounce is counted, a
    delay notice is ignored.  Never raises (falls back to ``hard``).
    """
    try:
        kind = classify_bounce(text, default="hard")
        if kind != "delay":
            await record_bounce(db, email, kind, inbox_id=inbox_id, source="dsn", reason=text[:1000])
        return kind
    except Exception:
        log.exception("DSN bookkeeping failed for %s", email)
        return "hard"


# ── Complaints (ARF / FBL) ──────────────────────────────────────────────────

_ARF_RE = re.compile(r"feedback-type:\s*abuse|report-type=\"?feedback-report", re.IGNORECASE)
_ARF_RCPT_RE = re.compile(
    r"(?:Original-Rcpt-To|Removal-Recipient)\s*:\s*(?:rfc822\s*;\s*)?<?([^\s<>;,]+@[^\s<>;,]+)>?",
    re.IGNORECASE,
)


def parse_complaint_report(text: str) -> Optional[str]:
    """Complaining address of an ARF abuse report ('' when unknown), else None."""
    if not text or not _ARF_RE.search(text):
        return None
    m = _ARF_RCPT_RE.search(text)
    return m.group(1).strip().lower() if m else ""


async def record_complaint(
    db: AsyncSession, email: str, inbox_id: Optional[int], reason: str = ""
) -> None:
    """Store a spam complaint and blocklist the complaining address."""
    from app.blocklist import add_email
    from app.models import BounceEvent

    addr = (email or "").strip().lower()[:255]
    now = time_provider.utcnow()
    reason = (reason or "")[:1000]
    if await _is_duplicate(db, addr, inbox_id, "complaint", "arf", reason, now):
        return
    db.add(BounceEvent(
        email=addr, inbox_id=inbox_id, kind="complaint", source="arf",
        reason=reason or None, created_at=now,
    ))
    await db.flush()
    if addr:
        await add_email(db, addr, "spam complaint (ARF)")
    log.warning("Spam complaint recorded for %s (inbox_id=%s)", addr or "?", inbox_id)


async def maybe_record_complaint(
    db: AsyncSession, inbox_id: Optional[int], text: str
) -> bool:
    """Hook for inbox sync: record an ARF abuse report if *text* is one. Never raises."""
    try:
        addr = parse_complaint_report(text)
        if addr is None:
            return False
        await record_complaint(db, addr, inbox_id, text[:1000])
        return True
    except Exception:
        log.exception("Complaint bookkeeping failed (inbox_id=%s)", inbox_id)
        return False
