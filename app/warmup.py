"""Weekly warm-up schedule and per-inbox deliverability auto-stop.

Warm-up ("weekly" ramp mode, default for new inboxes and for every existing
inbox that still has ramp-up enabled):

    week 1: 4/day, week 2: 9/day, week 3: 15/day, week 4+: 25/day

The schedule is configurable via ``QUICKLY_WARMUP_SCHEDULE`` (comma separated,
one value per week, the last value is the plateau; each value is clamped to
``MAX_WARMUP_PER_DAY`` = 30).  Weeks count from ``ramp_up_started_at``
(fallback ``created_at``) and freeze while the inbox is paused, exactly like
the legacy linear ramp.  The inbox's own ``max_emails_per_day`` and the global
``QUICKLY_MAX_PER_INBOX_PER_DAY`` still cap the result.

Auto-stop: over a rolling window (``QUICKLY_HEALTH_WINDOW_DAYS``, default 14)
an inbox with at least ``QUICKLY_HEALTH_MIN_SENDS`` (default 20) sends is
paused when its hard-bounce rate exceeds ``QUICKLY_MAX_BOUNCE_RATE`` (default
0.03) or its complaint rate exceeds ``QUICKLY_MAX_COMPLAINT_RATE`` (default
0.003).  A manual resume (PATCH ``paused=false``) clears the auto-pause and
restarts the window at the resume time.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import time as time_provider

log = logging.getLogger("quickly.warmup")

DEFAULT_WARMUP_SCHEDULE: tuple[int, ...] = (4, 9, 15, 25)
MAX_WARMUP_PER_DAY = 30

DEFAULT_HEALTH_WINDOW_DAYS = 14
DEFAULT_HEALTH_MIN_SENDS = 20
DEFAULT_MAX_BOUNCE_RATE = 0.03
DEFAULT_MAX_COMPLAINT_RATE = 0.003
AUTO_PAUSE_CHECK_INTERVAL_MINUTES = 15

RAMP_MODES = ("weekly", "linear")


# ── Schedule ────────────────────────────────────────────────────────────────

def warmup_schedule() -> tuple[int, ...]:
    """Per-week daily limits from ``QUICKLY_WARMUP_SCHEDULE`` (default 4,9,15,25)."""
    raw = os.getenv("QUICKLY_WARMUP_SCHEDULE", "").strip()
    if not raw:
        return DEFAULT_WARMUP_SCHEDULE
    try:
        values = tuple(int(p.strip()) for p in raw.split(",") if p.strip())
    except ValueError:
        values = ()
    if not values or any(v < 1 for v in values):
        log.warning(
            "QUICKLY_WARMUP_SCHEDULE=%r is invalid; using %s",
            raw, ",".join(map(str, DEFAULT_WARMUP_SCHEDULE)),
        )
        return DEFAULT_WARMUP_SCHEDULE
    return tuple(min(v, MAX_WARMUP_PER_DAY) for v in values)


def ramp_mode(inbox) -> str:
    """``weekly`` or ``linear``.

    Objects without the attribute (legacy duck-typed callers) keep the linear
    curve; a real Inbox whose column is still unset (not flushed yet) gets the
    column default ``weekly``.
    """
    mode = getattr(inbox, "ramp_up_mode", "linear") or "weekly"
    return mode if mode in RAMP_MODES else "weekly"


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def ramp_days_elapsed(inbox, for_date: Optional[date] = None) -> int:
    """Whole days since the ramp started, frozen at the pause date while paused."""
    ref = for_date or datetime.utcnow().date()
    paused_at = getattr(inbox, "ramp_up_paused_at", None)
    if getattr(inbox, "paused", False) and paused_at is not None:
        ref = _as_date(paused_at)
    started = _as_date(getattr(inbox, "ramp_up_started_at", None)) or _as_date(
        getattr(inbox, "created_at", None)
    ) or ref
    return max(0, (ref - started).days)


def warmup_week(inbox, for_date: Optional[date] = None) -> Optional[int]:
    """1-based warm-up week for weekly-mode inboxes with ramp-up on, else None."""
    if not getattr(inbox, "ramp_up_enabled", False) or ramp_mode(inbox) != "weekly":
        return None
    return ramp_days_elapsed(inbox, for_date) // 7 + 1


def weekly_daily_limit(inbox, for_date: Optional[date] = None) -> int:
    """Schedule value for the inbox's current week (no caps applied)."""
    schedule = warmup_schedule()
    week = ramp_days_elapsed(inbox, for_date) // 7
    return schedule[min(week, len(schedule) - 1)]


# ── Auto-stop ───────────────────────────────────────────────────────────────

def _env_number(name: str, default, cast):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        log.warning("%s=%r is invalid; using %s", name, raw, default)
        return default
    return value if value >= 0 else default


def health_thresholds() -> dict:
    return {
        "window_days": _env_number("QUICKLY_HEALTH_WINDOW_DAYS", DEFAULT_HEALTH_WINDOW_DAYS, int) or DEFAULT_HEALTH_WINDOW_DAYS,
        "min_sends": _env_number("QUICKLY_HEALTH_MIN_SENDS", DEFAULT_HEALTH_MIN_SENDS, int),
        "max_bounce_rate": _env_number("QUICKLY_MAX_BOUNCE_RATE", DEFAULT_MAX_BOUNCE_RATE, float),
        "max_complaint_rate": _env_number("QUICKLY_MAX_COMPLAINT_RATE", DEFAULT_MAX_COMPLAINT_RATE, float),
    }


async def inbox_health(db: AsyncSession, inbox, now: Optional[datetime] = None) -> dict:
    """Sends, hard bounces and complaints of *inbox* in the rolling window.

    Sends = EmailLog rows of the inbox plus send-time rejections (whose
    EmailLog row is deleted by the send job).  Only hard bounces count toward
    the bounce rate; soft bounces are temporary and escalate via the blocklist.
    """
    from app.models import BounceEvent, EmailLog

    th = health_thresholds()
    now = now or time_provider.utcnow()
    window_start = now - timedelta(days=th["window_days"])
    reset_at = getattr(inbox, "auto_pause_reset_at", None)
    if reset_at is not None and reset_at > window_start:
        window_start = reset_at

    logged = (await db.execute(
        select(func.count(EmailLog.id)).where(
            EmailLog.inbox_id == inbox.id, EmailLog.sent_at >= window_start,
        )
    )).scalar() or 0
    rows = (await db.execute(
        select(BounceEvent.kind, BounceEvent.source, func.count(BounceEvent.id))
        .where(BounceEvent.inbox_id == inbox.id, BounceEvent.created_at >= window_start)
        .group_by(BounceEvent.kind, BounceEvent.source)
    )).all()
    hard = sum(n for kind, _src, n in rows if kind == "hard")
    complaints = sum(n for kind, _src, n in rows if kind == "complaint")
    rejected_at_send = sum(n for _kind, src, n in rows if src == "send" and _kind in ("hard", "soft"))
    sends = logged + rejected_at_send
    return {
        "window_start": window_start,
        "sends": sends,
        "hard_bounces": hard,
        "complaints": complaints,
        "bounce_rate": (hard / sends) if sends else 0.0,
        "complaint_rate": (complaints / sends) if sends else 0.0,
    }


def auto_pause_reason(health: dict, thresholds: Optional[dict] = None) -> Optional[str]:
    """Reason string when *health* trips a threshold, else None."""
    th = thresholds or health_thresholds()
    if health["sends"] < th["min_sends"]:
        return None
    if health["bounce_rate"] > th["max_bounce_rate"]:
        return (
            f"bounce rate {health['bounce_rate'] * 100:.1f} % "
            f"({health['hard_bounces']}/{health['sends']} in {th['window_days']} d) "
            f"> {th['max_bounce_rate'] * 100:.1f} %"
        )
    if health["complaint_rate"] > th["max_complaint_rate"]:
        return (
            f"complaint rate {health['complaint_rate'] * 100:.2f} % "
            f"({health['complaints']}/{health['sends']} in {th['window_days']} d) "
            f"> {th['max_complaint_rate'] * 100:.2f} %"
        )
    return None


async def check_auto_pause(db: AsyncSession, now: Optional[datetime] = None) -> list[dict]:
    """Pause every active inbox whose bounce/complaint rate is above threshold.

    Returns one dict per paused inbox.  The caller commits.
    """
    from app.models import Inbox

    now = now or time_provider.utcnow()
    th = health_thresholds()
    inboxes = (await db.execute(
        select(Inbox).where(Inbox.paused == False)  # noqa: E712
    )).scalars().all()
    paused: list[dict] = []
    for inbox in inboxes:
        health = await inbox_health(db, inbox, now)
        reason = auto_pause_reason(health, th)
        if reason is None:
            continue
        inbox.paused = True
        if inbox.ramp_up_paused_at is None:
            inbox.ramp_up_paused_at = now  # freeze warm-up like a manual pause
        inbox.auto_paused_at = now
        inbox.auto_pause_reason = reason
        log.warning("Auto-stop: pausing inbox %s (id=%s): %s", inbox.email, inbox.id, reason)
        data = {
            "inbox_id": inbox.id,
            "inbox_email": inbox.email,
            "reason": reason,
            "sends": health["sends"],
            "hard_bounces": health["hard_bounces"],
            "complaints": health["complaints"],
            "timestamp": now.isoformat() + "Z",
        }
        try:
            from app.webhooks import fire_webhook_event
            await fire_webhook_event(db, "inbox.auto_paused", data)
        except Exception:
            log.exception("Auto-stop: notification failed for inbox %s", inbox.id)
        paused.append(data)
    return paused


async def run_auto_pause_job() -> None:
    """APScheduler entry point (``inbox_auto_pause``)."""
    from app import database as _database

    try:
        async with _database.AsyncSessionLocal() as db:
            paused = await check_auto_pause(db)
            await db.commit()
        if paused:
            log.warning("Auto-stop paused %d inbox(es)", len(paused))
    except Exception:
        log.exception("Auto-stop check failed")
