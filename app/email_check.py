"""Free, self-built address check for lead import (no paid service).

Three steps, all local or plain DNS:

1. Syntax: a pragmatic RFC 5321/5322 subset (dot-atom local part, LDH domain
   labels, alphabetic TLD, length limits).  Internationalised domains are
   accepted when they encode to IDNA.
2. Domain: MX lookup via dnspython.  Without an MX record the domain's A/AAAA
   record is the implicit mail host (RFC 5321 section 5.1), so the address is
   still deliverable but flagged ``no_mx``.  A "null MX" (RFC 7505, a single
   ``0 .`` record) or a non-existent domain means the domain accepts no mail.
   DNS timeouts and server failures are reported as ``unknown`` and never as
   invalid.  Results are cached per domain.
3. Flags: disposable domains (bundled list ``app/data/disposable_domains.txt``)
   and role addresses (info@, kontakt@, noreply@ ...).

Policy at import (see ``EmailCheckResult.skip_reason``):
  * invalid syntax, or neither MX nor A/AAAA -> skip, reported as ``invalid``
  * disposable domain                         -> skip, reported as ``disposable``
  * role address                              -> import, flagged ``role``

This module only does DNS lookups.  It never connects to a mail server and
never sends anything.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from app.time import utcnow

log = logging.getLogger("quickly.email_check")

# ── statuses / flags ─────────────────────────────────────────────────────────
STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_UNKNOWN = "unknown"

FLAG_ROLE = "role"
FLAG_DISPOSABLE = "disposable"
FLAG_NO_MX = "no_mx"
FLAG_SYNTAX = "syntax"

# Domain lookup outcomes
DOMAIN_MX = "mx"            # MX record(s) present
DOMAIN_A = "a"              # no MX, but A/AAAA present (implicit MX, RFC 5321)
DOMAIN_NONE = "none"        # NXDOMAIN, null MX, or neither MX nor A/AAAA
DOMAIN_UNKNOWN = "unknown"  # timeout / SERVFAIL / no nameservers / other error

DNS_TIMEOUT_SECONDS = 2.0      # per nameserver attempt
DNS_LIFETIME_SECONDS = 3.0     # total per query
DNS_CONCURRENCY = 20
CACHE_TTL_SECONDS = 6 * 3600
CACHE_TTL_UNKNOWN_SECONDS = 10 * 60
CACHE_MAX_ENTRIES = 20000

# ── syntax ───────────────────────────────────────────────────────────────────
_ATOM = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LOCAL_RE = re.compile(rf"^{_ATOM}(?:\.{_ATOM})*$")
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _ascii_domain(domain: str) -> str | None:
    try:
        return domain.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None


def split_email(email: str) -> tuple[str, str] | None:
    e = normalize_email(email)
    if e.count("@") != 1:
        return None
    local, domain = e.split("@")
    if not local or not domain:
        return None
    return local, domain


def is_valid_syntax(email: str) -> bool:
    e = normalize_email(email)
    if not e or len(e) > 254:
        return False
    parts = split_email(e)
    if parts is None:
        return False
    local, domain = parts
    if len(local) > 64 or not _LOCAL_RE.match(local):
        return False
    ascii_domain = _ascii_domain(domain)
    if not ascii_domain or len(ascii_domain) > 253:
        return False
    labels = ascii_domain.split(".")
    if len(labels) < 2:
        return False
    if not all(_LABEL_RE.match(lbl) for lbl in labels):
        return False
    return bool(_TLD_RE.match(labels[-1]))


# ── disposable / role ────────────────────────────────────────────────────────
_DISPOSABLE_FILE = Path(__file__).resolve().parent / "data" / "disposable_domains.txt"


@lru_cache(maxsize=1)
def disposable_domains() -> frozenset[str]:
    try:
        text = _DISPOSABLE_FILE.read_text(encoding="utf-8")
    except OSError:
        log.warning("disposable domain list missing: %s", _DISPOSABLE_FILE)
        return frozenset()
    out = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            out.add(line)
    return frozenset(out)


def is_disposable_domain(domain: str) -> bool:
    d = (domain or "").strip().lower().rstrip(".")
    if not d:
        return False
    lst = disposable_domains()
    if d in lst:
        return True
    # subdomains of a listed domain (e.g. foo.mailinator.com)
    parts = d.split(".")
    return any(".".join(parts[i:]) in lst for i in range(1, len(parts) - 1))


# Role / function mailboxes (German + English).  Matched against the local part
# without "+tag", and also with "-", "_" and "." removed (no-reply == noreply).
ROLE_LOCAL_PARTS: frozenset[str] = frozenset(
    {
        # English
        "info", "information", "office", "sales", "support", "help", "helpdesk",
        "noreply", "donotreply", "dontreply", "admin", "administrator",
        "webmaster", "hostmaster", "postmaster", "abuse", "security", "mail",
        "email", "service", "services", "customerservice", "customercare",
        "hello", "team", "contact", "contactus", "enquiries", "enquiry",
        "inquiries", "inquiry", "marketing", "press", "media", "pr", "hr",
        "jobs", "careers", "career", "recruiting", "recruitment", "billing",
        "accounts", "accounting", "finance", "invoice", "invoices", "orders",
        "order", "shop", "store", "booking", "bookings", "reservations",
        "reception", "feedback", "newsletter", "news", "privacy", "legal",
        "compliance", "it", "tech", "technik", "dev", "devnull", "root",
        "mailerdaemon", "notifications", "notification", "alerts", "all",
        "everyone", "staff", "general", "partners", "partner", "events",
        "management", "board", "headoffice", "frontdesk", "welcome",
        "community", "social", "investor", "investors", "purchasing",
        "procurement", "logistics", "shipping", "returns", "service-desk",
        "servicedesk", "mailbox",
        # German
        "kontakt", "kontaktformular", "buchhaltung", "bewerbung",
        "bewerbungen", "karriere", "personal", "personalabteilung", "verwaltung",
        "vertrieb", "verkauf", "einkauf", "kundenservice", "kundendienst",
        "anfrage", "anfragen", "auftrag", "auftraege", "bestellung",
        "bestellungen", "rechnung", "rechnungen", "presse", "zentrale",
        "sekretariat", "empfang", "buero", "datenschutz", "impressum",
        "redaktion", "termine", "termin", "praxis", "kanzlei", "werkstatt",
        "service-team", "serviceteam", "geschaeftsfuehrung",
        "geschaeftsleitung", "post", "poststelle", "hallo", "willkommen",
        "ausbildung", "marketingabteilung", "lager", "versand",
        "reklamation", "mitglieder", "verein", "vorstand",
        "schule",
    }
)


def _role_key(local: str) -> str:
    base = local.split("+", 1)[0]
    return re.sub(r"[-_.]", "", base)


_ROLE_KEYS = frozenset(re.sub(r"[-_.]", "", r) for r in ROLE_LOCAL_PARTS)


def is_role_address(email_or_local: str) -> bool:
    s = normalize_email(email_or_local)
    local = s.split("@", 1)[0] if "@" in s else s
    if not local:
        return False
    if local.split("+", 1)[0] in ROLE_LOCAL_PARTS:
        return True
    return _role_key(local) in _ROLE_KEYS


# ── DNS ──────────────────────────────────────────────────────────────────────
_cache: dict[str, tuple[str, float]] = {}


def clear_cache() -> None:
    _cache.clear()


async def _dns_lookup(domain: str) -> str:
    """Resolve the mail capability of ``domain``. Returns one of DOMAIN_*.

    Tests replace this function (no network in tests).
    """
    try:
        import dns.asyncresolver
        import dns.exception
        import dns.resolver
    except ImportError:  # pragma: no cover - dnspython is a hard dependency
        return DOMAIN_UNKNOWN

    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = DNS_TIMEOUT_SECONDS
    resolver.lifetime = DNS_LIFETIME_SECONDS

    try:
        answer = await resolver.resolve(domain, "MX")
        hosts = [str(r.exchange).rstrip(".") for r in answer]
        if hosts and all(h == "" for h in hosts):
            return DOMAIN_NONE  # null MX (RFC 7505): domain accepts no mail
        return DOMAIN_MX
    except dns.resolver.NXDOMAIN:
        return DOMAIN_NONE
    except dns.resolver.NoAnswer:
        pass  # no MX: fall back to A/AAAA
    except (dns.exception.Timeout, dns.resolver.NoNameservers):
        return DOMAIN_UNKNOWN
    except Exception as exc:  # noqa: BLE001 - every other DNS problem is "unknown"
        log.debug("MX lookup failed for %s: %s", domain, exc)
        return DOMAIN_UNKNOWN

    for rtype in ("A", "AAAA"):
        try:
            await resolver.resolve(domain, rtype)
            return DOMAIN_A
        except dns.resolver.NXDOMAIN:
            return DOMAIN_NONE
        except dns.resolver.NoAnswer:
            continue
        except Exception as exc:  # noqa: BLE001
            log.debug("%s lookup failed for %s: %s", rtype, domain, exc)
            return DOMAIN_UNKNOWN
    return DOMAIN_NONE


async def check_domain(domain: str) -> str:
    """Cached domain lookup. Never raises."""
    d = (domain or "").strip().lower().rstrip(".")
    d = _ascii_domain(d) or d
    now = time.monotonic()
    hit = _cache.get(d)
    if hit and hit[1] > now:
        return hit[0]
    try:
        result = await _dns_lookup(d)
    except Exception as exc:  # noqa: BLE001
        log.debug("domain lookup crashed for %s: %s", d, exc)
        result = DOMAIN_UNKNOWN
    if result not in (DOMAIN_MX, DOMAIN_A, DOMAIN_NONE, DOMAIN_UNKNOWN):
        result = DOMAIN_UNKNOWN
    ttl = CACHE_TTL_UNKNOWN_SECONDS if result == DOMAIN_UNKNOWN else CACHE_TTL_SECONDS
    if len(_cache) >= CACHE_MAX_ENTRIES:
        _cache.clear()
    _cache[d] = (result, now + ttl)
    return result


# ── combined check ───────────────────────────────────────────────────────────
@dataclass
class EmailCheckResult:
    email: str
    status: str
    flags: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def skip_reason(self) -> str | None:
        """Why the import policy skips this address (None = import)."""
        if self.status == STATUS_INVALID:
            return "invalid"
        if FLAG_DISPOSABLE in self.flags:
            return "disposable"
        return None

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "status": self.status,
            "flags": list(self.flags),
            "reason": self.reason,
            "import": self.skip_reason is None,
        }


def _evaluate(email: str, domain_result: str | None) -> EmailCheckResult:
    e = normalize_email(email)
    if not is_valid_syntax(e):
        return EmailCheckResult(e, STATUS_INVALID, [FLAG_SYNTAX], "invalid syntax")
    local, domain = split_email(e)  # type: ignore[misc]
    flags: list[str] = []
    if is_role_address(local):
        flags.append(FLAG_ROLE)
    if is_disposable_domain(domain):
        flags.append(FLAG_DISPOSABLE)
    if domain_result == DOMAIN_NONE:
        flags.append(FLAG_NO_MX)
        return EmailCheckResult(e, STATUS_INVALID, flags, "domain accepts no mail (no MX, no A/AAAA)")
    if domain_result == DOMAIN_A:
        flags.append(FLAG_NO_MX)
        return EmailCheckResult(e, STATUS_VALID, flags, "no MX, A record fallback")
    if domain_result == DOMAIN_MX:
        return EmailCheckResult(e, STATUS_VALID, flags, "")
    return EmailCheckResult(e, STATUS_UNKNOWN, flags, "DNS lookup failed")


async def check_emails(emails: Iterable[str]) -> dict[str, EmailCheckResult]:
    """Check many addresses; one DNS lookup per distinct domain.

    Keys are the normalised (lowercased, stripped) addresses.
    """
    normed = []
    seen: set[str] = set()
    for raw in emails:
        e = normalize_email(raw)
        if e and e not in seen:
            seen.add(e)
            normed.append(e)

    domains: set[str] = set()
    for e in normed:
        if is_valid_syntax(e):
            domains.add(split_email(e)[1])  # type: ignore[index]

    sem = asyncio.Semaphore(DNS_CONCURRENCY)

    async def _one(d: str) -> tuple[str, str]:
        async with sem:
            return d, await check_domain(d)

    domain_results = dict(await asyncio.gather(*(_one(d) for d in sorted(domains))))

    out: dict[str, EmailCheckResult] = {}
    for e in normed:
        parts = split_email(e)
        dres = domain_results.get(parts[1]) if parts else None
        out[e] = _evaluate(e, dres)
    return out


async def check_email(email: str) -> EmailCheckResult:
    res = await check_emails([email])
    return res.get(normalize_email(email)) or _evaluate(email, None)


def apply_to_lead(lead, result: EmailCheckResult, now: datetime | None = None) -> None:
    """Persist a check result on a ``Lead`` row."""
    lead.email_check_status = result.status
    lead.email_check_flags = ",".join(result.flags) if result.flags else None
    lead.email_checked_at = now or utcnow()


def summarize(results: Iterable[EmailCheckResult]) -> dict:
    """Counts and addresses per category, for import responses."""
    buckets: dict[str, list[str]] = {
        "invalid": [], "disposable": [], "role": [], "no_mx": [], "unknown": [],
    }
    for r in results:
        if r.status == STATUS_INVALID:
            buckets["invalid"].append(r.email)
        if FLAG_DISPOSABLE in r.flags:
            buckets["disposable"].append(r.email)
        if FLAG_ROLE in r.flags:
            buckets["role"].append(r.email)
        if FLAG_NO_MX in r.flags:
            buckets["no_mx"].append(r.email)
        if r.status == STATUS_UNKNOWN:
            buckets["unknown"].append(r.email)
    return {k: {"count": len(v), "emails": v} for k, v in buckets.items()}
