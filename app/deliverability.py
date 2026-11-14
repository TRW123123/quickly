"""Deliverability defaults for outgoing campaign mail.

* ``QUICKLY_PLAINTEXT_ONLY`` (default ``true``): every outgoing mail is sent as
  a single ``text/plain`` part.  HTML bodies are converted to text (links are
  kept as ``text (url)``), open/click tracking is skipped because it needs HTML.
  Set to ``0``/``false``/``no``/``off`` to restore the previous HTML behaviour.
* ``QUICKLY_MAX_LINKS_PER_MAIL`` (default ``1``): maximum number of distinct
  http(s) URLs in a sequence step body.  The unsubscribe link Quickly adds
  (``{{unsubscribe_link}}`` / footer / header) does not count.
* Images are never allowed in a step body: ``<img>``, markdown images and
  ``data:`` URIs.

The content guard runs when a step is saved (REST + MCP, 400 / ToolError) and
again, fail-closed, right before a campaign mail is sent (the slot stays in the
queue and a reason is logged).
"""
from __future__ import annotations

import html as _html
import os
import re
from typing import Iterable

DEFAULT_MAX_LINKS_PER_MAIL = 1

_FALSE_VALUES = {"0", "false", "no", "off"}


def plaintext_only() -> bool:
    """``QUICKLY_PLAINTEXT_ONLY`` — default true."""
    raw = (os.getenv("QUICKLY_PLAINTEXT_ONLY") or "").strip().lower()
    if not raw:
        return True
    return raw not in _FALSE_VALUES


def max_links_per_mail() -> int:
    """``QUICKLY_MAX_LINKS_PER_MAIL`` — default 1, never negative."""
    raw = (os.getenv("QUICKLY_MAX_LINKS_PER_MAIL") or "").strip()
    if not raw:
        return DEFAULT_MAX_LINKS_PER_MAIL
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_LINKS_PER_MAIL


# ── Content guard ───────────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.IGNORECASE)
_IMG_TAG_RE = re.compile(r"<\s*img\b", re.IGNORECASE)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_DATA_URI_RE = re.compile(r"\bdata:[a-z]+/[a-z0-9.+-]+[;,]", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?"


def _norm_url(url: str) -> str:
    return _html.unescape(url).rstrip(_TRAILING_PUNCT).rstrip("/").lower()


def find_links(body: str, ignore_urls: Iterable[str] = ()) -> list[str]:
    """Distinct http(s) URLs in *body*, minus *ignore_urls* (e.g. unsubscribe)."""
    ignored = {_norm_url(u) for u in ignore_urls if u}
    seen: dict[str, str] = {}
    for m in _URL_RE.finditer(body or ""):
        key = _norm_url(m.group(0))
        if not key or key in ignored:
            continue
        seen.setdefault(key, m.group(0).rstrip(_TRAILING_PUNCT))
    return list(seen.values())


def content_violation(body: str | None, ignore_urls: Iterable[str] = ()) -> str | None:
    """Return a human-readable reason when *body* breaks the content rules, else None."""
    text = body or ""
    if _IMG_TAG_RE.search(text) or _MD_IMAGE_RE.search(text) or _DATA_URI_RE.search(text):
        return "images are not allowed in mail bodies (<img>, markdown images, data: URIs)"
    limit = max_links_per_mail()
    links = find_links(text, ignore_urls)
    if len(links) > limit:
        return (
            f"too many links: {len(links)} found, at most {limit} allowed "
            f"(the unsubscribe link is added automatically and does not count)"
        )
    return None


def first_violation(bodies: Iterable[str | None]) -> str | None:
    for b in bodies:
        reason = content_violation(b)
        if reason:
            return reason
    return None


# ── HTML → plain text ───────────────────────────────────────────────────────

_ANCHOR_RE = re.compile(
    r"<a\b[^>]*?\bhref\s*=\s*([\"'])(.*?)\1[^>]*>(.*?)</a\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DROP_BLOCK_RE = re.compile(r"<(head|style|script)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_BLOCK_RE = re.compile(r"</?(p|div|br|tr|h[1-6]|ul|ol|table|blockquote)\b[^>]*>", re.IGNORECASE)
_LI_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def looks_like_html(body: str | None, is_html_flag: bool | None = None) -> bool:
    if is_html_flag is not None:
        return bool(is_html_flag)
    return bool(re.search(r"<[a-zA-Z][^>]*>", body or ""))


def html_to_plain(html: str | None) -> str:
    """Convert an HTML fragment/document to readable plain text, keeping link targets."""
    text = _DROP_BLOCK_RE.sub("", html or "")

    def _anchor(m: re.Match) -> str:
        href = _html.unescape(m.group(2).strip())
        label = _html.unescape(_TAG_RE.sub("", m.group(3))).strip()
        if not href or (label and href.lower().startswith(("mailto:", "#"))):
            return label or href
        if not label or label.rstrip("/") == href.rstrip("/"):
            return href
        return f"{label} ({href})"

    text = _ANCHOR_RE.sub(_anchor, text)
    text = _LI_RE.sub("\n- ", text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
