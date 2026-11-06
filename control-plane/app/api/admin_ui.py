"""Admin panel (server-rendered): login + 2FA, plans (data-driven), nodes, invoices, abuse."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.models import GiftToken, Peer
from app.models.base import as_utc, utcnow
from app.models.enums import NodeProtocol
from app.security import sessions
from app.security.rate_limit import client_key
from app.services import admin_auth, admin_service, gift
from app.services import promo as promo_service
from app.webui import (
    admin_flood_limiter,
    admin_session_binding,
    current_admin,
    login_limiter,
    set_cookie,
)
from app.webui import admin_templates as T

log = logging.getLogger("admin_ui")

# Secret admin path (defense-in-depth): /admin by default, in prod — an env secret
# (CP_ADMIN_PATH). Resolved once at import time; redirects/templates use the same
# value, so they are always consistent with the router prefix.
ADMIN = get_settings().admin_path


# Caddy on the CLEARNET sites (privacyvpn.example.com/.si) sets this marker on the upstream
# (header_up X-Tt-Edge clearnet, replace → the client cannot forge it). The Tor HS goes to
# the control-plane DIRECTLY (torrc HiddenServicePort), bypassing Caddy → no marker.
_EDGE_HEADER = "x-tt-edge"
_EDGE_CLEARNET = "clearnet"


def _is_tor(request: Request) -> bool:
    """The request came over Tor .onion. Fail-closed: we require BOTH signals — a genuine
    .onion Host AND the absence of the Caddy clearnet marker. This closes both Host-spoofing
    from the clearnet (Host=<onion>, but the marker is present → NOT Tor), and a Caddy
    misconfig without the marker (Host≠.onion → NOT Tor, plain clearnet is blocked)."""
    host_onion = (request.url.hostname or "").endswith(".onion")
    via_clearnet = request.headers.get(_EDGE_HEADER) == _EDGE_CLEARNET
    return host_onion and not via_clearnet


def _guard_onion(request: Request) -> None:
    """Tor-only: with CP_ADMIN_ONION_ONLY=true everything under ADMIN is served over
    Tor .onion only, otherwise → 404. Read at runtime → an env toggle + restart
    (an escape hatch against lockout)."""
    if get_settings().admin_onion_only and not _is_tor(request):
        raise HTTPException(status_code=404)


# the guard hangs on the whole router → it covers login (GET+POST), dashboard and all actions
router = APIRouter(prefix=ADMIN, tags=["admin"], dependencies=[Depends(_guard_onion)])


def _ctx(request: Request, **extra) -> dict:
    # admin_path is passed into all templates → links/forms/nav are built from the secret path
    return {"request": request, "admin_path": ADMIN, **extra}


def _require(request: Request, session: Session):
    return current_admin(request, session)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return T.TemplateResponse(request, "admin_login.html", _ctx(request))


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    totp: str = Form(""),
    session: Session = Depends(get_session),
):
    # M7: Caddy strips the client IP (no-logs), so an IP key would be global. We rate-limit
    # brute-force BY USERNAME (the name lives transiently in the RAM limiter, is not logged →
    # privacy preserved) + a global ceiling against flooding. The decision is deliberate: IP
    # privacy matters more, and per-username protects the real target (the known admin login).
    uname = (username or "").strip().lower()
    if not admin_flood_limiter.allow(client_key("all", "admin_login")):
        return T.TemplateResponse(request, "admin_login.html", _ctx(request, error="rate_limited"), status_code=429)
    if not login_limiter.allow(client_key(uname, "admin_login_user")):
        return T.TemplateResponse(request, "admin_login.html", _ctx(request, error="rate_limited"), status_code=429)
    admin = admin_auth.authenticate(session, username=username, password=password)
    if admin is None:
        return T.TemplateResponse(request, "admin_login.html", _ctx(request, error="invalid"), status_code=401)
    if admin_auth.has_totp(admin):
        if not admin_auth.verify_totp(admin, totp):
            return T.TemplateResponse(request, "admin_login.html", _ctx(request, error="totp"), status_code=401)
    elif get_settings().admin_require_2fa:
        # 2FA is mandatory, but the admin has no TOTP configured → login is closed (enroll first)
        return T.TemplateResponse(
            request, "admin_login.html", _ctx(request, error="2fa_required"), status_code=401
        )
    session.commit()  # #23: persist last_totp_counter (otherwise anti-replay won't survive the request)
    resp = RedirectResponse(ADMIN, status_code=303)
    ttl = get_settings().admin_session_ttl_seconds
    token = sessions.issue(
        admin.id, scope="admin", ttl=ttl, binding=admin_session_binding(admin)
    )
    # over Tor .onion (http inside Tor) a Secure cookie is dropped → no Secure;
    # on the clearnet — as usual (cookie_secure, True behind TLS). Tor encrypts the channel itself.
    secure = get_settings().cookie_secure and not _is_tor(request)
    set_cookie(resp, "admin_session", token, max_age=ttl, secure=secure)
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse(f"{ADMIN}/login", status_code=303)
    resp.delete_cookie("admin_session")
    return resp


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    return T.TemplateResponse(
        request,
        "admin_dashboard.html",
        _ctx(request, nodes=admin_service.list_nodes(session), plans=admin_service.list_plans(session)),
    )


@router.get("/plans", response_class=HTMLResponse)
def plans(request: Request, session: Session = Depends(get_session)):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    return T.TemplateResponse(
        request, "admin_plans.html", _ctx(request, plans=admin_service.list_plans(session))
    )


@router.post("/plans")
def create_plan(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    duration_days: int = Form(...),
    price_fiat_cents: int = Form(...),
    fiat_currency: str = Form("USD"),
    device_limit: int = Form(5),
    sort_order: int = Form(0),
    session: Session = Depends(get_session),
):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    admin_service.create_plan(
        session,
        code=code,
        name=name,
        duration_days=duration_days,
        price_fiat_cents=price_fiat_cents,
        fiat_currency=fiat_currency,
        device_limit=device_limit,
        sort_order=sort_order,
    )
    session.commit()
    return RedirectResponse(f"{ADMIN}/plans", status_code=303)


@router.post("/plans/{plan_id}")
def update_plan(
    plan_id: str,
    request: Request,
    price_fiat_cents: int = Form(None),
    device_limit: int = Form(None),
    is_active: str = Form(None),
    featured: str = Form(None),
    session: Session = Depends(get_session),
):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    from app.models import PricePlan

    plan = session.get(PricePlan, uuid.UUID(plan_id))
    if plan is not None:
        fields = {}
        if price_fiat_cents is not None:
            fields["price_fiat_cents"] = price_fiat_cents
        if device_limit is not None:
            fields["device_limit"] = device_limit
        if is_active is not None:
            fields["is_active"] = is_active == "1"
        if featured is not None:
            fields["featured"] = featured == "1"
        admin_service.update_plan(session, plan, **fields)
        session.commit()
    return RedirectResponse(f"{ADMIN}/plans", status_code=303)


# --- Promo codes -----------------------------------------------------------------------------
def _parse_expiry(raw: str):
    """`<input type=date>` value → aware end-of-day UTC, or None.

    End of the chosen day, not its start: an operator typing "expires 31.07" means the code works
    THROUGH the 31st. Anchoring at midnight would kill it a day early — a silent, deniable way to
    break codes that are already in customers' hands.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise ValueError("дата должна быть в формате ГГГГ-ММ-ДД") from None
    return day.replace(hour=23, minute=59, second=59)


def _parse_uses(raw: str) -> int | None:
    """Empty → None (unlimited). Non-numeric is rejected rather than coerced to "unlimited"."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ValueError("лимит использований — целое число или пусто")
    return int(raw)


def _promos_page(request: Request, session: Session, *, error: str | None = None, status: int = 200):
    return T.TemplateResponse(
        request, "admin_promos.html",
        _ctx(request, promos=admin_service.list_promos(session), error=error,
             min_percent=promo_service.MIN_PERCENT, max_percent=promo_service.MAX_PERCENT,
             min_len=promo_service.MIN_CODE_LEN),
        status_code=status,
    )


@router.get("/promos", response_class=HTMLResponse)
def promos(request: Request, session: Session = Depends(get_session)):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    return _promos_page(request, session)


@router.post("/promos", response_class=HTMLResponse)
def create_promo(
    request: Request,
    code: str = Form(...),
    percent_off: int = Form(...),
    expires_at: str = Form(""),
    max_uses: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    try:
        admin_service.create_promo(
            session,
            code=code,
            percent_off=percent_off,
            expires_at=_parse_expiry(expires_at),
            max_uses=_parse_uses(max_uses),
            note=note,
        )
        session.commit()
    except ValueError as e:
        # Render the reason instead of redirecting: a redirect would drop what the operator typed
        # and tell them nothing about which field was wrong.
        session.rollback()
        return _promos_page(request, session, error=str(e), status=400)
    except IntegrityError:
        # the UNIQUE on code fired between our check and the commit
        session.rollback()
        return _promos_page(request, session, error="такой код уже существует", status=400)
    return RedirectResponse(f"{ADMIN}/promos", status_code=303)


@router.post("/promos/{promo_id}", response_class=HTMLResponse)
def update_promo(
    promo_id: str,
    request: Request,
    percent_off: int = Form(None),
    expires_at: str = Form(None),
    max_uses: str = Form(None),
    is_active: str = Form(None),
    session: Session = Depends(get_session),
):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    from app.models import PromoCode

    try:
        row = session.get(PromoCode, uuid.UUID(promo_id))
    except (ValueError, TypeError):
        row = None
    if row is None:
        return RedirectResponse(f"{ADMIN}/promos", status_code=303)
    fields: dict = {}
    try:
        if percent_off is not None:
            fields["percent_off"] = percent_off
        if expires_at is not None:
            fields["expires_at"] = _parse_expiry(expires_at)
        if max_uses is not None:
            fields["max_uses"] = _parse_uses(max_uses)
        if is_active is not None:
            fields["is_active"] = is_active == "1"
        admin_service.update_promo(session, row, **fields)
        session.commit()
    except ValueError as e:
        session.rollback()
        return _promos_page(request, session, error=str(e), status=400)
    return RedirectResponse(f"{ADMIN}/promos", status_code=303)


@router.get("/nodes", response_class=HTMLResponse)
def nodes(request: Request, session: Session = Depends(get_session)):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    token = request.query_params.get("token")  # shown once after creation
    warn = request.query_params.get("warn")     # e.g. node_unreachable after block_peer
    return T.TemplateResponse(
        request, "admin_nodes.html",
        _ctx(request, nodes=admin_service.list_nodes(session), token=token, warn=warn),
    )


@router.post("/nodes")
def add_node(
    request: Request,
    name: str = Form(...),
    endpoint_host: str = Form(...),
    location_code: str = Form(...),
    location_name: str = Form(...),
    subnet_v4: str = Form(...),
    dns_ip: str = Form(...),
    subnet_v6: str = Form(""),
    protocol: str = Form("wireguard"),
    endpoint_port: str = Form(""),
    reality_dest: str = Form(""),
    reality_server_names: str = Form(""),
    session: Session = Depends(get_session),
):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    kwargs = dict(
        name=name,
        endpoint_host=endpoint_host,
        location_code=location_code,
        location_name=location_name,
        subnet_v4=subnet_v4,
        dns_ip=dns_ip,
        subnet_v6=subnet_v6 or None,
        protocol=NodeProtocol(protocol),
    )
    if endpoint_port.strip():
        kwargs["endpoint_port"] = int(endpoint_port)
    if reality_dest.strip():
        kwargs["reality_dest"] = reality_dest.strip()
    if reality_server_names.strip():
        kwargs["reality_server_names"] = [
            s.strip() for s in reality_server_names.split(",") if s.strip()
        ]
    try:
        # validate_dest=True: for vless_reality, a network check of dest (TLS1.3+h2+non-CDN).
        # DestError/ValueError → we do not create the node, we show the reason instead of a 500.
        _node, token = admin_service.add_node(session, validate_dest=True, **kwargs)
    except ValueError as e:
        session.rollback()
        return RedirectResponse(f"{ADMIN}/nodes?warn={quote(str(e))}", status_code=303)
    session.commit()
    return RedirectResponse(f"{ADMIN}/nodes?token={token}", status_code=303)


_INVOICE_ROWS = 100


@router.get("/invoices", response_class=HTMLResponse)
def invoices(request: Request, session: Session = Depends(get_session)):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    # `paid` is decided HERE, not in the template: the template used to read paid_at, which the
    # scrub nulls on purpose — see admin_service.invoice_is_paid.
    rows = [
        {"inv": inv, "paid": admin_service.invoice_is_paid(inv)}
        for inv in admin_service.list_invoices(session, limit=_INVOICE_ROWS)
    ]
    return T.TemplateResponse(
        request, "admin_invoices.html",
        _ctx(request, rows=rows, stats=admin_service.invoice_stats(session), row_cap=_INVOICE_ROWS),
    )


@router.post("/peers/{peer_id}/block")
def block_peer(peer_id: str, request: Request, session: Session = Depends(get_session)):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    peer = session.get(Peer, uuid.UUID(peer_id))
    dispatch_ok = True
    if peer is not None:
        from app.models import Node

        node = session.get(Node, peer.node_id)
        pubkey = admin_service.block_peer(session, peer)
        if node and node.agent_secret:
            from app.services import node_client

            try:
                node_client.dispatch_remove_peer(node, pubkey)
            except Exception as e:
                # an abuse block must NOT fail silently (M4): we log +
                # show the admin a banner that the node did not confirm the peer removal.
                dispatch_ok = False
                log.warning("block_peer dispatch failed node=%s peer=%s: %s", node.id, peer.id, e)
        session.commit()
    target = f"{ADMIN}/nodes" if dispatch_ok else f"{ADMIN}/nodes?warn=node_unreachable"
    return RedirectResponse(target, status_code=303)


# --- Accounts: operator manual grant/extend (a customer paid off-band, or a late/split payment
#     that needs a human decision). Search by the number the customer gives, then credit days. ---
#: Ceiling on a single manual grant. Not a policy limit — a crash guard: apply_credit does
#: expires_at + timedelta(days=N), and datetime overflows around 2.7M days, so an operator's
#: fat-fingered "300000" turns a money form into a 500 with nothing credited and nothing said.
MAX_GRANT_DAYS = 3650

#: Value of the "this grant pays off no invoice" radio. A sentinel rather than the empty string so
#: that "I chose to gift" and "I did not answer" are different answers on the wire — a UUID can
#: never collide with it.
GOODWILL_GRANT = "none"


def _resolve_days(plan, custom_days: str) -> int:
    """Days for a grant: an explicit number beats the chosen plan; 0 = nothing usable → error banner.

    An entry that is NOT empty but also not a number is an error, not a shrug. Falling back to the
    plan there (which is what "custom_days wins if it parses, else the plan" quietly did) means a
    mistyped day count is silently replaced by the tariff's length: the operator asks for one thing
    on a form that gives away paid time and gets another, with no message. Empty field = "I did not
    fill this in" → the plan. Non-empty = "I mean this number" → it must parse or be refused.
    """
    raw = (custom_days or "").strip()
    if not raw:
        days = plan.duration_days if plan is not None else 0
        return days if 0 < days <= MAX_GRANT_DAYS else 0
    # NOT a bare `raw.isdigit()`. That predicate is wider than int() accepts: "²".isdigit() is True
    # while int("²") raises — an unhandled ValueError, i.e. a bare 500 on a money form with nothing
    # credited and nothing said, which is the exact failure MAX_GRANT_DAYS exists to prevent. A
    # 4301-digit string of ordinary digits does the same (CPython's int-conversion limit).
    # `isascii()` additionally stops "٣٠" (Arabic-Indic) from reading as 30.
    ok = raw.isascii() and raw.isdigit() and len(raw) <= len(str(MAX_GRANT_DAYS))
    days = int(raw) if ok else 0
    return days if 0 < days <= MAX_GRANT_DAYS else 0


def _plan_or_none(session: Session, plan_id: str):
    """A plan by id, tolerating junk in the form field (a bad UUID must not be a 500)."""
    from app.models import PricePlan

    if not plan_id:
        return None
    try:
        return session.get(PricePlan, uuid.UUID(plan_id))
    except (ValueError, TypeError):
        return None


def _account_view(account, session):
    """Small read-only summary for the accounts page (no account number — we only hold a hash)."""
    from app.models import Peer
    from app.services.billing import evaluate_status

    peers = session.execute(
        select(Peer).where(Peer.account_id == account.id)
    ).scalars().all()
    exp = as_utc(account.expires_at)
    # Invoices reconcile may still auto-credit. Shown because the operator otherwise cannot tell a
    # genuine late payer from an abandoned cart (both sit in late_payment), and granting days on top
    # of one that later self-settles pays the customer twice. `seen` is the discriminator: coins
    # actually observed on the address.
    rescuable = [
        {
            "id": inv.id,
            "short": str(inv.id)[:8],
            "created": (as_utc(inv.created_at).strftime("%d.%m.%Y %H:%M UTC")
                        if inv.created_at else "—"),
            "amount": f"{inv.crypto_amount} {inv.crypto_currency}" if inv.crypto_amount else "—",
            # what the customer actually BOUGHT. Without it the operator sees only a crypto amount
            # and can link a 7-day make-good to a 365-day purchase — the dedupe then caps that
            # customer at 7 days permanently, because the real settle finds the credit already made.
            "days": inv.days_granted,
            "seen": inv.crypto_amount_paid is not None and inv.crypto_amount_paid > 0,
        }
        for inv in admin_service.rescuable_invoices(session, account)
    ]
    return {
        "id": account.id,
        "masked": admin_service.mask_account_id(account.id),
        "status": evaluate_status(account, grace_days=get_settings().grace_period_days).value,
        "expires_at": exp.strftime("%d.%m.%Y %H:%M UTC") if exp else "—",
        "devices": len(peers),
        "device_limit": account.device_limit,
        "rescuable": rescuable,
    }


def _accounts_ctx(request: Request, session: Session, **extra) -> dict:
    """Context shared by every render of the accounts page: the paginated listing + the plan picker.

    The filter and the page live in the QUERY STRING (plain <a> links) — that is what makes the
    listing work with JavaScript switched off, and it is safe to put there precisely because none of
    it is a secret: a status and a page number, never an account number (that one only ever travels
    in a POST body, see find_account_by_number).
    """
    status = request.query_params.get("status") or None
    if status not in admin_service.ACCOUNT_FILTERS:
        status = None
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    size = admin_service.ACCOUNT_PAGE_SIZE
    # ONE `now` for both queries: counting and listing a second apart could put an account on the
    # grace/expired boundary into the total but not the page (or the reverse) — an off-by-one the
    # operator would read as a lost account.
    now = utcnow()
    total = admin_service.count_accounts(session, status=status, now=now)
    pages = max(1, -(-total // size))
    page = min(page, pages)
    rows = admin_service.list_account_rows(
        session, status=status, limit=size, offset=(page - 1) * size, now=now
    )
    return _ctx(
        request,
        plans=admin_service.list_plans(session, active_only=True),
        accounts=rows,
        filters=admin_service.ACCOUNT_FILTERS,
        filter_status=status,
        max_grant_days=MAX_GRANT_DAYS,
        goodwill_grant=GOODWILL_GRANT,
        page=page,
        pages=pages,
        total=total,
        **extra,
    )


@router.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request, session: Session = Depends(get_session)):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    return T.TemplateResponse(request, "admin_accounts.html", _accounts_ctx(request, session))


@router.post("/accounts/search", response_class=HTMLResponse)
def accounts_search(
    request: Request,
    account_number: str = Form(""),
    session: Session = Depends(get_session),
):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    account = admin_service.find_account_by_number(session, account_number)
    ctx = _accounts_ctx(request, session)
    if account is None:
        # do NOT echo the entered number back (it is a customer secret) — just report not-found
        ctx["not_found"] = True
    else:
        ctx["found"] = _account_view(account, session)
    return T.TemplateResponse(request, "admin_accounts.html", ctx)


@router.post("/accounts/new", response_class=HTMLResponse)
def accounts_create(
    request: Request,
    plan_id: str = Form(""),
    custom_days: str = Form(""),
    session: Session = Depends(get_session),
):
    """Mint an account with time already on it and show its number ONCE.

    The number goes into the response BODY and nowhere else: not into the log line below (that would
    put a live credential into journald forever), not into a redirect (a 303 would park it in the
    URL, i.e. in every proxy log and in the operator's history), not into the DB (only its HMAC is
    stored — the server genuinely cannot show it a second time).
    """
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    plan = _plan_or_none(session, plan_id)
    days = _resolve_days(plan, custom_days)
    if days <= 0:
        return T.TemplateResponse(
            request, "admin_accounts.html", _accounts_ctx(request, session, create_error=True)
        )
    account, number = admin_service.create_account(
        session,
        days=days,
        device_limit=plan.device_limit if plan else None,
        price_plan_code=plan.code if plan else admin_service.OPERATOR_GRANT_CODE,
    )
    session.commit()
    session.refresh(account)
    # money-equivalent: this gives away paid time. Logged as a fact — WITHOUT the number.
    log.warning(
        "admin grant: NEW account=%s +%sd (plan=%s)",
        account.id, days, plan.code if plan else "custom",
    )
    exp = as_utc(account.expires_at)
    ctx = _accounts_ctx(request, session)
    ctx["created"] = {
        "number": number,
        "masked": admin_service.mask_account_id(account.id),
        "days": days,
        "device_limit": account.device_limit,
        "expires_at": exp.strftime("%d.%m.%Y %H:%M UTC") if exp else "—",
    }
    return T.TemplateResponse(request, "admin_accounts.html", ctx)


@router.post("/accounts/{account_id}/extend", response_class=HTMLResponse)
def accounts_extend(
    account_id: str,
    request: Request,
    plan_id: str = Form(""),
    custom_days: str = Form(""),
    # Which invoice this grant pays off, chosen explicitly. Never inferred: an account can hold an
    # abandoned cart AND a real late payment at once, and settling the wrong one (or all of them)
    # strands the customer's coins. GOODWILL_GRANT = "pays off nothing", said out loud; EMPTY means
    # the operator did not answer at all, which is refused whenever there is something to answer
    # about (see below).
    settle_invoice_id: str = Form(""),
    session: Session = Depends(get_session),
):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    from app.models import Account

    try:
        account = session.get(Account, uuid.UUID(account_id))
    except (ValueError, TypeError):
        account = None
    ctx = _accounts_ctx(request, session)
    if account is None:
        ctx["not_found"] = True
        return T.TemplateResponse(request, "admin_accounts.html", ctx)

    plan = _plan_or_none(session, plan_id)
    # custom days win over the plan; otherwise the chosen plan's duration
    days = _resolve_days(plan, custom_days)

    if days <= 0:
        ctx["found"] = _account_view(account, session)
        ctx["extend_error"] = True
        return T.TemplateResponse(request, "admin_accounts.html", ctx)

    # An UNANSWERED question is not a goodwill grant. The page has always warned that granting days
    # without linking them lets reconcile credit the same coins again later — but "against no
    # pre-selected, so forgetting to answer and deciding to gift looked identical on the wire. That
    # is the whole double-payout scenario the settle_invoice machinery exists to prevent, one step
    # short. So: when this account has invoices the automation may still settle, the form is only
    # accepted with an explicit choice — including an explicit "pays off nothing".
    rescuable = admin_service.rescuable_invoices(session, account)
    if rescuable and not settle_invoice_id:
        ctx["found"] = _account_view(account, session)
        ctx["settle_unchosen"] = True
        return T.TemplateResponse(request, "admin_accounts.html", ctx)

    # Resolve the chosen invoice, and only accept one that really belongs to this account and is
    # still rescuable — a stale form (the operator read the page while reconcile settled it) must
    # not link the credit to something that already paid out.
    settle = None
    if settle_invoice_id and settle_invoice_id != GOODWILL_GRANT:
        settle = next(
            (i for i in rescuable if str(i.id) == settle_invoice_id),
            None,
        )
        if settle is None:
            # the invoice moved on between render and submit — refuse rather than credit blindly,
            # because apply_credit's per-invoice dedupe is exactly what we would be bypassing
            ctx["found"] = _account_view(account, session)
            ctx["settle_stale"] = True
            return T.TemplateResponse(request, "admin_accounts.html", ctx)

    # Linking a SHORTER grant to a longer purchase is a permanent cap, not a partial help: the
    # per-invoice dedupe means the real settle will add nothing later. Refuse and say so.
    if settle is not None and days < settle.days_granted:
        ctx["found"] = _account_view(account, session)
        ctx["undercut"] = {"days": days, "invoice_days": settle.days_granted}
        return T.TemplateResponse(request, "admin_accounts.html", ctx)

    before = as_utc(account.expires_at)
    try:
        admin_service.grant_days(session, account, days=days, plan=plan, settle_invoice=settle)
        session.commit()
    except IntegrityError:
        # the UNIQUE on subscription.invoice_id fired: reconcile credited this very invoice
        # between our check and our commit. The invariant held — report it instead of a 500.
        session.rollback()
        ctx = _accounts_ctx(request, session)
        ctx["found"] = _account_view(account, session)
        ctx["already_credited"] = True
        return T.TemplateResponse(request, "admin_accounts.html", ctx)

    session.refresh(account)
    # rebuild the listing AFTER the commit — otherwise the table underneath the "extended" banner
    # still shows the account as expired, i.e. contradicts the banner on the same screen
    ctx = _accounts_ctx(request, session)
    # apply_credit returns the EXISTING row when this invoice was already credited, adding no days.
    # Saying "extended by N" then would be a lie the operator acts on (they would walk away).
    credited = as_utc(account.expires_at) != before
    log.warning(
        "admin grant: account=%s +%sd (plan=%s, settles_invoice=%s, credited=%s)",
        account.id, days, plan.code if plan else "custom",
        settle.id if settle else "none", credited,
    )
    ctx["found"] = _account_view(account, session)
    if credited:
        ctx["granted"] = days
        ctx["settled_invoice"] = str(settle.id)[:8] if settle else None
    else:
        ctx["already_credited"] = True
    return T.TemplateResponse(request, "admin_accounts.html", ctx)


# --- Gift tokens ------------------------------------------------------------------------------
def _gifts_ctx(request: Request, session: Session, **extra) -> dict:
    return _ctx(
        request,
        gifts=[_gift_view(row) for row in gift.list_tokens(session)],
        plans=admin_service.list_plans(session, active_only=True),
        max_batch=gift.MAX_BATCH,
        max_grant_days=MAX_GRANT_DAYS,
        **extra,
    )


def _gift_view(row) -> dict:
    """One row for the listing — with no token, the server has none to show. `status` is computed,
    not stored, so it cannot drift from services/gift._check."""
    redeemed = as_utc(row.redeemed_at)
    expires = as_utc(row.expires_at)
    if redeemed is not None:
        status = "redeemed"
    elif not row.is_active:
        status = "disabled"
    elif expires is not None and expires <= utcnow():
        status = "expired"
    else:
        status = "live"
    return {
        "id": row.id,
        "days": row.days,
        "device_limit": row.device_limit,
        "plan": row.price_plan_code or "—",
        "note": row.note,
        "status": status,
        "is_active": row.is_active,
        # day resolution on purpose — services/gift.redeem coarsens it
        "redeemed_at": redeemed.strftime("%d.%m.%Y") if redeemed else "—",
        "expires_at": expires.strftime("%d.%m.%Y") if expires else "—",
        "created_at": as_utc(row.created_at).strftime("%d.%m.%Y") if row.created_at else "—",
    }


@router.get("/gifts", response_class=HTMLResponse)
def gifts(request: Request, session: Session = Depends(get_session)):
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    return T.TemplateResponse(request, "admin_gifts.html", _gifts_ctx(request, session))


@router.post("/gifts", response_class=HTMLResponse)
def gifts_issue(
    request: Request,
    plan_id: str = Form(""),
    custom_days: str = Form(""),
    count: str = Form("1"),
    expires_at: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    """Mint a batch and print it ONCE, into the response BODY: not into the log line below, not into
    a 303 (a batch of live credentials in the URL), not into the DB, which holds only HMACs. Same
    rule as accounts_create."""
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    plan = _plan_or_none(session, plan_id)
    days = _resolve_days(plan, custom_days)
    if days <= 0:
        return T.TemplateResponse(
            request, "admin_gifts.html",
            _gifts_ctx(request, session, error="выберите тариф или укажите число дней"),
            status_code=400,
        )
    raw_count = (count or "").strip()
    if not (raw_count.isascii() and raw_count.isdigit()):
        return T.TemplateResponse(
            request, "admin_gifts.html",
            _gifts_ctx(request, session, error="количество — целое число"),
            status_code=400,
        )
    try:
        tokens = gift.issue_batch(
            session,
            count=int(raw_count),
            days=days,
            device_limit=plan.device_limit if plan else None,
            price_plan_code=plan.code if plan else None,
            expires_at=_parse_expiry(expires_at),
            note=note,
        )
        session.commit()
    except ValueError as e:
        session.rollback()
        return T.TemplateResponse(
            request, "admin_gifts.html", _gifts_ctx(request, session, error=str(e)), status_code=400
        )
    except IntegrityError:
        # UNIQUE on token_hash at 80 bits = a broken RNG. Refuse the batch, do not issue a short one.
        session.rollback()
        return T.TemplateResponse(
            request, "admin_gifts.html",
            _gifts_ctx(request, session, error="коллизия кода при генерации — повторите выпуск"),
            status_code=500,
        )
    # money-equivalent: each of these is paid time. Logged as a fact — WITHOUT the tokens.
    log.warning(
        "admin gift: issued %s token(s) x%sd (plan=%s)",
        len(tokens), days, plan.code if plan else "custom",
    )
    return T.TemplateResponse(
        request, "admin_gifts.html",
        _gifts_ctx(
            request, session,
            issued={"tokens": tokens, "days": days,
                    "device_limit": plan.device_limit if plan else None},
        ),
    )


@router.post("/gifts/{gift_id}", response_class=HTMLResponse)
def gifts_toggle(
    gift_id: str,
    request: Request,
    is_active: str = Form(...),
    session: Session = Depends(get_session),
):
    """Switch a token off or back on — the only edit an issued token allows (gift.set_active)."""
    if _require(request, session) is None:
        return RedirectResponse(f"{ADMIN}/login", status_code=303)
    try:
        row = session.get(GiftToken, uuid.UUID(gift_id))
    except (ValueError, TypeError):
        row = None
    if row is not None:
        gift.set_active(session, row, is_active == "1")
        session.commit()
    return RedirectResponse(f"{ADMIN}/gifts", status_code=303)
