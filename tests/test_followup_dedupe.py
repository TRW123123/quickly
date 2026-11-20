"""Regression tests: a sequence step is sent at most once per (campaign, lead, step).

Production incident (campaign 2): the follow-up (sequence_index=1) went out
again and again to the same lead through the same inbox, spaced by the inbox
wait time, until the inbox's daily cap was used up.  The pre-created EmailLog
row was committed before the SMTP call, but the slot deletion, the final
message_id and the enrollment update were committed only at the very end of
``send_slot_job`` – together with unibox mirroring, webhooks and in-app
notifications.  Any error in that tail rolled the bookkeeping back while the
mail was already out; the slot stayed queued and was dispatched again as soon
as the rate limit allowed it.

The session context used here mirrors ``AsyncSessionLocal``: leaving the
``async with`` block discards everything that was not committed.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

import app.jobs as jobs_mod
from app.models import CampaignLead, EmailLog, QueueSlot, SmtpAccount
from app.sender import SendResult
from tests.conftest import (
    make_campaign,
    make_campaign_inbox,
    make_campaign_lead,
    make_inbox,
    make_lead,
    make_queue_slot,
    make_sequence,
)


class _RollbackOnExitSessionCtx:
    """Like ``AsyncSessionLocal()``: uncommitted work is discarded on exit."""

    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        await self.session.rollback()
        return None


async def _setup_followup(session, *, with_step1_log: bool = False):
    """Campaign with step 0 (sent days ago) and a due slot for step 1 (follow-up)."""
    inbox = await make_inbox(session, email="relay@example.com", provider="smtp",
                             max_emails_per_day=5, wait_minutes_between=8)
    session.add(SmtpAccount(
        inbox_id=inbox.id, smtp_host="smtp.example.com", smtp_port=587,
        smtp_username="u", smtp_password="p", smtp_use_tls=True,
    ))
    campaign = await make_campaign(
        session, sending_days=[0, 1, 2, 3, 4, 5, 6],
        sending_hours_start="00:00", sending_hours_end="23:59",
    )
    await make_sequence(session, campaign.id, position=0, subject="Kurze Frage")
    # Empty subject → threaded follow-up ("Re: ..." + In-Reply-To)
    await make_sequence(session, campaign.id, position=1, subject="", body="Nachfass")
    lead = await make_lead(session, email="lead12@example.com")
    cl = await make_campaign_lead(session, campaign.id, lead.id)
    cl.enrollment_status = "contacted"
    await make_campaign_inbox(session, campaign.id, inbox.id)

    session.add(EmailLog(
        lead_id=lead.id, campaign_id=campaign.id, inbox_id=inbox.id,
        sequence_index=0, subject="Kurze Frage", message_id="<step0@example.com>",
        thread_id="<step0@example.com>", sent_at=datetime.utcnow() - timedelta(days=3),
    ))
    if with_step1_log:
        session.add(EmailLog(
            lead_id=lead.id, campaign_id=campaign.id, inbox_id=inbox.id,
            sequence_index=1, subject="Re: Kurze Frage", message_id="<step1@example.com>",
            thread_id="<step0@example.com>", sent_at=datetime.utcnow() - timedelta(days=1),
        ))
    slot = await make_queue_slot(
        session, cl.id, inbox.id, sequence_index=1,
        scheduled_date=datetime.utcnow() - timedelta(minutes=5),
    )
    await session.commit()
    # Plain ids: the rollback-on-exit context expires ORM instances.
    return _Ids(inbox.id), _Ids(campaign.id), _Ids(lead.id), _Ids(cl.id), _Ids(slot.id)


class _Ids:
    def __init__(self, id_):
        self.id = id_


def _patch_common(monkeypatch, session, sent: list, webhook=None):
    counter = {"n": 0}

    def fake_send(**kwargs):
        counter["n"] += 1
        sent.append(kwargs)
        return SendResult(
            message_id=f"<sent-{counter['n']}@example.com>",
            thread_id="<step0@example.com>",
        )

    async def quiet_webhook(db, event, data):
        return None

    monkeypatch.setattr(jobs_mod, "send_email", fake_send)
    monkeypatch.setattr(jobs_mod, "fire_webhook_event", webhook or quiet_webhook)
    monkeypatch.setattr(jobs_mod, "AsyncSessionLocal", lambda: _RollbackOnExitSessionCtx(session))
    # Pretend the inbox wait time has always elapsed – in production the
    # duplicate went out as soon as the 8–13 minute gap had passed.
    monkeypatch.setattr(jobs_mod, "_rate_limited", lambda delta, required: False)


async def _step_logs(session, campaign_id, lead_id, idx=1) -> int:
    return (await session.execute(
        select(func.count(EmailLog.id)).where(
            EmailLog.campaign_id == campaign_id,
            EmailLog.lead_id == lead_id,
            EmailLog.sequence_index == idx,
        )
    )).scalar()


async def _all_due_slot_ids(session) -> list[int]:
    return list((await session.execute(select(QueueSlot.id))).scalars().all())


@pytest.mark.asyncio
async def test_error_after_smtp_success_never_resends(session, monkeypatch):
    """Post-send failure (webhook/notification/unibox mirror) must not cause a resend."""
    inbox, campaign, lead, cl, slot = await _setup_followup(session)
    sent: list = []

    async def exploding_webhook(db, event, data):
        if event == "email.sent":
            raise RuntimeError("notification insert failed")

    _patch_common(monkeypatch, session, sent, webhook=exploding_webhook)

    with contextlib.suppress(Exception):
        await jobs_mod.send_slot_job(slot.id)
    assert len(sent) == 1

    # Next scan ticks: whatever slot is still due gets dispatched again.
    for _ in range(3):
        for sid in await _all_due_slot_ids(session):
            with contextlib.suppress(Exception):
                await jobs_mod.send_slot_job(sid)

    assert len(sent) == 1, f"follow-up sent {len(sent)}x"
    assert await _step_logs(session, campaign.id, lead.id) == 1
    log_row = (await session.execute(
        select(EmailLog).where(EmailLog.campaign_id == campaign.id,
                               EmailLog.lead_id == lead.id, EmailLog.sequence_index == 1)
    )).scalar_one()
    # The send bookkeeping is committed before the side effects run.
    assert log_row.message_id == "<sent-1@example.com>"
    status = (await session.execute(
        select(CampaignLead.enrollment_status).where(CampaignLead.id == cl.id)
    )).scalar_one()
    assert status == "completed"
    assert await _all_due_slot_ids(session) == []


@pytest.mark.asyncio
async def test_unibox_mirror_db_error_after_send_never_resends(session, monkeypatch):
    """A DB error inside the unibox mirror poisons the session; still no resend."""
    inbox, campaign, lead, cl, slot = await _setup_followup(session)
    sent: list = []
    _patch_common(monkeypatch, session, sent)

    async def broken_upsert(db, **kwargs):
        await db.execute(text("INSERT INTO no_such_table VALUES (1)"))

    monkeypatch.setattr("app.unibox.upsert_sent_smtp_message", broken_upsert)

    for _ in range(3):
        for sid in await _all_due_slot_ids(session):
            with contextlib.suppress(Exception):
                await jobs_mod.send_slot_job(sid)

    assert len(sent) == 1
    assert await _step_logs(session, campaign.id, lead.id) == 1
    assert await _all_due_slot_ids(session) == []


@pytest.mark.asyncio
async def test_send_job_twice_with_stale_slot_does_not_resend(session, monkeypatch):
    """Production state: step 1 already logged, but a slot for it still exists."""
    inbox, campaign, lead, cl, slot = await _setup_followup(session, with_step1_log=True)
    sent: list = []
    _patch_common(monkeypatch, session, sent)

    await jobs_mod.send_slot_job(slot.id)
    await jobs_mod.send_slot_job(slot.id)

    assert sent == []
    assert await _step_logs(session, campaign.id, lead.id) == 1
    assert await _all_due_slot_ids(session) == []


@pytest.mark.asyncio
async def test_slot_job_run_twice_sends_once(session, monkeypatch):
    inbox, campaign, lead, cl, slot = await _setup_followup(session)
    sent: list = []
    _patch_common(monkeypatch, session, sent)

    await jobs_mod.send_slot_job(slot.id)
    await jobs_mod.send_slot_job(slot.id)

    assert len(sent) == 1
    assert sent[0]["subject"] == "Re: Kurze Frage"
    assert sent[0]["reply_to_msg_id"] == "<step0@example.com>"
    assert await _step_logs(session, campaign.id, lead.id) == 1


@pytest.mark.asyncio
async def test_transient_failure_keeps_step_retryable(session, monkeypatch):
    """The claim must be released when the send itself fails (slot back, no log)."""
    inbox, campaign, lead, cl, slot = await _setup_followup(session)
    sent: list = []
    _patch_common(monkeypatch, session, sent)
    monkeypatch.setattr(jobs_mod, "send_email", lambda **kw: (sent.append(kw), None)[1])

    await jobs_mod.send_slot_job(slot.id)

    assert len(sent) == 1
    assert await _step_logs(session, campaign.id, lead.id) == 0
    slots = (await session.execute(select(QueueSlot))).scalars().all()
    assert [(s.campaign_lead_id, s.sequence_index) for s in slots] == [(cl.id, 1)]


@pytest.mark.asyncio
async def test_send_raising_releases_claim(session, monkeypatch):
    inbox, campaign, lead, cl, slot = await _setup_followup(session)
    sent: list = []
    _patch_common(monkeypatch, session, sent)

    def boom(**kw):
        raise RuntimeError("transport exploded")

    monkeypatch.setattr(jobs_mod, "send_email", boom)
    with pytest.raises(RuntimeError):
        await jobs_mod.send_slot_job(slot.id)

    assert await _step_logs(session, campaign.id, lead.id) == 0
    assert len(await _all_due_slot_ids(session)) == 1


@pytest.mark.asyncio
async def test_claim_lost_to_other_worker_skips_send(session, monkeypatch):
    """If the slot vanished between load and claim, nothing is sent."""
    inbox, campaign, lead, cl, slot = await _setup_followup(session)
    sent: list = []
    _patch_common(monkeypatch, session, sent)

    from sqlalchemy import delete as sql_delete
    real_footer = jobs_mod.get_footer_text

    async def footer_and_steal(db):
        # Another worker claims the slot while this one runs its checks.
        await db.execute(sql_delete(QueueSlot).where(QueueSlot.id == slot.id))
        return await real_footer(db)

    monkeypatch.setattr(jobs_mod, "get_footer_text", footer_and_steal)
    await jobs_mod.send_slot_job(slot.id)

    assert sent == []
    assert await _step_logs(session, campaign.id, lead.id) == 0


@pytest.mark.asyncio
async def test_recalc_after_send_does_not_requeue_sent_step(session, monkeypatch):
    """Recalc must drop slots for logged steps and never create new ones for them."""
    from app.queue_logic import (
        recalculate_queue_after_sequence_change_for_leads,
        recalculate_queue_round_robin,
    )

    inbox, campaign, lead, cl, slot = await _setup_followup(session, with_step1_log=True)

    await recalculate_queue_after_sequence_change_for_leads(session, [cl.id])
    await session.commit()
    remaining = (await session.execute(
        select(QueueSlot.sequence_index).where(QueueSlot.campaign_lead_id == cl.id)
    )).scalars().all()
    assert 1 not in remaining and 0 not in remaining

    # Round-robin recalculation (global recalc path) likewise.
    await make_queue_slot(session, cl.id, inbox.id, sequence_index=1,
                          scheduled_date=datetime.utcnow() - timedelta(minutes=1))
    await session.commit()
    await recalculate_queue_round_robin(session, [cl.id])
    await session.commit()
    remaining = (await session.execute(
        select(QueueSlot.sequence_index).where(QueueSlot.campaign_lead_id == cl.id)
    )).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_new_lead_bulk_reservation_skips_logged_steps(session):
    """Re-enrolling a lead whose step 0 is already logged must not re-queue step 0."""
    from app.queue_logic import reserve_slots_for_new_leads_bulk

    inbox, campaign, lead, cl, slot = await _setup_followup(session)
    from sqlalchemy import delete as sql_delete
    await session.execute(sql_delete(QueueSlot).where(QueueSlot.id == slot.id))
    await session.commit()
    cl_row = (await session.execute(select(CampaignLead).where(CampaignLead.id == cl.id))).scalar_one()
    cl_row.enrollment_status = "active"
    await session.commit()

    await reserve_slots_for_new_leads_bulk(session, [cl.id], campaign.id)
    await session.commit()
    idx = (await session.execute(
        select(QueueSlot.sequence_index).where(QueueSlot.campaign_lead_id == cl.id)
    )).scalars().all()
    assert 0 not in idx


@pytest.mark.asyncio
async def test_unique_step_index_tolerates_existing_duplicates(engine):
    """The DB backstop must not break on legacy duplicates, but block new ones."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.database import ensure_email_log_step_unique_index

    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        inbox = await make_inbox(s)
        campaign = await make_campaign(s)
        lead = await make_lead(s)
        for _ in range(3):  # legacy duplicates like lead 12 in production
            s.add(EmailLog(lead_id=lead.id, campaign_id=campaign.id, inbox_id=inbox.id,
                           sequence_index=1, message_id="<dup>"))
        s.add(EmailLog(lead_id=lead.id, campaign_id=campaign.id, inbox_id=inbox.id,
                       sequence_index=0, message_id="<ok>"))
        await s.commit()

    async with engine.begin() as conn:
        created = await ensure_email_log_step_unique_index(conn)
        assert created is True
        # idempotent
        assert await ensure_email_log_step_unique_index(conn) is False

    async with Session() as s:
        assert (await s.execute(select(func.count(EmailLog.id)))).scalar() == 4
        s.add(EmailLog(lead_id=lead.id, campaign_id=campaign.id, inbox_id=inbox.id,
                       sequence_index=2, message_id="<new>"))
        await s.commit()
        s.add(EmailLog(lead_id=lead.id, campaign_id=campaign.id, inbox_id=inbox.id,
                       sequence_index=2, message_id="<new-dup>"))
        with pytest.raises(IntegrityError):
            await s.commit()
