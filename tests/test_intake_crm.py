"""Free address check at lead import (app/email_check.py) and the CRM job (app/crm_leads.py).

DNS is always mocked: ``app.email_check._dns_lookup`` is replaced, and for the
resolver-level tests ``dns.asyncresolver.Resolver.resolve`` is patched.
"""
import ast
import inspect
from datetime import datetime

import dns.resolver
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

import app.crm_leads as crm_mod
import app.email_check as ec
from app.crm_leads import sync_crm_leads
from app.mcp_campaigns import add_leads_core
from app.models import CampaignLead, CrmLead, Lead, ReplyInbox
from app.routers import crm_leads as crm_router
from app.routers.campaigns import CampaignLeadAdd, bulk_add_leads_to_campaign, import_campaign_leads
from app.routers.leads import EmailVerifyRequest, verify_email_list
from tests.conftest import make_campaign, make_lead

# domain -> lookup result used by the fake resolver
DNS_TABLE = {
    "firma.de": ec.DOMAIN_MX,
    "acme.com": ec.DOMAIN_MX,
    "nomx-a.de": ec.DOMAIN_A,
    "dead-domain.de": ec.DOMAIN_NONE,
    "flaky.de": ec.DOMAIN_UNKNOWN,
}


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch):
    calls: list[str] = []

    async def _lookup(domain: str) -> str:
        calls.append(domain)
        return DNS_TABLE.get(domain, ec.DOMAIN_MX)

    ec.clear_cache()
    monkeypatch.setattr(ec, "_dns_lookup", _lookup)

    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr("app.email_provider.detect_provider_for_email", _none)
    monkeypatch.setattr("app.routers.campaigns._detect_providers_background", _none)
    monkeypatch.setattr("app.mcp_campaigns._schedule_global_recalc", lambda: None)
    yield calls
    ec.clear_cache()


# ── syntax ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("email", [
    "max.mustermann@firma.de", "a+tag@acme.com", "x_y-z@sub.domain.co.uk", "  Max@Firma.DE ",
    "info@müller.de",
])
def test_syntax_valid(email):
    assert ec.is_valid_syntax(email)


@pytest.mark.parametrize("email", [
    "", "plain", "a@b", "@firma.de", "max@", "a..b@firma.de", ".a@firma.de", "a.@firma.de",
    "a@-firma.de", "a@firma-.de", "a@firma..de", "a b@firma.de", "a@@firma.de", "a@firma.d3",
    "x" * 65 + "@firma.de",
])
def test_syntax_invalid(email):
    assert not ec.is_valid_syntax(email)


# ── resolver-level: MX present / absent / A fallback / errors ────────────────

class _MX:
    def __init__(self, host):
        import dns.name
        self.exchange = dns.name.from_text(host)
        self.preference = 10


def _patch_resolver(monkeypatch, answers: dict):
    """answers: (domain, rtype) -> list | exception class."""
    import dns.asyncresolver

    async def _resolve(self, qname, rdtype="A", *a, **k):
        val = answers.get((str(qname), rdtype), dns.resolver.NoAnswer)
        if isinstance(val, type) and issubclass(val, BaseException):
            raise val()
        return val

    monkeypatch.setattr(dns.asyncresolver.Resolver, "resolve", _resolve)


async def _real_lookup(monkeypatch, answers, domain):
    # undo the module-level fake, keep the network mocked at resolver level
    monkeypatch.setattr(ec, "_dns_lookup", _ORIGINAL_LOOKUP)
    _patch_resolver(monkeypatch, answers)
    return await ec._dns_lookup(domain)


_ORIGINAL_LOOKUP = inspect.getattr_static(ec, "_dns_lookup")


async def test_lookup_mx_present(monkeypatch):
    r = await _real_lookup(monkeypatch, {("firma.de", "MX"): [_MX("mx.firma.de.")]}, "firma.de")
    assert r == ec.DOMAIN_MX


async def test_lookup_no_mx_falls_back_to_a(monkeypatch):
    r = await _real_lookup(monkeypatch, {("x.de", "MX"): dns.resolver.NoAnswer, ("x.de", "A"): ["1.2.3.4"]}, "x.de")
    assert r == ec.DOMAIN_A


async def test_lookup_no_mx_aaaa_only(monkeypatch):
    r = await _real_lookup(monkeypatch, {("x.de", "AAAA"): ["::1"]}, "x.de")
    assert r == ec.DOMAIN_A


async def test_lookup_no_mx_no_a(monkeypatch):
    assert await _real_lookup(monkeypatch, {}, "x.de") == ec.DOMAIN_NONE


async def test_lookup_nxdomain(monkeypatch):
    assert await _real_lookup(monkeypatch, {("x.de", "MX"): dns.resolver.NXDOMAIN}, "x.de") == ec.DOMAIN_NONE


async def test_lookup_null_mx(monkeypatch):
    assert await _real_lookup(monkeypatch, {("x.de", "MX"): [_MX(".")]}, "x.de") == ec.DOMAIN_NONE


@pytest.mark.parametrize("exc", [dns.resolver.NoNameservers, dns.resolver.LifetimeTimeout, RuntimeError])
async def test_lookup_dns_error_is_unknown(monkeypatch, exc):
    assert await _real_lookup(monkeypatch, {("x.de", "MX"): exc}, "x.de") == ec.DOMAIN_UNKNOWN


async def test_domain_cache(_fake_dns):
    await ec.check_emails(["a@firma.de", "b@firma.de"])
    await ec.check_emails(["c@firma.de"])
    assert _fake_dns.count("firma.de") == 1


# ── combined check ───────────────────────────────────────────────────────────

async def test_check_results():
    res = await ec.check_emails([
        "max@firma.de", "info@firma.de", "Kontakt@acme.com", "no-reply@acme.com",
        "max@nomx-a.de", "max@dead-domain.de", "max@flaky.de", "max@mailinator.com",
        "max@foo.yopmail.com", "broken@@x.de",
    ])
    assert res["max@firma.de"].status == "valid" and res["max@firma.de"].flags == []
    assert res["info@firma.de"].flags == ["role"] and res["info@firma.de"].skip_reason is None
    assert "role" in res["kontakt@acme.com"].flags
    assert "role" in res["no-reply@acme.com"].flags
    a = res["max@nomx-a.de"]
    assert a.status == "valid" and a.flags == ["no_mx"] and a.skip_reason is None
    d = res["max@dead-domain.de"]
    assert d.status == "invalid" and "no_mx" in d.flags and d.skip_reason == "invalid"
    u = res["max@flaky.de"]
    assert u.status == "unknown" and u.skip_reason is None
    assert res["max@mailinator.com"].skip_reason == "disposable"
    assert res["max@foo.yopmail.com"].skip_reason == "disposable"
    assert res["broken@@x.de"].status == "invalid" and res["broken@@x.de"].flags == ["syntax"]


def test_role_detection():
    for e in ("info@x.de", "buchhaltung@x.de", "bewerbung@x.de", "sales+eu@x.de", "do-not-reply@x.de",
              "office@x.de", "team@x.de", "hello@x.de", "service@x.de", "mail@x.de", "admin@x.de"):
        assert ec.is_role_address(e), e
    for e in ("max.mustermann@x.de", "information.officer.bob@x.de", "infomax@x.de"):
        assert not ec.is_role_address(e), e


def test_disposable_list_loaded():
    lst = ec.disposable_domains()
    assert len(lst) >= 300
    assert "gmail.com" not in lst and "web.de" not in lst and "gmx.de" not in lst


async def test_verify_endpoint_does_not_import(session):
    out = await verify_email_list(EmailVerifyRequest(emails=["max@firma.de", "max@mailinator.com", "x"]))
    assert out["importable"] == 1
    assert out["summary"]["disposable"]["count"] == 1
    assert out["summary"]["invalid"]["count"] == 1
    assert (await session.execute(select(func.count(Lead.id)))).scalar() == 0


# ── import paths ─────────────────────────────────────────────────────────────

INPUT = [
    "max@firma.de", "info@firma.de", "max@nomx-a.de", "max@dead-domain.de",
    "max@mailinator.com", "max@flaky.de",
]


def _assert_policy(added_emails, session_leads):
    assert set(added_emails) == {"max@firma.de", "info@firma.de", "max@nomx-a.de", "max@flaky.de"}
    by_email = {l.email: l for l in session_leads}
    assert "max@dead-domain.de" not in by_email and "max@mailinator.com" not in by_email
    assert by_email["info@firma.de"].email_check_flags == "role"
    assert by_email["info@firma.de"].email_check_status == "valid"
    assert by_email["max@nomx-a.de"].email_check_flags == "no_mx"
    assert by_email["max@flaky.de"].email_check_status == "unknown"
    assert by_email["max@firma.de"].email_checked_at is not None


async def test_rest_bulk_add_applies_policy(session):
    campaign = await make_campaign(session)
    res = await bulk_add_leads_to_campaign(
        campaign_id=campaign.id,
        leads_data=[CampaignLeadAdd(email=e) for e in INPUT],
        db=session,
    )
    added = [r["email"] for r in res["results"] if r["status"] == "added"]
    leads = (await session.execute(select(Lead))).scalars().all()
    _assert_policy(added, leads)
    rep = res["email_check"]
    assert rep["invalid"]["emails"] == ["max@dead-domain.de"]
    assert rep["disposable"]["emails"] == ["max@mailinator.com"]
    assert rep["role"]["count"] == 1
    assert set(rep["no_mx"]["emails"]) == {"max@nomx-a.de", "max@dead-domain.de"}
    assert rep["unknown"]["emails"] == ["max@flaky.de"]
    assert res["errors"] == 0


async def test_rest_bulk_add_preview_reports_checks(session):
    campaign = await make_campaign(session)
    res = await bulk_add_leads_to_campaign(
        campaign_id=campaign.id, leads_data=[CampaignLeadAdd(email=e) for e in INPUT],
        confirm_only=True, db=session,
    )
    assert res["total_valid"] == 4
    assert res["email_check"]["disposable"]["count"] == 1
    assert (await session.execute(select(func.count(Lead.id)))).scalar() == 0


async def test_rest_bulk_add_check_can_be_disabled(session):
    campaign = await make_campaign(session)
    res = await bulk_add_leads_to_campaign(
        campaign_id=campaign.id, leads_data=[CampaignLeadAdd(email="max@mailinator.com")],
        email_check=False, db=session,
    )
    assert res["added"] == 1


async def test_csv_import_applies_policy(session):
    campaign = await make_campaign(session)
    body = ("email,name\n" + "\n".join(f"{e},X" for e in INPUT) + "\n").encode()

    class _Upload:
        filename = "leads.csv"

        async def read(self):
            return body

    res = await import_campaign_leads(campaign_id=campaign.id, file=_Upload(), skip_duplicates=True, db=session)
    assert res["added"] == 4
    assert res["email_check"]["invalid"]["count"] == 1
    assert res["email_check"]["disposable"]["count"] == 1
    assert res["email_check"]["role"]["count"] == 1
    assert res["total_rows"] == 6
    leads = (await session.execute(select(Lead))).scalars().all()
    _assert_policy([l.email for l in leads], leads)


async def test_mcp_add_leads_reports_counts(session):
    campaign = await make_campaign(session, paused=True)
    res = await add_leads_core(session, campaign.id, leads=INPUT + ["not-an-email"])
    assert set(res["added"]["emails"]) == {"max@firma.de", "info@firma.de", "max@nomx-a.de", "max@flaky.de"}
    assert set(res["invalid"]["emails"]) == {"not-an-email", "max@dead-domain.de"}
    assert res["disposable"] == {"count": 1, "emails": ["max@mailinator.com"]}
    assert res["role"]["count"] == 1
    assert res["no_mx"]["count"] == 2
    assert res["unknown"]["count"] == 1
    enrolled = (await session.execute(select(func.count(CampaignLead.id)))).scalar()
    assert enrolled == 4


# ── CRM job ──────────────────────────────────────────────────────────────────

async def _reply_row(session, lead, campaign, *, positive, n, from_name="", company=None):
    from app.models import LeadReply
    lr = LeadReply(lead_id=lead.id, campaign_id=campaign.id, replied_at=datetime(2026, 11, 13, 10, n))
    session.add(lr)
    await session.flush()
    row = ReplyInbox(
        lead_reply_id=lr.id, lead_id=lead.id, campaign_id=campaign.id, from_email=lead.email,
        from_name=from_name, company=company, subject="Re", snippet="", is_positive=positive,
        received_at=datetime(2026, 11, 13, 10, n), status="neu",
        created_at=datetime(2026, 11, 13, 10, n), updated_at=datetime(2026, 11, 13, 10, n),
    )
    session.add(row)
    await session.flush()
    return row


async def test_crm_job_copies_positive_once(session):
    campaign = await make_campaign(session, name="Herbst-Kampagne")
    pos = await make_lead(session, email="kim@firma.de", name="Kim K")
    pos.custom_data = {"first_name": "Kim", "last_name": "Kunde", "Firma": "Firma GmbH"}
    neg = await make_lead(session, email="no@acme.com", name="No")
    unk = await make_lead(session, email="hm@acme.com", name="Hm")
    plain = await make_lead(session, email="pat@acme.com", name="Pat Plain")
    await _reply_row(session, pos, campaign, positive=True, n=1)
    await _reply_row(session, neg, campaign, positive=False, n=2)
    await _reply_row(session, unk, campaign, positive=None, n=3)
    await _reply_row(session, plain, campaign, positive=True, n=4, company="Acme Inc")

    assert (await sync_crm_leads(session))["inserted"] == 2
    assert (await sync_crm_leads(session))["inserted"] == 0  # idempotent

    rows = (await session.execute(select(CrmLead).order_by(CrmLead.id))).scalars().all()
    assert [r.email for r in rows] == ["kim@firma.de", "pat@acme.com"]
    kim, pat = rows
    assert kim.name == "Kim Kunde" and kim.company == "Firma GmbH"
    assert kim.campaign_name == "Herbst-Kampagne" and kim.status == "neu"
    assert kim.replied_at == datetime(2026, 11, 13, 10, 1)
    assert pat.name == "Pat Plain" and pat.company == "Acme Inc"

    # owner status survives later syncs
    await crm_router.patch_crm_lead(kim.id, crm_router.CrmLeadPatch(status="kontaktiert"), db=session)
    await sync_crm_leads(session)
    lst = await crm_router.list_crm_leads(status="kontaktiert", limit=100, offset=0, db=session)
    assert lst.total == 1 and lst.items[0].email == "kim@firma.de"
    all_rows = await crm_router.list_crm_leads(status=None, limit=100, offset=0, db=session)
    assert all_rows.total == 2


async def test_crm_patch_404(session):
    with pytest.raises(HTTPException) as ei:
        await crm_router.patch_crm_lead(999, crm_router.CrmLeadPatch(status="x"), db=session)
    assert ei.value.status_code == 404


def test_crm_and_check_modules_never_send():
    for mod in (crm_mod, ec, crm_router):
        tree = ast.parse(inspect.getsource(mod))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
        assert not names & {"app.sender", "app.jobs", "smtplib", "app.smtp_utils"}, (mod.__name__, names)


def test_crm_job_registered_only_with_scheduler():
    import app.main as main_mod
    src = inspect.getsource(main_mod)
    start = src.index("if not scheduler_enabled():")
    assert src.index('id="sync_crm_leads"') > start
