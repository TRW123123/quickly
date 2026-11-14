"""Compliance guards for campaign sending (feat/limits).

Covers: mandatory imprint footer + unsubscribe link, fail-closed without an
imprint, forced List-Unsubscribe header, 3-minute minimum gap per inbox,
global per-inbox daily cap, blocklist skip, and new-inbox defaults.
All tests drive ``send_slot_job`` (runs on SQLite) with a fake ``send_email``.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

import app.jobs as jobs_mod
from app import send_compliance as sc
from app import time as time_provider
from app.models import AppSetting, CampaignLead, EmailLog, Inbox, QueueSlot
from app.queue_logic import compute_effective_daily_limit
from app.sender import SendResult
from tests.conftest import (
    make_campaign,
    make_campaign_inbox,
    make_campaign_lead,
    make_email_log,
    make_inbox,
    make_lead,
    make_queue_slot,
    make_sequence,
)

FOOTER = "Test GmbH\nTeststr. 1, 12345 Teststadt\ninfo@test.example"


class _SessionCtx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return None


@pytest.fixture
def sent(monkeypatch, session):
    """Capture send_email calls and route the job to the test session."""
    calls: list[dict] = []

    def fake_send(**kwargs):
        calls.append(kwargs)
        return SendResult(message_id=f"<m{len(calls)}@test>", thread_id="t")

    async def fake_webhook(db, event, data):
        return None

    monkeypatch.setattr(jobs_mod, "send_email", fake_send)
    monkeypatch.setattr(jobs_mod, "fire_webhook_event", fake_webhook)
    monkeypatch.setattr(jobs_mod, "AsyncSessionLocal", lambda: _SessionCtx(session))
    return calls


async def _setup(session, *, body="Hallo {{name}}", is_html=False, lead_email="lead@example.com",
                 inbox_kwargs=None, campaign_kwargs=None):
    inbox = await make_inbox(session, provider="smtp", **(inbox_kwargs or {}))
    inbox.ramp_up_enabled = False  # new default is on; these tests target other limits
    from app.models import SmtpAccount
    session.add(SmtpAccount(
        inbox_id=inbox.id, smtp_host="smtp.example.com", smtp_port=587,
        smtp_username="u", smtp_password="p", smtp_use_tls=True,
    ))
    campaign = await make_campaign(
        session, sending_days=[0, 1, 2, 3, 4, 5, 6],
        sending_hours_start="00:00", sending_hours_end="23:59",
    )
    for k, v in (campaign_kwargs or {}).items():
        setattr(campaign, k, v)
    seq = await make_sequence(session, campaign.id, body=body)
    seq.is_html = is_html
    lead = await make_lead(session, email=lead_email)
    cl = await make_campaign_lead(session, campaign.id, lead.id)
    await make_campaign_inbox(session, campaign.id, inbox.id)
    slot = await make_queue_slot(
        session, cl.id, inbox.id, scheduled_date=time_provider.now() - timedelta(minutes=1)
    )
    await session.flush()
    return inbox, campaign, lead, cl, slot


async def _slot_count(session, inbox_id):
    return (await session.execute(
        select(func.count(QueueSlot.id)).where(QueueSlot.inbox_id == inbox_id)
    )).scalar()


# ── Footer ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_text_mail_gets_imprint_and_unsubscribe_footer(session, sent):
    inbox, campaign, lead, cl, slot = await _setup(
        session, campaign_kwargs={"add_unsubscribe_header": False}
    )
    await jobs_mod.send_slot_job(slot.id)

    assert len(sent) == 1
    call = sent[0]
    assert call["body"].startswith("Hallo Test Lead")
    assert "Teststr. 1, 12345 Teststadt" in call["body"]
    unsub = call["list_unsubscribe_url"]
    # Header is forced on even though the campaign toggle is off.
    assert unsub and "/u/" in unsub
    assert call["body"].rstrip().endswith(unsub)


@pytest.mark.asyncio
async def test_html_mail_with_own_unsubscribe_link_gets_only_imprint(session, sent):
    body = '<p>Hallo</p><p><a href="{{unsubscribe_link}}">Austragen</a></p>'
    _, _, _, _, slot = await _setup(session, body=body, is_html=True)
    await jobs_mod.send_slot_job(slot.id)

    assert len(sent) == 1
    html = sent[0]["body"]
    unsub = sent[0]["list_unsubscribe_url"]
    assert "quickly-footer" in html
    assert "Teststr. 1, 12345 Teststadt" in html
    assert html.count(unsub) == 1  # no second unsubscribe link appended
    assert "Abmelden" not in html


def test_html_footer_goes_before_body_close_and_text_part_carries_it():
    from app.sender import _strip_html_tags

    out = sc.append_compliance_footer(
        "<html><body><p>Hi</p></body></html>", is_html=True,
        footer_text="A GmbH\nWeg 2", unsub_url="https://x.test/u/tok", include_unsubscribe=True,
    )
    assert out.index("quickly-footer") < out.lower().rindex("</body>")
    plain = _strip_html_tags(out)
    assert "A GmbH" in plain and "Weg 2" in plain and "Abmelden" in plain


@pytest.mark.asyncio
async def test_db_footer_setting_overrides_env(session, sent):
    session.add(AppSetting(key=sc.FOOTER_SETTING_KEY, value="DB Impressum AG"))
    _, _, _, _, slot = await _setup(session)
    await jobs_mod.send_slot_job(slot.id)
    assert "DB Impressum AG" in sent[0]["body"]
    assert "Teststr. 1" not in sent[0]["body"]


@pytest.mark.asyncio
async def test_fail_closed_without_imprint(session, sent, monkeypatch, caplog):
    monkeypatch.delenv("QUICKLY_FOOTER_TEXT", raising=False)
    inbox, _, _, _, slot = await _setup(session)
    with caplog.at_level(logging.ERROR, logger="quickly.jobs"):
        await jobs_mod.send_slot_job(slot.id)

    assert sent == []
    assert await _slot_count(session, inbox.id) == 1  # slot kept for later
    assert any("Impressum" in r.getMessage() for r in caplog.records)


# ── Rate limits ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_minimum_gap_three_minutes_even_if_inbox_allows_less(session, sent):
    inbox, campaign, lead, cl, slot = await _setup(session, inbox_kwargs={"wait_minutes_between": 1})
    await make_email_log(session, lead.id, campaign.id, inbox_id=inbox.id,
                         sent_at=time_provider.utcnow() - timedelta(minutes=2))
    await jobs_mod.send_slot_job(slot.id)
    assert sent == []
    assert await _slot_count(session, inbox.id) == 1


@pytest.mark.asyncio
async def test_send_allowed_after_three_minutes(session, sent):
    inbox, campaign, lead, cl, slot = await _setup(session, inbox_kwargs={"wait_minutes_between": 1})
    await make_email_log(session, lead.id, campaign.id, inbox_id=inbox.id,
                         sent_at=time_provider.utcnow() - timedelta(minutes=3, seconds=10))
    await jobs_mod.send_slot_job(slot.id)
    assert len(sent) == 1


def test_effective_wait_minutes_floor():
    assert sc.effective_wait_minutes(Inbox(wait_minutes_between=0)) == 3
    assert sc.effective_wait_minutes(Inbox(wait_minutes_between=1)) == 3
    assert sc.effective_wait_minutes(Inbox(wait_minutes_between=7)) == 7


@pytest.mark.asyncio
async def test_global_daily_cap_overrides_higher_inbox_limit(session, sent, monkeypatch):
    monkeypatch.setenv("QUICKLY_MAX_PER_INBOX_PER_DAY", "2")
    inbox, campaign, lead, cl, slot = await _setup(session, inbox_kwargs={"max_emails_per_day": 50})
    assert compute_effective_daily_limit(inbox) == 2
    old = time_provider.utcnow() - timedelta(minutes=30)
    for _ in range(2):
        await make_email_log(session, lead.id, campaign.id, inbox_id=inbox.id, sent_at=old)
    await jobs_mod.send_slot_job(slot.id)
    assert sent == []


def test_global_cap_default_and_inbox_minimum(monkeypatch):
    monkeypatch.delenv("QUICKLY_MAX_PER_INBOX_PER_DAY", raising=False)
    assert sc.global_max_per_inbox_per_day() == 10
    assert compute_effective_daily_limit(Inbox(max_emails_per_day=50, ramp_up_enabled=False)) == 10
    assert compute_effective_daily_limit(Inbox(max_emails_per_day=4, ramp_up_enabled=False)) == 4
    monkeypatch.setenv("QUICKLY_MAX_PER_INBOX_PER_DAY", "kaputt")
    assert sc.global_max_per_inbox_per_day() == 10


# ── Blocklist ───────────────────────────────────────────────────────────────

async def _create_blocklist(session, rows):
    # The real table is owned by another module; create a compatible one here.
    await session.execute(text(
        "CREATE TABLE IF NOT EXISTS blocklist ("
        "id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, "
        "reason TEXT, created_at TIMESTAMP)"
    ))
    for value, kind in rows:
        await session.execute(
            text("INSERT INTO blocklist (value, kind) VALUES (:v, :k)"), {"v": value, "k": kind}
        )


@pytest.fixture
async def drop_blocklist(session):
    yield
    await session.execute(text("DROP TABLE IF EXISTS blocklist"))


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [("blocked@firma.de", "email"), ("firma.de", "domain")])
async def test_blocklisted_recipient_is_skipped(session, sent, drop_blocklist, entry):
    await _create_blocklist(session, [entry])
    inbox, campaign, lead, cl, slot = await _setup(session, lead_email="Blocked@Firma.de")
    await jobs_mod.send_slot_job(slot.id)

    assert sent == []
    assert await _slot_count(session, inbox.id) == 0
    await session.refresh(cl)
    assert cl.enrollment_status == "unsubscribed"


@pytest.mark.asyncio
async def test_blocklist_other_domain_still_sends(session, sent, drop_blocklist):
    await _create_blocklist(session, [("andere.de", "domain")])
    _, _, _, _, slot = await _setup(session, lead_email="x@firma.de")
    await jobs_mod.send_slot_job(slot.id)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_missing_blocklist_table_is_skipped_not_crashing(session, sent):
    await session.execute(text("DROP TABLE IF EXISTS blocklist"))
    assert await sc.is_blocklisted(session, "a@b.de") is None
    _, _, _, _, slot = await _setup(session)
    await jobs_mod.send_slot_job(slot.id)
    assert len(sent) == 1


# ── New inbox defaults ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_inbox_defaults(session):
    from app.schemas import ConnectUrlRequest, InboxCreate

    inbox = Inbox(email="neu@test.com")
    session.add(inbox)
    await session.flush()
    assert inbox.max_emails_per_day == 10
    assert inbox.wait_minutes_between == 3
    assert inbox.ramp_up_enabled is True

    data = InboxCreate(email="x@test.com")
    assert (data.max_emails_per_day, data.wait_minutes_between, data.ramp_up_enabled) == (10, 3, True)
    req = ConnectUrlRequest()
    assert (req.max_per_day, req.wait_minutes_between, req.ramp_up_enabled) == (10, 3, True)


# ── Settings endpoint ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_footer_endpoint_get_put(session, monkeypatch):
    from fastapi import HTTPException
    from app.routers import settings as settings_router

    monkeypatch.delenv("QUICKLY_FOOTER_TEXT", raising=False)
    data = await settings_router.get_footer_endpoint(db=session, _admin=None)
    assert data["source"] == "none" and data["effective_footer_text"] == ""

    monkeypatch.setenv("QUICKLY_FOOTER_TEXT", "Env GmbH\nZeile 2")
    data = await settings_router.get_footer_endpoint(db=session, _admin=None)
    assert data["source"] == "env" and data["effective_footer_text"] == "Env GmbH\nZeile 2"

    data = await settings_router.set_footer_endpoint(
        settings_router._FooterPayload(footer_text="  DB GmbH  "), db=session, _admin=None
    )
    assert data["source"] == "db" and data["footer_text"] == "DB GmbH"
    assert await sc.get_footer_text(session) == "DB GmbH"

    with pytest.raises(HTTPException):
        await settings_router.set_footer_endpoint(
            settings_router._FooterPayload(footer_text="x" * 5000), db=session, _admin=None
        )
