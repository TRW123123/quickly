"""Deliverability defaults (feat/deliv).

* open/click tracking off by default (schema, model, MCP create),
* plain-text-only MIME (QUICKLY_PLAINTEXT_ONLY, default on),
* content guard: max links per step body (QUICKLY_MAX_LINKS_PER_MAIL), no images,
  on save (REST + MCP) and fail-closed at send time,
* RFC 8058: List-Unsubscribe + List-Unsubscribe-Post on SMTP/Gmail MIME,
  POST /u/{token} one-click unsubscribe that also blocklists the address.

No network: send_email is faked, MIME is only built, never sent.
"""
from __future__ import annotations

import base64
import email
import email.policy
import logging
from datetime import timedelta

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import func, select

import app.jobs as jobs_mod
from app import deliverability as dv
from app import time as time_provider
from app.mcp_campaigns import ToolError, create_campaign_core
from app.models import (
    BlocklistEntry,
    Campaign,
    CampaignLead,
    Lead,
    LeadUnsubscribeToken,
    QueueSlot,
)
from app.routers import campaigns as campaigns_router
from app.routers.tracking import unsubscribe_one_click
from app.schemas import CampaignCreate, SequenceCreate, SequenceUpdate
from app.sender import SendResult, _build_email_message, build_raw_mime
from tests.conftest import (
    make_campaign,
    make_campaign_inbox,
    make_campaign_lead,
    make_inbox,
    make_lead,
    make_queue_slot,
    make_sequence,
)

UNSUB = "https://track.example.com/u/tok123"


@pytest.fixture(autouse=True)
def _deliv_env(monkeypatch):
    monkeypatch.delenv("QUICKLY_PLAINTEXT_ONLY", raising=False)
    monkeypatch.delenv("QUICKLY_MAX_LINKS_PER_MAIL", raising=False)


# ── 1. Tracking defaults ────────────────────────────────────────────────────

def test_tracking_defaults_off_in_schema_and_model():
    c = CampaignCreate(name="x", inbox_ids=[1])
    assert c.track_opens is False and c.track_clicks is False
    assert Campaign.__table__.c.track_opens.default.arg is False
    assert Campaign.__table__.c.track_clicks.default.arg is False


@pytest.mark.asyncio
async def test_mcp_create_campaign_tracking_off(session):
    out = await create_campaign_core(session, "MCP", [{"subject": "Hi", "body": "Hallo", "delay_days": 0}])
    camp = (await session.execute(select(Campaign).where(Campaign.id == out["id"]))).scalar_one()
    assert camp.track_opens is False and camp.track_clicks is False


# ── 2. Plain-text only ──────────────────────────────────────────────────────

def test_settings_defaults():
    assert dv.plaintext_only() is True
    assert dv.max_links_per_mail() == 1


def test_plaintext_mime_has_no_html_part():
    msg = _build_email_message(
        to_email="a@example.com", subject="S",
        body='<p>Hallo <b>Welt</b></p><p><a href="https://x.example/p">Seite</a></p>',
        from_email="me@example.com", is_html=True, list_unsubscribe_url=UNSUB,
    )
    types = [p.get_content_type() for p in msg.walk()]
    assert "text/html" not in types
    assert msg.get_content_type() == "text/plain"
    text = msg.get_content()
    assert "Hallo Welt" in text
    assert "Seite (https://x.example/p)" in text
    assert "<" not in text


def test_plaintext_off_restores_html_alternative(monkeypatch):
    monkeypatch.setenv("QUICKLY_PLAINTEXT_ONLY", "false")
    msg = _build_email_message(
        to_email="a@example.com", subject="S", body="<p>Hi</p>",
        from_email="me@example.com", is_html=True,
    )
    assert "text/html" in [p.get_content_type() for p in msg.walk()]


def test_html_to_plain_keeps_bare_link_once():
    assert dv.html_to_plain('<a href="https://a.example">https://a.example</a>') == "https://a.example"


# ── 3. Content guard ────────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    "Hi https://a.example and https://b.example",
    '<p><a href="https://a.example">a</a> <a href="https://b.example">b</a></p>',
    'Logo <img src="https://a.example/l.png">',
    "Look ![logo](https://a.example/l.png)",
    "inline data:image/png;base64,AAAA",
])
def test_guard_rejects(body):
    assert dv.content_violation(body) is not None


@pytest.mark.parametrize("body", [
    "Hallo, kein Link.",
    "Termin: https://cal.example/me",
    '<a href="https://a.example">https://a.example</a>',  # same URL twice = 1 link
    "Abmelden: {{unsubscribe_link}} oder https://cal.example/me",
])
def test_guard_allows(body):
    assert dv.content_violation(body) is None


def test_guard_ignores_unsubscribe_url_and_respects_env(monkeypatch):
    assert dv.content_violation(f"https://cal.example {UNSUB}", ignore_urls=[UNSUB]) is None
    monkeypatch.setenv("QUICKLY_MAX_LINKS_PER_MAIL", "2")
    assert dv.content_violation("https://a.example https://b.example") is None


@pytest.mark.asyncio
async def test_rest_sequence_create_and_update_reject(session):
    camp = await make_campaign(session)
    with pytest.raises(HTTPException) as ei:
        await campaigns_router.create_sequence(
            camp.id,
            SequenceCreate(position=0, subject="S", body="https://a.example https://b.example"),
            BackgroundTasks(), db=session,
        )
    assert ei.value.status_code == 400 and "too many links" in ei.value.detail

    seq = await make_sequence(session, camp.id, body="ok")
    with pytest.raises(HTTPException) as ei:
        await campaigns_router.update_sequence(
            camp.id, seq.id, SequenceUpdate(body='<img src="x.png">'), BackgroundTasks(), db=session,
        )
    assert ei.value.status_code == 400 and "images" in ei.value.detail


@pytest.mark.asyncio
async def test_mcp_create_campaign_rejects_two_links(session):
    with pytest.raises(ToolError, match="too many links"):
        await create_campaign_core(
            session, "Bad", [{"subject": "S", "body": "https://a.example https://b.example"}]
        )


class _SessionCtx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return None


@pytest.fixture
def sent(monkeypatch, session):
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


async def _setup_slot(session, body, *, is_html=False, campaign_kwargs=None):
    inbox = await make_inbox(session, provider="smtp")
    inbox.ramp_up_enabled = False
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
    lead = await make_lead(session, email="lead@example.com")
    cl = await make_campaign_lead(session, campaign.id, lead.id)
    await make_campaign_inbox(session, campaign.id, inbox.id)
    slot = await make_queue_slot(
        session, cl.id, inbox.id, scheduled_date=time_provider.now() - timedelta(minutes=1)
    )
    await session.flush()
    return inbox, slot


@pytest.mark.asyncio
async def test_send_time_guard_fail_closed(session, sent, caplog):
    # Saved directly (bypassing the API guard): two links must not go out.
    inbox, slot = await _setup_slot(session, "Hallo https://a.example und https://b.example")
    with caplog.at_level(logging.ERROR, logger="quickly.jobs"):
        await jobs_mod.send_slot_job(slot.id)
    assert sent == []
    left = (await session.execute(
        select(func.count(QueueSlot.id)).where(QueueSlot.inbox_id == inbox.id)
    )).scalar()
    assert left == 1
    assert any("too many links" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_send_html_step_goes_out_as_plain_text_without_tracking(session, sent):
    _, slot = await _setup_slot(
        session, '<p>Hallo {{name}}</p><p><a href="https://cal.example/me">Termin</a></p>',
        is_html=True, campaign_kwargs={"track_opens": True, "track_clicks": True,
                                       "add_one_click_unsubscribe": False},
    )
    await jobs_mod.send_slot_job(slot.id)
    assert len(sent) == 1
    call = sent[0]
    assert call["is_html"] is False
    assert "<p>" not in call["body"] and "/c/" not in call["body"]
    assert "Termin (https://cal.example/me)" in call["body"]
    unsub = call["list_unsubscribe_url"]
    assert call["body"].rstrip().endswith(unsub)  # plain-text footer with unsubscribe URL
    assert call["list_unsubscribe_one_click"] is True  # RFC 8058 always on


# ── 4. RFC 8058 headers ─────────────────────────────────────────────────────

def test_smtp_mime_carries_both_unsubscribe_headers():
    msg = _build_email_message(
        to_email="a@example.com", subject="S", body="Hi",
        from_email="me@example.com", list_unsubscribe_url=UNSUB,
    )
    assert msg["List-Unsubscribe"] == f"<{UNSUB}>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_gmail_raw_mime_carries_both_unsubscribe_headers():
    raw = build_raw_mime(
        to_email="a@example.com", subject="S", body="<p>Hi</p>", is_html=True,
        from_email="me@example.com", list_unsubscribe_url=UNSUB,
    )
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)
    assert parsed["List-Unsubscribe"] == f"<{UNSUB}>"
    assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "text/html" not in [p.get_content_type() for p in parsed.walk()]


# ── 5. One-click POST unsubscribe ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_unsubscribe_marks_lead_and_blocklists(session, monkeypatch):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr("app.tracking_events.fire_webhook_event", _noop)

    inbox = await make_inbox(session)
    camp = await make_campaign(session)
    lead = await make_lead(session, email="Opt.Out@Example.com")
    cl = await make_campaign_lead(session, camp.id, lead.id)
    await make_queue_slot(session, cl.id, inbox.id)
    session.add(LeadUnsubscribeToken(lead_id=lead.id, campaign_id=camp.id, token="tok-post"))
    await session.flush()

    resp = await unsubscribe_one_click(token="tok-post", db=session)
    assert resp.status_code == 200
    # Idempotent: second POST is fine and creates no duplicate.
    resp2 = await unsubscribe_one_click(token="tok-post", db=session)
    assert resp2.status_code == 200

    cl_row = (await session.execute(select(CampaignLead).where(CampaignLead.id == cl.id))).scalar_one()
    assert cl_row.enrollment_status == "unsubscribed"
    lead_row = (await session.execute(select(Lead).where(Lead.id == lead.id))).scalar_one()
    assert lead_row.status == "unsubscribed"
    entries = (await session.execute(
        select(BlocklistEntry).where(BlocklistEntry.value == "opt.out@example.com")
    )).scalars().all()
    assert len(entries) == 1 and entries[0].kind == "email"
    slots = (await session.execute(
        select(func.count(QueueSlot.id)).where(QueueSlot.campaign_lead_id == cl.id)
    )).scalar()
    assert slots == 0


@pytest.mark.asyncio
async def test_post_unsubscribe_unknown_token_404(session):
    resp = await unsubscribe_one_click(token="nope", db=session)
    assert resp.status_code == 404
