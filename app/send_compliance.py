"""Compliance and warm-up guards for campaign (cold) sending.

Centralises the rules that apply to every outgoing *campaign* mail, regardless
of how an inbox is configured:

* a hard minimum gap between two mails of the same inbox
  (``MIN_WAIT_MINUTES_BETWEEN``),
* a global per-inbox daily cap (env ``QUICKLY_MAX_PER_INBOX_PER_DAY``,
  default 30) that is enforced on top of the inbox's own limit,
* a mandatory imprint footer (AppSetting ``compliance_footer_text`` with env
  fallback ``QUICKLY_FOOTER_TEXT``) plus unsubscribe link; without a footer
  campaign mails are not sent (fail-closed),
* a blocklist lookup (table ``blocklist``, owned by another module) by full
  address and by domain.

Manual Unibox replies do not go through this module.
"""
from __future__ import annotations

import html as _html
import logging
import os
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("quickly.send_compliance")

MIN_WAIT_MINUTES_BETWEEN = 3
# Upper bound per inbox and day.  The weekly warm-up schedule (app/warmup.py)
# governs below it; 30 is the highest plateau the owner allows.
DEFAULT_MAX_PER_INBOX_PER_DAY = 30
FOOTER_SETTING_KEY = "compliance_footer_text"
FOOTER_MAX_LENGTH = 4000

_blocklist_warned = False  # log a missing/broken blocklist table once per process


# ── Rate / volume limits ────────────────────────────────────────────────────

def global_max_per_inbox_per_day() -> int:
    """Global per-inbox daily cap from ``QUICKLY_MAX_PER_INBOX_PER_DAY``."""
    raw = os.getenv("QUICKLY_MAX_PER_INBOX_PER_DAY", "").strip()
    if not raw:
        return DEFAULT_MAX_PER_INBOX_PER_DAY
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "QUICKLY_MAX_PER_INBOX_PER_DAY=%r is not an integer; using %d",
            raw, DEFAULT_MAX_PER_INBOX_PER_DAY,
        )
        return DEFAULT_MAX_PER_INBOX_PER_DAY
    return max(0, value)


def effective_wait_minutes(inbox) -> int:
    """Inbox wait time, never below ``MIN_WAIT_MINUTES_BETWEEN``."""
    configured = getattr(inbox, "wait_minutes_between", None) or 0
    return max(int(configured), MIN_WAIT_MINUTES_BETWEEN)


# ── Footer (imprint + unsubscribe) ──────────────────────────────────────────

async def get_footer_text(db: AsyncSession) -> str:
    """Imprint text from the settings table, falling back to the env var."""
    from app.app_settings import get_setting

    value = None
    try:
        value = await get_setting(db, FOOTER_SETTING_KEY)
    except Exception:
        log.exception("Could not read %s from settings", FOOTER_SETTING_KEY)
    if value and value.strip():
        return value.strip()
    return env_footer_text()


def env_footer_text() -> str:
    """``QUICKLY_FOOTER_TEXT``; a literal backslash-n counts as a line break."""
    return (os.getenv("QUICKLY_FOOTER_TEXT") or "").replace("\\n", "\n").strip()


async def set_footer_text(db: AsyncSession, value: str) -> None:
    from app.app_settings import put_setting

    await put_setting(db, FOOTER_SETTING_KEY, (value or "").strip())


_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def body_contains_unsubscribe(body: str, unsub_url: str) -> bool:
    """True if the rendered body already carries the unsubscribe link."""
    if not body or not unsub_url:
        return False
    return unsub_url in body or _html.escape(unsub_url) in body


def append_compliance_footer(
    body: str,
    *,
    is_html: bool,
    footer_text: str,
    unsub_url: str,
    include_unsubscribe: bool,
) -> str:
    """Append imprint (and optionally the unsubscribe link) at the end of *body*.

    For HTML bodies the footer is inserted before ``</body>`` when present so
    it stays inside the document; the plain-text alternative is derived from
    the HTML by the sender, so it carries the footer too.
    """
    footer_text = (footer_text or "").strip()
    if is_html:
        lines = "<br>".join(_html.escape(line) for line in footer_text.splitlines())
        parts = [
            '<div class="quickly-footer" style="margin-top:24px;padding-top:12px;'
            'border-top:1px solid #dddddd;font-size:12px;line-height:1.4;color:#666666;">'
        ]
        parts.append(f"<div>{lines}</div>")
        if include_unsubscribe:
            safe_url = _html.escape(unsub_url, quote=True)
            parts.append(
                f'<div>Keine weiteren E-Mails erhalten: <a href="{safe_url}">Abmelden</a></div>'
            )
        parts.append("</div>")
        footer_html = "".join(parts)
        matches = list(_BODY_CLOSE_RE.finditer(body or ""))
        if matches:
            pos = matches[-1].start()
            return body[:pos] + footer_html + body[pos:]
        return (body or "") + footer_html

    text_footer = "\n\n--\n" + footer_text
    if include_unsubscribe:
        text_footer += f"\n\nKeine weiteren E-Mails erhalten: {unsub_url}"
    return (body or "").rstrip() + text_footer


def apply_footer_to_body(
    body: str,
    *,
    is_html: bool,
    footer_text: str,
    unsub_url: str,
    template_body: str | None = None,
) -> str:
    """Append the footer; skip the extra unsubscribe link if the mail has one.

    *template_body* is the rendered template content (before tracking / quoted
    history); when given, it decides whether the author already placed
    ``{{unsubscribe_link}}`` in the mail.
    """
    probe = body if template_body is None else template_body
    include_unsub = not body_contains_unsubscribe(probe, unsub_url)
    return append_compliance_footer(
        body,
        is_html=is_html,
        footer_text=footer_text,
        unsub_url=unsub_url,
        include_unsubscribe=include_unsub,
    )


# ── Blocklist ───────────────────────────────────────────────────────────────

async def is_blocklisted(db: AsyncSession, email: str) -> str | None:
    """Return the matching blocklist value for *email* (address or domain).

    Returns ``None`` when nothing matches or when the ``blocklist`` table does
    not exist / cannot be queried (the check is skipped, never crashes the
    send). The query runs inside a SAVEPOINT so a failing statement does not
    abort the surrounding Postgres transaction.
    """
    addr = (email or "").strip().lower()
    if not addr:
        return None
    domain = addr.rsplit("@", 1)[1] if "@" in addr else ""
    stmt = text(
        "SELECT value FROM blocklist "
        "WHERE (kind = 'email' AND value = :addr) "
        "OR (kind = 'domain' AND value = :domain) "
        "LIMIT 1"
    )
    try:
        async with db.begin_nested():
            res = await db.execute(stmt, {"addr": addr, "domain": domain or "\x00"})
            row = res.first()
    except Exception as exc:  # table missing or other DB error
        global _blocklist_warned
        level = logging.DEBUG if _blocklist_warned else logging.WARNING
        _blocklist_warned = True
        log.log(level, "Blocklist check skipped (%s: %s)", type(exc).__name__, exc)
        return None
    return row[0] if row else None
