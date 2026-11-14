"""Reply inbox: read model of lead replies for external bots.

The ``sync_reply_inbox`` job copies new ``lead_reply`` rows idempotently into
``reply_inbox`` (upsert keyed on ``lead_reply_id``), enriched with sender,
subject, a plain-text snippet and a classification.  External bots read that
table directly in Postgres and move ``status`` from ``neu`` to ``gesehen`` /
``erledigt``.

SAFETY: this module only READS mail mirrors and WRITES the ``reply_inbox`` row
plus ``campaign_lead.interest_status``.  It must never send, answer or queue an
email.  Do not import ``app.sender``, ``app.jobs`` or any provider send helper
here; ``tests/test_reply_inbox.py`` enforces that.

Classification
--------------
1. If Quickly already classified the reply (AI classifier or manual edit on the
   campaign lead: ``interest_status`` / ``enrollment_status``), that value is used
   (``classification_source = 'quickly'``).  No LLM is called from this job.
2. Otherwise a deterministic keyword heuristic (German + English) runs on subject
   and the unquoted reply text: ``bounce``, ``out_of_office``, ``auto_reply``,
   ``unsubscribed``, ``not_interested`` or ``unklassifiziert_echt``
   (``classification_source = 'heuristik'``).  The heuristic never claims a reply
   is positive.

Decision for automatic mail: out-of-office, auto-replies and bounces ARE stored,
but with ``status = 'auto'`` and ``is_positive = False``.  Bots should filter on
``status = 'neu'``; the rows stay visible for auditing and can be reclassified
when Quickly's AI result arrives later.

``is_positive``: True only for ``interested``; False for negative or automatic
classes; NULL for ``unklassifiziert_echt`` (real reply, nobody judged it yet).
Positive rows also make sure ``campaign_lead.interest_status = 'interested'``.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import database as _database
from app import time as time_provider
from app.models import (
    CampaignLead,
    EmailLog,
    GmailMessage,
    Inbox,
    Lead,
    LeadReply,
    Office365Message,
    ReplyInbox,
    SmtpMessage,
)

log = logging.getLogger("quickly.reply_inbox")

SYNC_INTERVAL_MINUTES = 10
BATCH_SIZE = 500
SNIPPET_MAX = 500
# A reply whose mail is not mirrored yet is retried for this long before it is
# stored without subject/snippet.
MESSAGE_GRACE = timedelta(hours=2)
# Heuristic rows younger than this are re-checked for a later Quickly/AI result.
RECLASSIFY_WINDOW = timedelta(days=7)

STATUS_NEW = "neu"
STATUS_AUTO = "auto"
ALLOWED_STATUSES = ("neu", "gesehen", "erledigt", "auto")

UNCLASSIFIED_REAL = "unklassifiziert_echt"
AUTO_CLASSES = frozenset({"out_of_office", "auto_reply", "bounce"})
NEGATIVE_CLASSES = frozenset({"not_interested", "unsubscribed", "wrong_person"})

# ── heuristic ────────────────────────────────────────────────────────────────

_MAILER_DAEMON_RE = re.compile(r"^(mailer-daemon|postmaster|mail-daemon)@", re.IGNORECASE)

_BOUNCE_RE = re.compile(
    r"mail delivery (subsystem|failed|system)|delivery status notification"
    r"|undeliver(able|ed)|unzustellbar|nicht zugestellt|zustellung fehlgeschlagen"
    r"|returned mail|delivery has failed|delivery failure|mailer-daemon"
    r"|message not delivered|nachricht konnte nicht zugestellt",
    re.IGNORECASE,
)
_OOO_RE = re.compile(
    r"abwesenheit|abwesend|out of (the )?office|out-of-office|\booo\b"
    r"|nicht im (büro|buero|haus)|au(ß|ss)er haus|im urlaub|urlaubsbedingt"
    r"|on (vacation|holiday|leave|annual leave)|away from (the|my) (office|desk)"
    r"|elternzeit|krankheitsbedingt|limited access to (my )?e-?mail",
    re.IGNORECASE,
)
_AUTO_RE = re.compile(
    r"automatische antwort|automatisch (generiert|erstellt)|automatic reply|auto-?reply"
    r"|autoresponder|automated (response|reply|message)|this is an automated"
    r"|dies ist eine automatische|eingangsbest(ä|ae)tigung|wir haben ihre (nachricht|anfrage|e-?mail) erhalten"
    r"|we have received your (message|request|e-?mail)|do not reply to this|bitte antworten sie nicht",
    re.IGNORECASE,
)
_UNSUB_RE = re.compile(
    r"abmelden|abmeldung|austragen|unsubscribe|remove me|opt[- ]?out|take me off"
    r"|entfernen sie mich|nehmen sie mich aus|keine (weiteren )?(e-?mails|mails|nachrichten) mehr"
    r"|keine weiteren (e-?mails|mails|nachrichten)|nicht mehr (kontaktieren|anschreiben)"
    r"|do not (contact|email) me|stop (emailing|contacting|sending)|^\s*stop\b",
    re.IGNORECASE,
)
_NOT_INTERESTED_RE = re.compile(
    r"kein(en)? interesse|kein(en)? bedarf|nicht interessiert|nein,? danke|kein thema f(ü|ue)r uns"
    r"|not interested|no interest|no,? thank(s| you)|not a (good )?fit"
    r"|we('re| are) not looking|brauchen wir nicht|sind (bereits |schon )?versorgt",
    re.IGNORECASE,
)

# Quoted history (our own outreach incl. its unsubscribe footer) must not feed
# the heuristic or the snippet.
_QUOTE_START_RE = re.compile(
    r"(^|\n)[ \t]*>"
    r"|(^|\n)[ \t]*-{3,}"
    r"|(^|\n)[ \t]*_{5,}"
    r"|(^|\n)[ \t]*(von|from|gesendet|sent)[ \t]*:[^\n]*\n"
    r"|\bOn .{5,200}?wrote:"
    r"|\bAm .{5,200}?schrieb[^\n]*:",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_DROP_RE = re.compile(r"<(style|script|head)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_QUOTE_RE = re.compile(r"<blockquote[^>]*>.*?</blockquote>", re.IGNORECASE | re.DOTALL)
_HTML_BREAK_RE = re.compile(r"<(br|/p|/div|/tr|/li)[^>]*>", re.IGNORECASE)


def html_to_text(raw: str) -> str:
    text = _HTML_DROP_RE.sub(" ", raw or "")
    text = _HTML_QUOTE_RE.sub(" ", text)
    text = _HTML_BREAK_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    return _html.unescape(text)


def strip_quoted(text: str) -> str:
    m = _QUOTE_START_RE.search(text or "")
    return text[: m.start()].rstrip() if m else (text or "")


def reply_text(body_plain: str, body_html: str, fallback: str = "") -> str:
    """Unquoted plain text of a reply (not truncated)."""
    if body_plain and body_plain.strip():
        base = body_plain
    elif body_html and body_html.strip():
        base = html_to_text(body_html)
    else:
        base = html_to_text(fallback) if "<" in (fallback or "") else _html.unescape(fallback or "")
    return strip_quoted(base).strip()


def make_snippet(text: str) -> str:
    return " ".join((text or "").split())[:SNIPPET_MAX]


def classify_heuristic(
    subject: str,
    text: str,
    from_email: str = "",
    headers: dict[str, str] | None = None,
) -> str:
    """Deterministic fallback classification.  Never returns a positive class."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    subject = subject or ""
    text = text or ""
    both = f"{subject}\n{text}"

    if _MAILER_DAEMON_RE.match((from_email or "").strip()) or _BOUNCE_RE.search(both):
        return "bounce"
    if _OOO_RE.search(both):
        return "out_of_office"
    auto_submitted = headers.get("auto-submitted", "").strip().lower()
    if (
        (auto_submitted and auto_submitted != "no")
        or "x-autoreply" in headers
        or "x-autorespond" in headers
        or headers.get("precedence", "").strip().lower() in {"auto_reply", "bulk", "junk"}
        or _AUTO_RE.search(both)
    ):
        return "auto_reply"
    if _UNSUB_RE.search(text) or _UNSUB_RE.search(subject):
        return "unsubscribed"
    if _NOT_INTERESTED_RE.search(both):
        return "not_interested"
    return UNCLASSIFIED_REAL


def quickly_classification(cl: CampaignLead | None) -> str | None:
    """Classification Quickly already stored on the campaign lead (AI or manual)."""
    if cl is None:
        return None
    if cl.enrollment_status in ("unsubscribed", "wrong_person"):
        return cl.enrollment_status
    if cl.interest_status:
        return cl.interest_status
    return None


def derive_flags(classification: str) -> tuple[bool | None, str]:
    """(is_positive, status) for a freshly classified row."""
    if classification == "interested":
        return True, STATUS_NEW
    if classification in AUTO_CLASSES:
        return False, STATUS_AUTO
    if classification in NEGATIVE_CLASSES:
        return False, STATUS_NEW
    if classification == UNCLASSIFIED_REAL:
        return None, STATUS_NEW
    # unknown value written by someone else: show it, do not guess polarity
    return None, STATUS_NEW


# ── message lookup ───────────────────────────────────────────────────────────

def _gmail_headers(headers_json: str) -> dict[str, str]:
    try:
        rows = json.loads(headers_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return {}
    out: dict[str, str] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("name"):
                out.setdefault(str(row["name"]).lower(), str(row.get("value", "")))
    return out


def _ms_to_dt(ms: int | None) -> datetime | None:
    if not ms:
        return None
    try:
        return datetime.utcfromtimestamp(ms / 1000.0)
    except (OverflowError, OSError, ValueError):
        return None


async def _find_reply_message(
    db: AsyncSession, email: str, not_before: datetime | None
) -> dict[str, Any] | None:
    """Earliest inbound mail from *email* at/after *not_before* (else the latest one).

    Looks into the Gmail, Office 365 and IMAP mirrors the Unibox keeps.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    candidates: list[dict[str, Any]] = []

    gm_rows = await db.execute(
        select(GmailMessage)
        .where(
            func.lower(GmailMessage.headers_json).like(f"%{email}%"),
            ~GmailMessage.label_ids_json.like('%"SENT"%'),
        )
        .order_by(GmailMessage.internal_date.desc())
        .limit(50)
    )
    for gm in gm_rows.scalars().all():
        headers = _gmail_headers(gm.headers_json)
        name, addr = parseaddr(headers.get("from", ""))
        if addr.strip().lower() != email:
            continue
        candidates.append({
            "inbox_id": gm.inbox_id,
            "received_at": _ms_to_dt(gm.internal_date),
            "subject": headers.get("subject", ""),
            "from_email": addr.strip().lower(),
            "from_name": name.strip(),
            "text": reply_text(gm.body_plain or "", gm.body_html or "", gm.snippet or ""),
            "headers": headers,
        })

    o_rows = await db.execute(
        select(Office365Message)
        .where(func.lower(Office365Message.from_address) == email)
        .order_by(Office365Message.received_at.desc())
        .limit(50)
    )
    for om in o_rows.scalars().all():
        candidates.append({
            "inbox_id": om.inbox_id,
            "received_at": om.received_at,
            "subject": om.subject or "",
            "from_email": email,
            "from_name": "",
            "text": reply_text(om.body_plain or "", om.body_html or ""),
            "headers": {},
        })

    s_rows = await db.execute(
        select(SmtpMessage)
        .where(
            func.lower(SmtpMessage.from_address) == email,
            SmtpMessage.direction == "received",
        )
        .order_by(SmtpMessage.received_at.desc())
        .limit(50)
    )
    for sm in s_rows.scalars().all():
        name, addr = parseaddr(sm.from_address or "")
        candidates.append({
            "inbox_id": sm.inbox_id,
            "received_at": sm.received_at,
            "subject": sm.subject or "",
            "from_email": (addr or email).strip().lower(),
            "from_name": name.strip(),
            "text": reply_text(sm.body_plain or "", sm.body_html or ""),
            "headers": {},
        })

    if not candidates:
        return None
    dated = [c for c in candidates if c["received_at"] is not None]
    if not_before is not None:
        after = [c for c in dated if c["received_at"] >= not_before]
        if after:
            return min(after, key=lambda c: c["received_at"])
    if dated:
        return max(dated, key=lambda c: c["received_at"])
    return candidates[0]


def _company(lead: Lead) -> str | None:
    data = lead.custom_data if isinstance(lead.custom_data, dict) else {}
    for key in ("company", "Company", "firma", "Firma", "company_name", "companyName", "organization"):
        val = data.get(key)
        if val not in (None, ""):
            return str(val)[:255]
    return None


async def _mark_positive(db: AsyncSession, cl: CampaignLead | None) -> None:
    if cl is not None and cl.interest_status != "interested":
        cl.interest_status = "interested"


def _upsert_stmt(db: AsyncSession, values: dict[str, Any]):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:  # pragma: no cover - only Postgres and SQLite are supported
        raise RuntimeError(f"reply_inbox upsert not supported on {dialect}")
    return _insert(ReplyInbox).values(**values).on_conflict_do_nothing(
        index_elements=["lead_reply_id"]
    )


# ── sync ─────────────────────────────────────────────────────────────────────

async def sync_reply_inbox(db: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Copy new lead replies into ``reply_inbox`` and refresh late classifications.

    Idempotent: running it twice inserts nothing new.  Never sends anything.
    """
    now = now or time_provider.utcnow()
    stats = {"inserted": 0, "deferred": 0, "reclassified": 0}

    pending = await db.execute(
        select(LeadReply, Lead, CampaignLead)
        .join(Lead, Lead.id == LeadReply.lead_id)
        .outerjoin(
            CampaignLead,
            and_(
                CampaignLead.lead_id == LeadReply.lead_id,
                CampaignLead.campaign_id == LeadReply.campaign_id,
            ),
        )
        .outerjoin(ReplyInbox, ReplyInbox.lead_reply_id == LeadReply.id)
        .where(ReplyInbox.id.is_(None))
        .order_by(LeadReply.id.asc())
        .limit(BATCH_SIZE)
    )
    inbox_emails: dict[int, str] = {}

    for lr, lead, cl in pending.all():
        first_sent = (
            await db.execute(
                select(func.min(EmailLog.sent_at)).where(
                    EmailLog.lead_id == lr.lead_id, EmailLog.campaign_id == lr.campaign_id
                )
            )
        ).scalar()
        msg = await _find_reply_message(db, lead.email, first_sent)
        replied_at = lr.replied_at or now
        if msg is None and now - replied_at < MESSAGE_GRACE:
            stats["deferred"] += 1  # mail mirror not there yet; next run retries
            continue
        msg = msg or {}

        q_class = quickly_classification(cl)
        if q_class:
            classification, source = q_class, "quickly"
        else:
            classification = classify_heuristic(
                msg.get("subject", ""), msg.get("text", ""),
                msg.get("from_email", ""), msg.get("headers"),
            )
            source = "heuristik"
        is_positive, status = derive_flags(classification)

        inbox_id = msg.get("inbox_id")
        if inbox_id is not None and inbox_id not in inbox_emails:
            inbox_emails[inbox_id] = (
                await db.execute(select(Inbox.email).where(Inbox.id == inbox_id))
            ).scalar() or ""

        values = {
            "lead_reply_id": lr.id,
            "lead_id": lr.lead_id,
            "campaign_id": lr.campaign_id,
            "inbox_id": inbox_id,
            "inbox_email": inbox_emails.get(inbox_id) if inbox_id is not None else None,
            "from_email": (msg.get("from_email") or lead.email or "").lower()[:255],
            "from_name": (msg.get("from_name") or lead.name or "")[:255],
            "company": _company(lead),
            "subject": msg.get("subject", "") or "",
            "snippet": make_snippet(msg.get("text", "")),
            "received_at": msg.get("received_at") or lr.replied_at,
            "classification": classification,
            "classification_source": source,
            "is_positive": is_positive,
            "status": status,
            "created_at": now,
            "updated_at": now,
        }
        result = await db.execute(_upsert_stmt(db, values))
        if result.rowcount:
            stats["inserted"] += 1
            if is_positive:
                await _mark_positive(db, cl)

    # Quickly's AI classifier runs in the background after reply detection; when
    # it lands after our heuristic, adopt it.  Bot-owned states are not touched.
    late = await db.execute(
        select(ReplyInbox, CampaignLead)
        .join(
            CampaignLead,
            and_(
                CampaignLead.lead_id == ReplyInbox.lead_id,
                CampaignLead.campaign_id == ReplyInbox.campaign_id,
            ),
        )
        .where(
            ReplyInbox.classification_source == "heuristik",
            ReplyInbox.created_at >= now - RECLASSIFY_WINDOW,
            or_(
                CampaignLead.interest_status.is_not(None),
                CampaignLead.enrollment_status.in_(("unsubscribed", "wrong_person")),
            ),
        )
    )
    for row, cl in late.all():
        q_class = quickly_classification(cl)
        if not q_class:
            continue
        is_positive, status = derive_flags(q_class)
        row.classification = q_class
        row.classification_source = "quickly"
        row.is_positive = is_positive
        if row.status in (STATUS_NEW, STATUS_AUTO):
            row.status = status
        row.updated_at = now
        stats["reclassified"] += 1

    await db.commit()
    return stats


async def run_reply_inbox_sync_job() -> None:
    """APScheduler entry point (``sync_reply_inbox``, every 10 minutes)."""
    try:
        async with _database.AsyncSessionLocal() as db:
            stats = await sync_reply_inbox(db)
        if any(stats.values()):
            log.info("reply_inbox sync: %s", stats)
    except Exception:
        log.exception("reply_inbox sync failed")
