"""Weekly warm-up, inbox rotation, deliverability auto-stop and bounce policy.

No mail is sent: the send job runs against a fake ``send_email``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

import app.jobs as jobs_mod
from app import time as time_provider
from app.bounces import (
    classify_bounce,
    maybe_record_complaint,
    parse_complaint_report,
    record_bounce,
    record_dsn_bounce,
    soft_bounce_count,
)
from app.blocklist import is_blocked
from app.models import BounceEvent, CampaignLead, Inbox, QueueSlot
from app.queue_logic import compute_effective_daily_limit, reserve_slots_for_new_leads_bulk
from app.sender import SendFailure
from app.warmup import (
    auto_pause_reason,
    check_auto_pause,
    inbox_health,
    warmup_schedule,
    warmup_week,
)
from tests.conftest import (
    make_campaign,
    make_campaign_inbox,
    make_campaign_lead,
    make_email_log,
    make_inbox,
    make_lead,
    make_sequence,
)
from tests.test_send_compliance import _setup, sent  # noqa: F401  (fixture re-export)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "QUICKLY_WARMUP_SCHEDULE", "QUICKLY_MAX_PER_INBOX_PER_DAY", "QUICKLY_HEALTH_WINDOW_DAYS",
        "QUICKLY_HEALTH_MIN_SENDS", "QUICKLY_MAX_BOUNCE_RATE", "QUICKLY_MAX_COMPLAINT_RATE",
    ):
        monkeypatch.delenv(name, raising=False)


def _inbox(days_ago: int, *, max_per_day: int = 25, paused_days_ago: int | None = None, mode="weekly"):
    now = datetime.utcnow()
    return SimpleNamespace(
        max_emails_per_day=max_per_day,
        ramp_up_enabled=True,
        ramp_up_mode=mode,
        ramp_up_started_at=now - timedelta(days=days_ago),
        created_at=now - timedelta(days=400),
        paused=paused_days_ago is not None,
        ramp_up_paused_at=(now - timedelta(days=paused_days_ago)) if paused_days_ago is not None else None,
    )


# ── Weekly schedule ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("days,week,limit", [
    (0, 1, 4), (6, 1, 4), (7, 2, 9), (13, 2, 9), (14, 3, 15), (20, 3, 15),
    (21, 4, 25), (60, 9, 25),
])
def test_weekly_schedule_per_week(days, week, limit):
    inbox = _inbox(days)
    assert warmup_week(inbox) == week
    assert compute_effective_daily_limit(inbox) == limit


def test_schedule_from_env_and_clamped_to_30(monkeypatch):
    monkeypatch.setenv("QUICKLY_WARMUP_SCHEDULE", "3,8,15,40")
    assert warmup_schedule() == (3, 8, 15, 30)
    assert compute_effective_daily_limit(_inbox(30, max_per_day=30)) == 30
    monkeypatch.setenv("QUICKLY_WARMUP_SCHEDULE", "kaputt")
    assert warmup_schedule() == (4, 9, 15, 25)


def test_inbox_cap_and_fallback_to_created_at():
    assert compute_effective_daily_limit(_inbox(30, max_per_day=12)) == 12
    inbox = _inbox(0)
    inbox.ramp_up_started_at = None
    inbox.created_at = datetime.utcnow() - timedelta(days=8)
    assert warmup_week(inbox) == 2 and compute_effective_daily_limit(inbox) == 9


def test_pause_freezes_weekly_progress():
    # Ramp started 30 days ago, paused 25 days ago → frozen in week 1.
    inbox = _inbox(30, paused_days_ago=25)
    assert warmup_week(inbox) == 1
    assert compute_effective_daily_limit(inbox) == 4


def test_linear_mode_and_disabled_ramp_unchanged():
    inbox = _inbox(3, mode="linear")
    inbox.ramp_up_start, inbox.ramp_up_step_size = 1, 1
    assert compute_effective_daily_limit(inbox) == 4  # 1 + 3*1
    assert warmup_week(inbox) is None
    off = _inbox(0)
    off.ramp_up_enabled = False
    assert compute_effective_daily_limit(off) == 25
    assert warmup_week(off) is None


def test_global_cap_default_30_and_override(monkeypatch):
    off = _inbox(0, max_per_day=50)
    off.ramp_up_enabled = False
    assert compute_effective_daily_limit(off) == 30
    monkeypatch.setenv("QUICKLY_MAX_PER_INBOX_PER_DAY", "10")
    assert compute_effective_daily_limit(_inbox(21)) == 10
    assert compute_effective_daily_limit(_inbox(0)) == 4


@pytest.mark.asyncio
async def test_new_inbox_defaults_to_weekly_mode(session):
    inbox = Inbox(email="new@test.com")
    session.add(inbox)
    await session.flush()
    assert inbox.ramp_up_mode == "weekly"
    assert inbox.max_emails_per_day == 25
    assert inbox.warmup_week == 1
    assert compute_effective_daily_limit(inbox) == 4


# ── Rotation across a campaign's inboxes ────────────────────────────────────

@pytest.mark.asyncio
async def test_rotation_distributes_campaign_across_two_inboxes(session):
    a = await make_inbox(session, email="a@test.com")
    b = await make_inbox(session, email="b@test.com")
    for i in (a, b):
        i.ramp_up_enabled = False
        i.max_emails_per_day = 30
    campaign = await make_campaign(session)
    await make_sequence(session, campaign.id, position=0)
    await make_campaign_inbox(session, campaign.id, a.id, position=0)
    await make_campaign_inbox(session, campaign.id, b.id, position=1)
    cl_ids = []
    for n in range(8):
        lead = await make_lead(session, email=f"l{n}@example.com")
        cl_ids.append((await make_campaign_lead(session, campaign.id, lead.id)).id)
    await reserve_slots_for_new_leads_bulk(session, cl_ids, campaign.id, start_date=date(2030, 3, 4))
    await session.flush()
    rows = (await session.execute(
        select(QueueSlot.inbox_id, func.count(QueueSlot.id)).group_by(QueueSlot.inbox_id)
    )).all()
    per_inbox = dict(rows)
    assert per_inbox.get(a.id) == 4 and per_inbox.get(b.id) == 4


@pytest.mark.asyncio
async def test_rotation_spills_over_when_warmup_limit_is_reached(session):
    # Both inboxes in warm-up week 1 (4/day): 8 leads fill one day, 4 per inbox.
    a = await make_inbox(session, email="wa@test.com")
    b = await make_inbox(session, email="wb@test.com")
    campaign = await make_campaign(session)
    await make_sequence(session, campaign.id, position=0)
    await make_campaign_inbox(session, campaign.id, a.id, position=0)
    await make_campaign_inbox(session, campaign.id, b.id, position=1)
    cl_ids = []
    for n in range(10):
        lead = await make_lead(session, email=f"w{n}@example.com")
        cl_ids.append((await make_campaign_lead(session, campaign.id, lead.id)).id)
    start = date(2030, 3, 4)  # a Monday
    # Pin the ramp start so the target day is in week 1.
    for i in (a, b):
        i.ramp_up_started_at = datetime(2030, 3, 4)
    await reserve_slots_for_new_leads_bulk(session, cl_ids, campaign.id, start_date=start)
    await session.flush()
    rows = (await session.execute(
        select(QueueSlot.inbox_id, func.count(QueueSlot.id))
        .where(func.date(QueueSlot.scheduled_date) == start.isoformat())
        .group_by(QueueSlot.inbox_id)
    )).all()
    assert dict(rows) == {a.id: 4, b.id: 4}


# ── Auto-stop ───────────────────────────────────────────────────────────────

async def _inbox_with_sends(session, n_sends: int, email="health@test.com"):
    inbox = await make_inbox(session, email=email)
    campaign = await make_campaign(session)
    lead = await make_lead(session, email=f"x-{email}")
    for _ in range(n_sends):
        await make_email_log(session, lead.id, campaign.id, inbox_id=inbox.id,
                             sent_at=time_provider.utcnow() - timedelta(days=1))
    return inbox


async def _hard_bounces(session, inbox, n, source="dsn"):
    for k in range(n):
        await record_bounce(session, f"gone{k}-{inbox.id}@example.com", "hard",
                            inbox_id=inbox.id, source=source, reason=f"550 5.1.1 #{k}")


@pytest.mark.asyncio
async def test_auto_stop_below_threshold_keeps_inbox_active(session):
    inbox = await _inbox_with_sends(session, 100)
    await _hard_bounces(session, inbox, 3)  # 3 % is not > 3 %
    assert await check_auto_pause(session) == []
    assert inbox.paused is False and inbox.auto_paused_at is None


@pytest.mark.asyncio
async def test_auto_stop_above_bounce_threshold_pauses_and_freezes(session, monkeypatch):
    events = []

    async def fake_webhook(db, event, data):
        events.append((event, data))

    monkeypatch.setattr("app.webhooks.fire_webhook_event", fake_webhook)
    inbox = await _inbox_with_sends(session, 100)
    await _hard_bounces(session, inbox, 4)
    paused = await check_auto_pause(session)
    assert [p["inbox_id"] for p in paused] == [inbox.id]
    assert inbox.paused is True
    assert inbox.auto_paused_at is not None and inbox.ramp_up_paused_at is not None
    assert "bounce rate 4.0 %" in inbox.auto_pause_reason
    assert events and events[0][0] == "inbox.auto_paused"


@pytest.mark.asyncio
async def test_auto_stop_needs_min_sample(session):
    inbox = await _inbox_with_sends(session, 10)
    await _hard_bounces(session, inbox, 5)  # 50 % but only 10 sends
    assert await check_auto_pause(session) == []
    assert inbox.paused is False


@pytest.mark.asyncio
async def test_send_time_rejections_count_as_sends(session):
    inbox = await _inbox_with_sends(session, 18)
    await _hard_bounces(session, inbox, 2, source="send")
    health = await inbox_health(session, inbox)
    assert health["sends"] == 20 and health["hard_bounces"] == 2
    assert auto_pause_reason(health) is not None


@pytest.mark.asyncio
async def test_auto_stop_complaint_threshold(session):
    inbox = await _inbox_with_sends(session, 300)
    arf = "Feedback-Type: abuse\nOriginal-Rcpt-To: angry@example.com\n"
    assert await maybe_record_complaint(session, inbox.id, arf) is True
    health = await inbox_health(session, inbox)
    assert health["complaints"] == 1
    assert auto_pause_reason(health) is not None  # 0.33 % > 0.3 %
    assert await is_blocked(session, "angry@example.com")


@pytest.mark.asyncio
async def test_manual_resume_clears_auto_pause_and_restarts_window(session):
    from app.routers.inbox import _clear_auto_pause

    inbox = await _inbox_with_sends(session, 100)
    await _hard_bounces(session, inbox, 10)
    inbox.paused = True
    inbox.auto_paused_at = time_provider.utcnow()
    inbox.auto_pause_reason = "bounce rate"
    _clear_auto_pause(inbox)
    inbox.paused = False
    assert inbox.auto_paused_at is None and inbox.auto_pause_reason is None
    health = await inbox_health(session, inbox)
    assert health["sends"] == 0 and auto_pause_reason(health) is None


# ── Bounce policy ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,kind", [
    ("Final-Recipient: rfc822; a@x.de\nAction: failed\nStatus: 5.1.1", "hard"),
    ("550 5.1.1 <a@x.de>: Recipient address rejected: User unknown", "hard"),
    ("The email account that you tried to reach does not exist.", "hard"),
    ("452 4.2.2 The email account that you tried to reach is over quota", "soft"),
    ("552 5.2.2 Mailbox full", "soft"),
    ("Status: 4.4.1\nAction: failed\nConnection timed out", "soft"),
    ("421 Temporary failure, please try again later", "soft"),
    ("Delivery Status Notification (Delay)\nAction: delayed\nWill retry for 2 days", "delay"),
    ("Gmail rejected the message (400): bad request", "hard"),
])
def test_classify_bounce(text, kind):
    assert classify_bounce(text) == kind


@pytest.mark.asyncio
async def test_hard_bounce_blocklists_immediately(session):
    res = await record_bounce(session, "Gone@Example.com", "hard", inbox_id=None,
                              source="dsn", reason="550 5.1.1 user unknown")
    assert res["blocklisted"] is True
    assert await is_blocked(session, "gone@example.com")


@pytest.mark.asyncio
async def test_three_soft_bounces_blocklist(session):
    for k in range(2):
        res = await record_bounce(session, "full@example.com", "soft", inbox_id=None,
                                  source="dsn", reason=f"452 4.2.2 mailbox full #{k}")
        assert res["blocklisted"] is False
    assert not await is_blocked(session, "full@example.com")
    res = await record_bounce(session, "full@example.com", "soft", inbox_id=None,
                              source="dsn", reason="452 4.2.2 mailbox full #3")
    assert res["soft_count"] == 3 and res["blocklisted"] is True
    assert await is_blocked(session, "full@example.com")


@pytest.mark.asyncio
async def test_duplicate_dsn_is_counted_once(session):
    text = "Status: 4.2.2\nmailbox full"
    for _ in range(3):
        await record_dsn_bounce(session, "dup@example.com", None, text)
    assert await soft_bounce_count(session, "dup@example.com") == 1
    assert not await is_blocked(session, "dup@example.com")


@pytest.mark.asyncio
async def test_delay_dsn_is_ignored(session):
    kind = await record_dsn_bounce(session, "slow@example.com", None,
                                   "Action: delayed\nStatus: 4.4.7\nwill retry")
    assert kind == "delay"
    n = (await session.execute(select(func.count(BounceEvent.id)))).scalar()
    assert n == 0


def test_parse_complaint_report():
    assert parse_complaint_report("Hello, thanks for your mail") is None
    assert parse_complaint_report("Feedback-Type: abuse\nOriginal-Rcpt-To: <A@b.de>") == "a@b.de"


@pytest.mark.asyncio
async def test_send_job_hard_rejection_blocklists_recipient(session, sent, monkeypatch):
    inbox, campaign, lead, cl, slot = await _setup(session, lead_email="nobody@example.com")

    def failing_send(**kwargs):
        return SendFailure(error_type="invalid_recipient",
                           message="SMTP recipient refused: {'nobody@example.com': (550, b'5.1.1 User unknown')}")

    monkeypatch.setattr(jobs_mod, "send_email", failing_send)
    await jobs_mod.send_slot_job(slot.id)
    await session.refresh(cl)
    assert cl.enrollment_status == "bounced"
    assert await is_blocked(session, "nobody@example.com")
    ev = (await session.execute(select(BounceEvent))).scalars().all()
    assert [(e.kind, e.source, e.inbox_id) for e in ev] == [("hard", "send", inbox.id)]
