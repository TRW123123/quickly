"""Blocklist, MCP campaign tools, draft enforcement and start approval/logging."""

import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import database as _adb
from app.blocklist import blocked_emails, is_blocked, normalize
from app.mcp_campaigns import (
    add_leads_core,
    create_campaign_core,
    get_stats_core,
    list_campaigns_core,
    list_replies_core,
    pause_campaign_core,
    start_or_resume_core,
)
from app.mcp_leads import leads_mcp
from app.models import BlocklistEntry, Campaign, CampaignLead, CampaignStartLog, Lead, LeadReply
from app.routers.blocklist import BlocklistAdd, add_blocklist, delete_blocklist, list_blocklist
from app.routers.campaigns import CampaignLeadAdd, bulk_add_leads_to_campaign, import_campaign_leads
from tests.conftest import (
    make_campaign,
    make_campaign_inbox,
    make_campaign_lead,
    make_email_log,
    make_inbox,
    make_lead,
    make_sequence,
)


@pytest.fixture(autouse=True)
def _no_network_or_background(monkeypatch):
    """No MX lookups, no background provider detection, no global recalcs."""

    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr("app.email_provider.detect_provider_for_email", _none)
    monkeypatch.setattr("app.routers.campaigns._detect_providers_background", _none)
    monkeypatch.setattr("app.mcp_campaigns._schedule_global_recalc", lambda: None)


async def _block(session, *values):
    for v in values:
        session.add(BlocklistEntry(value=normalize(v), kind="email" if "@" in v else "domain"))
    await session.flush()


async def _call(name: str, args: dict) -> dict:
    res = await leads_mcp.call_tool(name, args)
    if isinstance(res, tuple):  # newer mcp: (content, structured)
        res = res[0]
    return json.loads(res[0].text)


async def _fresh(fn):
    async with _adb.AsyncSessionLocal() as db:
        return await fn(db)


# ── Blocklist helper ────────────────────────────────────────────────────────

async def test_normalize():
    assert normalize("  Foo@Bar.COM ") == "foo@bar.com"
    assert normalize("@Example.org") == "example.org"


async def test_is_blocked_email_and_domain(session):
    await _block(session, "Boss@Acme.com", "competitor.io")
    assert await is_blocked(session, "boss@acme.com")
    assert await is_blocked(session, "  BOSS@ACME.COM ")
    assert not await is_blocked(session, "other@acme.com")
    assert await is_blocked(session, "anyone@Competitor.io")
    assert not await is_blocked(session, "x@sub.competitor.io")
    assert not await is_blocked(session, "")
    hits = await blocked_emails(session, ["a@competitor.io", "boss@acme.com", "ok@fine.de"])
    assert hits == {"a@competitor.io", "boss@acme.com"}


# ── Blocklist REST ──────────────────────────────────────────────────────────

async def test_blocklist_rest_add_list_delete(session):
    one = await add_blocklist(BlocklistAdd(value=" Spam@Example.com ", reason="asked"), db=session)
    assert [e["value"] for e in one["added"]] == ["spam@example.com"]
    assert one["added"][0]["kind"] == "email"

    many = await add_blocklist(
        [BlocklistAdd(value="@Rival.DE"), "spam@example.com", "not an email", BlocklistAdd(value="x@y.z", kind="bogus")],
        db=session,
    )
    assert [e["value"] for e in many["added"]] == ["rival.de"]
    assert many["added"][0]["kind"] == "domain"
    assert many["existing"] == ["spam@example.com"]
    assert set(many["invalid"]) == {"not an email", "x@y.z"}

    listing = await list_blocklist(q="", kind="", limit=100, offset=0, db=session)
    assert listing["total"] == 2
    dom = next(i for i in listing["items"] if i["value"] == "rival.de")
    res = await delete_blocklist(dom["id"], db=session)
    assert res["ok"] is True
    assert not await is_blocked(session, "a@rival.de")


def test_blocklist_requires_auth():
    with TestClient(app_instance()) as client:
        assert client.get("/api/blocklist").status_code == 401
        assert client.post("/api/blocklist", json={"value": "a@b.de"}).status_code == 401


def app_instance():
    from app.main import app

    return app


# ── Lead imports skip blocked addresses ─────────────────────────────────────

async def test_bulk_add_skips_blocked(session):
    campaign = await make_campaign(session)
    await _block(session, "no@acme.com", "blocked.org")
    result = await bulk_add_leads_to_campaign(
        campaign_id=campaign.id,
        leads_data=[
            CampaignLeadAdd(email="NO@acme.com"),
            CampaignLeadAdd(email="ceo@blocked.org"),
            CampaignLeadAdd(email="yes@acme.com"),
        ],
        skip_duplicates=True,
        db=session,
    )
    assert result["added"] == 1
    assert result["blocked"] == 2
    assert set(result["blocked_leads"]) == {"no@acme.com", "ceo@blocked.org"}
    emails = (await session.execute(select(Lead.email))).scalars().all()
    assert emails == ["yes@acme.com"]


async def test_csv_import_skips_blocked(session):
    campaign = await make_campaign(session)
    await _block(session, "blocked.org")

    class _Upload:
        filename = "leads.csv"

        async def read(self):
            return b"email,name\nceo@blocked.org,X\nok@fine.de,Y\n"

    result = await import_campaign_leads(campaign_id=campaign.id, file=_Upload(), skip_duplicates=True, db=session)
    assert result["added"] == 1
    assert result["blocked"] == 1
    assert result["blocked_leads"] == ["ceo@blocked.org"]


# ── MCP add_leads ───────────────────────────────────────────────────────────

async def test_add_leads_duplicates_blocked_invalid(session):
    other = await make_campaign(session, name="Other")
    campaign = await make_campaign(session, name="Target", paused=True)
    # existing lead enrolled elsewhere + existing lead not enrolled anywhere
    enrolled = await make_lead(session, email="old@acme.com")
    await make_campaign_lead(session, other.id, enrolled.id)
    await make_lead(session, email="orphan@acme.com")
    await _block(session, "blocked.org", "vip@acme.com")

    result = await add_leads_core(
        session,
        campaign.id,
        leads=[
            {"email": "New@Acme.com", "name": "New", "company": "Acme"},
            "new@acme.com",
            "OLD@acme.com",
            {"email": "orphan@acme.com"},
            {"email": "ceo@blocked.org"},
            "vip@acme.com",
            "not-an-email",
        ],
        csv_text="email,name,title\nfresh@site.de,Fresh,CTO\n",
    )
    assert result["added"] == {"count": 2, "emails": ["new@acme.com", "fresh@site.de"]}
    assert result["duplicates"]["count"] == 3
    assert set(result["duplicates"]["emails"]) == {"new@acme.com", "old@acme.com", "orphan@acme.com"}
    assert result["blocked"]["count"] == 2
    assert set(result["blocked"]["emails"]) == {"ceo@blocked.org", "vip@acme.com"}
    assert result["invalid"] == {"count": 1, "emails": ["not-an-email"]}

    lead = (await session.execute(select(Lead).where(Lead.email == "new@acme.com"))).scalar_one()
    assert lead.name == "New" and lead.custom_data == {"company": "Acme"}
    fresh = (await session.execute(select(Lead).where(Lead.email == "fresh@site.de"))).scalar_one()
    assert fresh.custom_data == {"title": "CTO"}
    n_enrolled = len(
        (await session.execute(select(CampaignLead).where(CampaignLead.campaign_id == campaign.id))).all()
    )
    assert n_enrolled == 2


async def test_add_leads_csv_string_in_leads_param(session):
    campaign = await make_campaign(session, paused=True)
    result = await add_leads_core(session, campaign.id, leads="email\na@x.de\nb@x.de\na@x.de\n")
    assert result["added"]["count"] == 2
    assert result["duplicates"]["emails"] == ["a@x.de"]


# ── create_campaign is always a draft ───────────────────────────────────────

async def test_create_campaign_core_always_paused(session):
    inbox = await make_inbox(session, email="sender@test.com")
    out = await create_campaign_core(
        session,
        "Q4 Outreach",
        steps=[{"subject": "Hi", "body": "Hello", "delay_days": 0}, {"subject": "", "body": "Bump", "delay_days": 3}],
        inbox_ids=[inbox.id],
    )
    camp = await session.get(Campaign, out["id"])
    assert camp.paused is True and out["status"] == "paused"
    assert out["sequence_steps"] == 2 and out["inbox_ids"] == [inbox.id]


async def test_create_campaign_tool_ignores_paused_false(engine):
    data = await _call("create_campaign", {"name": "Via MCP", "paused": False, "steps": [{"subject": "S", "body": "B"}]})
    assert data["paused"] is True

    async def _get(db):
        return (await db.get(Campaign, data["id"])).paused

    assert await _fresh(_get) is True


async def test_create_campaign_unknown_inbox_creates_nothing(engine):
    data = await _call("create_campaign", {"name": "Bad", "inbox_ids": [9999]})
    assert "error" in data

    async def _count(db):
        return len((await db.execute(select(Campaign))).all())

    assert await _fresh(_count) == 0


# ── start / resume approval gate and log ───────────────────────────────────

async def _ready_campaign(session):
    campaign = await make_campaign(session, paused=True)
    inbox = await make_inbox(session, email="gate@test.com")
    await make_campaign_inbox(session, campaign.id, inbox.id)
    await make_sequence(session, campaign.id, position=0)
    await session.commit()
    return campaign.id


async def _logs(db):
    return (await db.execute(select(CampaignStartLog).order_by(CampaignStartLog.id))).scalars().all()


async def test_start_rejected_without_approval_and_logged(session):
    cid = await _ready_campaign(session)

    for bad in (False, None, "true", 1):
        out = await start_or_resume_core(session, cid, "start", bad, {"api_key_id": 7, "user": "a@b.de"})
        assert out["ok"] is False and "approved_by_safak" in out["error"]
    out = await start_or_resume_core(session, cid, "resume", False)
    assert out["ok"] is False

    assert (await session.get(Campaign, cid)).paused is True
    logs = await _logs(session)
    assert len(logs) == 5
    assert all(l.result.startswith("rejected") for l in logs)
    assert [l.action for l in logs] == ["start"] * 4 + ["resume"]
    assert logs[0].approved_by_safak is False and logs[0].api_key_id == 7 and logs[0].user == "a@b.de"
    assert logs[2].approved_by_safak is None  # non-bool value


async def test_start_with_approval_unpauses_and_logs(session):
    cid = await _ready_campaign(session)
    out = await start_or_resume_core(session, cid, "start", True)
    assert out["ok"] is True and out["result"] == "started" and out["changed"] is True
    assert (await session.get(Campaign, cid)).paused is False

    await pause_campaign_core(session, cid)
    out = await start_or_resume_core(session, cid, "resume", True)
    assert out["result"] == "resumed"
    logs = await _logs(session)
    assert [(l.action, l.approved_by_safak, l.result) for l in logs] == [
        ("start", True, "started"),
        ("resume", True, "resumed"),
    ]


async def test_start_requires_sequence_and_inbox(session):
    campaign = await make_campaign(session, paused=True)
    out = await start_or_resume_core(session, campaign.id, "start", True)
    assert out["ok"] is False and "no sequence" in out["error"]
    assert (await session.get(Campaign, campaign.id)).paused is True
    assert len(await _logs(session)) == 1


async def test_start_tool_rejects_and_logs(engine):
    async def _setup(db):
        c = Campaign(name="Tool", paused=True)
        db.add(c)
        await db.commit()
        return c.id

    cid = await _fresh(_setup)
    data = await _call("start_campaign", {"campaign_id": cid, "approved_by_safak": False})
    assert data["ok"] is False
    data = await _call("resume_campaign", {"campaign_id": cid, "approved_by_safak": "true"})
    assert data["ok"] is False
    with pytest.raises(Exception):
        await _call("start_campaign", {"campaign_id": cid})  # missing required param

    async def _state(db):
        return (await db.get(Campaign, cid)).paused, [(l.action, l.result[:8]) for l in await _logs(db)]

    paused, logs = await _fresh(_state)
    assert paused is True
    assert logs == [("start", "rejected"), ("resume", "rejected")]


def test_start_tool_schema_requires_approval():
    tools = {t.name: t for t in leads_mcp._tool_manager.list_tools()}
    for name in ("start_campaign", "resume_campaign"):
        schema = tools[name].parameters
        assert "approved_by_safak" in schema["required"]
        assert schema["properties"]["approved_by_safak"]["type"] == "boolean"
    for name in ("list_campaigns", "create_campaign", "add_leads", "get_stats", "list_replies", "pause_campaign"):
        assert name in tools


# ── list / stats / replies ──────────────────────────────────────────────────

async def test_list_campaigns_stats_and_replies(session):
    c = await make_campaign(session, name="Stats")
    l1 = await make_lead(session, email="r@x.de", name="R")
    l2 = await make_lead(session, email="b@x.de")
    cl1 = await make_campaign_lead(session, c.id, l1.id)
    cl2 = await make_campaign_lead(session, c.id, l2.id)
    cl1.interest_status = "interested"
    cl2.enrollment_status = "bounced"
    e1 = await make_email_log(session, l1.id, c.id)
    e1.opened = True
    e1.clicked = True
    await make_email_log(session, l2.id, c.id)
    session.add(LeadReply(lead_id=l1.id, campaign_id=c.id))
    await session.flush()

    camps = await list_campaigns_core(session)
    assert camps[0]["leads"] == 2 and camps[0]["sent"] == 2 and camps[0]["status"] == "active"

    st = await get_stats_core(session, c.id)
    assert (st["sent"], st["opens"], st["clicks"], st["replies"], st["bounces"]) == (2, 1, 1, 1, 1)
    assert (await get_stats_core(session))["sent"] == 2

    rep = await list_replies_core(session, campaign_id=c.id, since="2000-01-01")
    assert rep["count"] == 1
    assert rep["replies"][0]["lead"]["email"] == "r@x.de"
    assert rep["replies"][0]["classification"] == "interested"
    assert (await list_replies_core(session, since="2999-01-01"))["count"] == 0
