"""Open-pixel tracking, click redirects, and Caddy on-demand TLS ask endpoint.

Routes
------
GET /o/{log_id}        — open tracking pixel (1×1 transparent GIF)
GET /c/{token}         — click-tracking redirect to original URL
GET/POST /u/{token}    — unsubscribe confirmation page / unsubscribe (RFC 8058)
GET /api/caddy/ask     — Caddy on_demand_tls domain approval gate

These routes work on any hostname (the app's own domain *or* a custom
tracking domain that CNAMEs here), so click/open tracking survives even
without a custom domain configured.
"""
from __future__ import annotations

# // BEACON SYNC: This file handles tracking logic that Beacon mirrors.
# // Any changes to tracking behavior, event types, metadata collected, or URL structure
# // must also be reflected in the Beacon service (/beacon or the separate Beacon repo).
# // If you are an AI agent, add a TODO at the end of your response listing what needs to be updated in Beacon.

import ipaddress
import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import Inbox
from app.tracking import PIXEL_GIF, is_domain_pending
from app.tracking_events import (
    record_email_click,
    record_email_open,
    process_unsubscribe,
    peek_unsubscribe,
    log_unsubscribe_view,
    unsubscribe_confirm_html,
    INVALID_UNSUBSCRIBE_DE_HTML,
    ALREADY_UNSUBSCRIBED_DE_HTML,
    resolve_click_redirect_url,
)
from app.client_ip import client_ip_from_request

log = logging.getLogger("quickly.tracking")

router = APIRouter(tags=["tracking"])


@router.get("/o/{token}", include_in_schema=False)
async def open_pixel(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return a 1×1 transparent GIF and record the email open.

    The *token* is a random URL-safe string stored in ``EmailLog.open_token``.
    For backwards compatibility with older emails that used integer log IDs,
    we fall back to an integer lookup when the token doesn't match.
    """
    ip = client_ip_from_request(request)
    await record_email_open(db, token, ip)
    return Response(
        content=PIXEL_GIF,
        media_type="image/gif",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/c/{token}", include_in_schema=False)
async def click_redirect(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Record a link click and 302-redirect the recipient to the original URL."""
    dest = await resolve_click_redirect_url(db, token)
    if not dest:
        return Response(status_code=404, content="Not found")
    ip = client_ip_from_request(request)
    await record_email_click(db, token, ip)
    return RedirectResponse(url=dest, status_code=302)


_NO_CACHE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}


@router.get("/u/{token}", include_in_schema=False)
async def unsubscribe(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Show the unsubscribe confirmation page. Never changes state.

    Link scanners (Defender Safe Links, Sophos, ...) prefetch this URL, so the
    actual unsubscribe happens only on POST (form submit or RFC 8058 one-click).
    """
    row, already = await peek_unsubscribe(db, token)
    if row is None:
        return Response(content=INVALID_UNSUBSCRIBE_DE_HTML, media_type="text/html",
                        status_code=404, headers=_NO_CACHE)
    await log_unsubscribe_view(
        db, row, ip=client_ip_from_request(request),
        user_agent=request.headers.get("user-agent"),
    )
    html = ALREADY_UNSUBSCRIBED_DE_HTML if already else unsubscribe_confirm_html(token)
    return Response(content=html, media_type="text/html", status_code=200, headers=_NO_CACHE)


async def _read_form(request: Request) -> dict[str, str]:
    reader = getattr(request, "body", None)
    if reader is None:
        return {}
    try:
        raw = await reader()
    except Exception:
        return {}
    return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "ignore")).items()}


@router.post("/u/{token}", include_in_schema=False)
async def unsubscribe_one_click(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Perform the unsubscribe (idempotent).

    Serves both the confirmation form and RFC 8058 one-click
    (``List-Unsubscribe=One-Click``, form-encoded or empty body).
    Browsers (Accept: text/html or field ``confirm``) get the German HTML page,
    mail providers get plain text ``unsubscribed``.
    """
    form = await _read_form(request)
    accept = request.headers.get("accept") or ""
    browser = "text/html" in accept.lower() or "confirm" in form
    one_click = form.get("List-Unsubscribe") == "One-Click"
    html, status = await process_unsubscribe(
        db, token, method="POST", ip=client_ip_from_request(request),
        user_agent=request.headers.get("user-agent"), one_click=one_click,
        german=browser,
    )
    if browser:
        return Response(content=html, media_type="text/html", status_code=status, headers=_NO_CACHE)
    if status != 200:
        return PlainTextResponse("not found", status_code=status, headers=_NO_CACHE)
    return PlainTextResponse("unsubscribed", status_code=200, headers=_NO_CACHE)


@router.get("/api/caddy/ask", include_in_schema=False)
async def caddy_ask(
    domain: str,
    db: AsyncSession = Depends(get_db),
):
    """Caddy on_demand_tls ask endpoint.

    Returns HTTP 200 to allow TLS certificate provisioning for *domain*, or
    HTTP 403 to deny.  Caddy calls this as:

        GET /api/caddy/ask?domain=<hostname>

    We approve ONLY domains that are explicitly saved as a custom tracking
    domain on an inbox record.  Everything else — including the app's own
    primary host, bare hostnames (e.g. ``localhost``), and IP addresses —
    is rejected so that Caddy never attempts to obtain a public certificate
    for something that would inevitably fail (and loop).

    The app's primary domain does not need on_demand_tls; Caddy's explicit
    site block handles that automatically.
    """
    if "." not in domain:
        log.debug("caddy/ask: skipped bare hostname domain=%s", domain)
        return Response(status_code=403, content="domain not allowed")

    try:
        ipaddress.ip_address(domain)
        log.debug("caddy/ask: skipped IP address domain=%s", domain)
        return Response(status_code=403, content="domain not allowed")
    except ValueError:
        pass

    result = await db.execute(
        select(Inbox).where(Inbox.tracking_domain == domain)
    )
    if result.scalar_one_or_none() is not None:
        log.info("caddy/ask: approved domain=%s (per-inbox tracking domain)", domain)
        return Response(status_code=200)

    if is_domain_pending(domain):
        log.info("caddy/ask: approved domain=%s (pending cert provisioning)", domain)
        return Response(status_code=200)

    log.warning("caddy/ask: rejected unknown domain=%s", domain)
    return Response(status_code=403, content="domain not allowed")


@router.get("/api/tracking-probe", include_in_schema=False)
async def tracking_probe():
    """Return a simple JSON OK.

    The backend verification endpoint fetches this URL via the *custom*
    domain to confirm that the CNAME resolves to this server.
    """
    return {"ok": True}
