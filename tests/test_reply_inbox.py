"""Tests for the bot-facing reply inbox (app/reply_inbox.py + /api/reply-inbox)."""
import ast
import inspect
import json
import pathlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

import app.reply_inbox as reply_inbox_mod
from app.models import (
    CampaignLead,
    EmailLog,
    GmailMessage,
    GmailThread,
    LeadReply,
    QueueSlot,
    ReplyInbox,
    SmtpMessage,
    SmtpThread,
)
from app.reply_inbox import classify_heuristic, reply_text, sync_reply_inbox
from app.routers import reply_inbox as router_mod
from tests.conftest import (
    make_campaign,
    make_campaign_lead,
    make_email_log,
    make_inbox,
    make_lead,
)

NOW = datetime(2026, 11, 14, 12, 0, 0)


def _ms(dt: datetime) -> int:
    return int((dt - datetime(1970, 1, 1)).total_seconds() * 1000)


async def _gmail_reply(session, inbox, lead, *, body, subject="Re: Hallo", when=None, msg_id="m1", extra_headers=()):
    when = when or NOW - timedelta(hours=3)
    session.add(GmailThread(inbox_id=inbox.id, thread_id=f"t-{msg_id}", last_internal_date=_ms(when)))
    headers = [
        {"name": "From", "value": f"{lead.name} <{lead.email}>"},
        {"name": "Subject", "value": subject},
        *extra_headers,
    ]
    session.add(
        GmailMessage(
            inbox_id=inbox.id,
            message_id=msg_id,
            thread_id=f"t-{msg_id}",
            internal_date=_ms(when),
            snippet=body[:100],
            headers_json=json.dumps(headers),
            label_ids_json=json.dumps(["INBOX"]),
            body_fetched=True,
            body_plain=body,
        )
    )
    await session.flush()


async def _setup(session, *, body="Klingt spannend, rufen Sie mich gern an.", email="kunde@firma.de", company="Firma GmbH"):
    inbox = await make_inbox(session, email="sales@quickly.test")
    campaign = await make_campaign(session)
    lead = await make_lead(session, email=email, name="Kim Kunde")
    lead.custom_data = {"company": company}
    cl = await make_campaign_lead(session, campaign.id, lead.id)
    await make_email_log(session, lead.id, campaign.id, inbox_id=inbox.id, sent_at=NOW - timedelta(days=2))
    await _gmail_reply(session, inbox, lead, body=body)
    lr = LeadReply(lead_id=lead.id, campaign_id=campaign.id, replied_at=NOW - timedelta(hours=3))
    session.add(lr)
    await session.flush()
    return inbox, campaign, lead, cl, lr


# ── heuristic ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "subject,text,from_email,expected",
    [
        ("Automatische Antwort: Hallo", "Ich bin bis 20.11. nicht im Büro.", "a@b.de", "out_of_office"),
        ("Out of Office", "I am currently on vacation.", "a@b.com", "out_of_office"),
        ("Abwesenheitsnotiz", "Vielen Dank", "a@b.de", "out_of_office"),
        ("Re: Hallo", "Dies ist eine automatische Antwort, bitte antworten Sie nicht.", "a@b.de", "auto_reply"),
        ("Mail Delivery Subsystem", "The message could not be sent", "a@b.de", "bounce"),
        ("Undeliverable: Hallo", "", "postmaster@b.de", "bounce"),
        ("Re: Hallo", "Bitte abmelden.", "a@b.de", "unsubscribed"),
        ("Re: Hallo", "Please unsubscribe me from this list", "a@b.com", "unsubscribed"),
        ("Re: Hallo", "Danke, aber kein Interesse.", "a@b.de", "not_interested"),
        ("Re: Hallo", "Not interested, thanks.", "a@b.com", "not_interested"),
        ("Re: Hallo", "Klingt gut, wann passt Ihnen ein Telefonat?", "a@b.de", "unklassifiziert_echt"),
    ],
)
def test_heuristic(subject, text, from_email, expected):
    assert classify_heuristic(subject, text, from_email) == expected


def test_heuristic_auto_submitted_header():
    assert classify_heuristic("Re: x", "Danke", "a@b.de", {"Auto-Submitted": "auto-replied"}) == "auto_reply"
    assert classify_heuristic("Re: x", "Danke", "a@b.de", {"Auto-Submitted": "no"}) == "unklassifiziert_echt"


def test_heuristic_ignores_quoted_unsubscribe_footer():
    body = (
        "Gern, schicken Sie mir Infos.\n\n"
        "Am 12.11.2026 um 10:00 schrieb Sales <sales@quickly.test>:\n"
        "> Hallo Kim ...\n> Wenn Sie nichts mehr hören wollen: abmelden"
    )
    text = reply_text(body, "")
    assert "abmelden" not in text
    assert classify_heuristic("Re: Hallo", text, "a@b.de") == "unklassifiziert_echt"


# ── sync job ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_inserts_real_reply_and_is_idempotent(session):
    inbox, campaign, lead, cl, lr = await _setup(session)

    stats = await sync_reply_inbox(session, now=NOW)
    assert stats["inserted"] == 1
    stats2 = await sync_reply_inbox(session, now=NOW)
    assert stats2["inserted"] == 0

    rows = (await session.execute(select(ReplyInbox))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.lead_reply_id == lr.id
    assert row.lead_id == lead.id and row.campaign_id == campaign.id
    assert row.inbox_id == inbox.id and row.inbox_email == "sales@quickly.test"
    assert row.from_email == "kunde@firma.de" and row.from_name == "Kim Kunde"
    assert row.company == "Firma GmbH"
    assert row.subject == "Re: Hallo"
    assert row.snippet.startswith("Klingt spannend")
    assert row.classification == "unklassifiziert_echt"
    assert row.classification_source == "heuristik"
    assert row.is_positive is None
    assert row.status == "neu"


@pytest.mark.asyncio
async def test_bot_status_survives_resync(session):
    await _setup(session)
    await sync_reply_inbox(session, now=NOW)
    row = (await session.execute(select(ReplyInbox))).scalar_one()
    row.status = "erledigt"
    await session.flush()
    await sync_reply_inbox(session, now=NOW)
    row = (await session.execute(select(ReplyInbox))).scalar_one()
    assert row.status == "erledigt"


@pytest.mark.asyncio
async def test_out_of_office_stored_as_auto(session):
    await _setup(session, body="Ich bin bis zum 20.11. im Urlaub und nicht im Büro.")
    await sync_reply_inbox(session, now=NOW)
    row = (await session.execute(select(ReplyInbox))).scalar_one()
    assert row.classification == "out_of_office"
    assert row.status == "auto"
    assert row.is_positive is False


@pytest.mark.asyncio
async def test_ai_interested_is_used_and_marked_positive(session):
    _, _, _, cl, _ = await _setup(session, body="Kein Interesse")  # heuristic would say negative
    cl.interest_status = "interested"
    await session.flush()
    await sync_reply_inbox(session, now=NOW)
    row = (await session.execute(select(ReplyInbox))).scalar_one()
    assert row.classification == "interested"
    assert row.classification_source == "quickly"
    assert row.is_positive is True
    assert row.status == "neu"
    cl_db = (await session.execute(select(CampaignLead).where(CampaignLead.id == cl.id))).scalar_one()
    assert cl_db.interest_status == "interested"


@pytest.mark.asyncio
async def test_late_ai_result_reclassifies_heuristic_row(session):
    _, _, _, cl, _ = await _setup(session)
    await sync_reply_inbox(session, now=NOW)
    cl.interest_status = "interested"
    await session.flush()
    stats = await sync_reply_inbox(session, now=NOW)
    assert stats["reclassified"] == 1
    row = (await session.execute(select(ReplyInbox))).scalar_one()
    assert row.classification == "interested" and row.is_positive is True
    assert row.classification_source == "quickly"


@pytest.mark.asyncio
async def test_missing_mail_is_deferred_then_stored(session):
    campaign = await make_campaign(session)
    lead = await make_lead(session, email="leer@firma.de")
    await make_campaign_lead(session, campaign.id, lead.id)
    session.add(LeadReply(lead_id=lead.id, campaign_id=campaign.id, replied_at=NOW - timedelta(minutes=5)))
    await session.flush()

    stats = await sync_reply_inbox(session, now=NOW)
    assert stats == {"inserted": 0, "deferred": 1, "reclassified": 0}
    stats = await sync_reply_inbox(session, now=NOW + timedelta(hours=3))
    assert stats["inserted"] == 1
    row = (await session.execute(select(ReplyInbox))).scalar_one()
    assert row.subject == "" and row.from_email == "leer@firma.de"
    assert row.classification == "unklassifiziert_echt"


@pytest.mark.asyncio
async def test_smtp_reply_is_found(session):
    inbox = await make_inbox(session, email="imap@quickly.test", provider="smtp")
    campaign = await make_campaign(session)
    lead = await make_lead(session, email="imap-lead@firma.de")
    await make_campaign_lead(session, campaign.id, lead.id)
    session.add(SmtpThread(inbox_id=inbox.id, thread_key="k1"))
    session.add(
        SmtpMessage(
            inbox_id=inbox.id, message_id="1:1", thread_key="k1", direction="received",
            from_address="imap-lead@firma.de", subject="AW: Angebot",
            body_html="<p>Bitte <b>abmelden</b>.</p>", received_at=NOW - timedelta(hours=4),
        )
    )
    session.add(LeadReply(lead_id=lead.id, campaign_id=campaign.id, replied_at=NOW - timedelta(hours=4)))
    await session.flush()
    await sync_reply_inbox(session, now=NOW)
    row = (await session.execute(select(ReplyInbox))).scalar_one()
    assert row.inbox_id == inbox.id
    assert row.subject == "AW: Angebot"
    assert row.classification == "unsubscribed" and row.is_positive is False and row.status == "neu"


# ── never send ───────────────────────────────────────────────────────────────

def test_module_never_imports_send_paths():
    """The reply inbox must never answer a reply: no sender/job imports at all."""
    src = pathlib.Path(inspect.getfile(reply_inbox_mod)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    forbidden = {"app.sender", "app.jobs", "smtplib", "app.smtp_utils", "app.webhooks"}
    assert not any(m in forbidden or m.startswith("app.sender") for m in imported), imported
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not names & {"send_email", "send_message", "sendmail", "send_mail", "reply"}


@pytest.mark.asyncio
async def test_sync_job_does_not_send(session, monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("reply inbox sync tried to send mail")

    import app.jobs as jobs_mod
    import app.sender as sender_mod

    monkeypatch.setattr(sender_mod, "send_email", _boom)
    monkeypatch.setattr(jobs_mod, "send_email", _boom)
    monkeypatch.setattr("smtplib.SMTP.sendmail", _boom)
    monkeypatch.setattr("smtplib.SMTP.send_message", _boom)

    _, _, _, cl, _ = await _setup(session)
    cl.interest_status = "interested"
    await session.flush()
    logs_before = (await session.execute(select(func.count(EmailLog.id)))).scalar()

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    # Patch the module reference only: patching app.database.AsyncSessionLocal
    # itself would be restored after the engine fixture's teardown and leave a
    # disposed sessionmaker behind for later tests.
    monkeypatch.setattr(reply_inbox_mod, "_database", SimpleNamespace(AsyncSessionLocal=lambda: _Ctx()))
    await reply_inbox_mod.run_reply_inbox_sync_job()

    assert (await session.execute(select(func.count(ReplyInbox.id)))).scalar() == 1
    assert (await session.execute(select(func.count(EmailLog.id)))).scalar() == logs_before
    assert (await session.execute(select(func.count(QueueSlot.id)))).scalar() == 0


def _main_source() -> str:
    # Read app/main.py as text: importing it needs a built frontend (frontend/dist).
    return (pathlib.Path(inspect.getfile(reply_inbox_mod)).parent / "main.py").read_text(encoding="utf-8")


def test_job_registered_only_in_scheduler_branch():
    src = _main_source()
    lifespan = src[src.index("async def lifespan"):]
    disabled_return = lifespan.index("if not scheduler_enabled():")
    reg = lifespan.index('id="sync_reply_inbox"')
    assert reg > lifespan.index("return", disabled_return)
    assert "run_reply_inbox_sync_job" in lifespan[:reg]
    assert reply_inbox_mod.SYNC_INTERVAL_MINUTES == 10


# ── API ──────────────────────────────────────────────────────────────────────

def test_api_rejects_unknown_status():
    with pytest.raises(Exception):
        router_mod.ReplyInboxPatch(status="geloescht")


def test_api_router_mounted_with_auth():
    assert "app.include_router(reply_inbox_router.router, dependencies=_auth_deps)" in _main_source()
    assert router_mod.router.prefix == "/api/reply-inbox"


# Keep a DB-backed test last: several existing API tests (test_server_info_api,
# test_spa_routing) only find their schema when the previous test used the
# ``engine`` fixture, whose teardown re-creates tables on the app engine.
@pytest.mark.asyncio
async def test_api_list_filter_and_patch(session):
    _, _, _, cl, _ = await _setup(session)
    await sync_reply_inbox(session, now=NOW)

    res = await router_mod.list_reply_inbox(status="neu", is_positive=None, since=None, limit=100, offset=0, db=session)
    assert res.total == 1 and res.items[0].status == "neu"
    res = await router_mod.list_reply_inbox(status=None, is_positive=True, since=None, limit=100, offset=0, db=session)
    assert res.total == 0
    res = await router_mod.list_reply_inbox(
        status=None, is_positive=None, since=NOW - timedelta(hours=1), limit=100, offset=0, db=session
    )
    assert res.total == 0
    res = await router_mod.list_reply_inbox(
        status=None, is_positive=None, since=NOW - timedelta(days=1), limit=100, offset=0, db=session
    )
    assert res.total == 1
    item_id = res.items[0].id

    out = await router_mod.patch_reply_inbox(item_id, router_mod.ReplyInboxPatch(status="gesehen"), db=session)
    assert out.status == "gesehen"
    out = await router_mod.patch_reply_inbox(item_id, router_mod.ReplyInboxPatch(is_positive=True), db=session)
    assert out.is_positive is True
    cl_db = (await session.execute(select(CampaignLead).where(CampaignLead.id == cl.id))).scalar_one()
    assert cl_db.interest_status == "interested"

    with pytest.raises(HTTPException) as exc:
        await router_mod.patch_reply_inbox(99999, router_mod.ReplyInboxPatch(status="erledigt"), db=session)
    assert exc.value.status_code == 404
