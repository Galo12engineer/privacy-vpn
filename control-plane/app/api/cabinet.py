"""Web cabinet (server-rendered, .onion-compatible, works without JS).

Landing on `/`, account-number login on `/login` (anti-bruteforce/enumeration), browser
keygen with fallback, devices, purchase/renewal, expiry banner, guides.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import seo
from app.config import get_settings
from app.db import get_session
from app.models import Account, Invoice, Node, Peer, PricePlan
from app.models.base import as_utc
from app.models.enums import InvoiceStatus, NodeProtocol, PaymentMethod
from app.payments import registry
from app.security import sessions
from app.security.rate_limit import client_key
from app.services import account_email, gift, min_tx
from app.services import promo as promo_service
from app.services.account_number import hash_account_number, is_valid_format
from app.services.billing import evaluate_status
from app.services.config_builder import render_conf
from app.services.gift import GiftError
from app.services.promo import PromoError, discounted_cents
from app.services.provisioning import (
    AccountNotActive,
    DeviceLimitReached,
    create_peer,
    create_vless_peer,
    remove_device,
)
from app.services.purchase import UnknownAccount, start_purchase
from app.services.vless_link import to_vless_link
from app.webui import (
    buy_limiter,
    current_account,
    get_lang,
    login_limiter,
    preview_limiter,
    promo_limiter,
    redeem_limiter,
    set_cookie,
    translator,
)
from app.webui import (
    cabinet_templates as T,
)

log = logging.getLogger("cabinet")
router = APIRouter(tags=["cabinet"])

# BlockBee (non-custodial "address per order") is the ONLY gateway, on BTC/LTC/USDT-TRC20.
# XMR and Lightning have no entry at all: the NOWPayments adapter that used to carry them was
# removed (2026-07-24), and a method with no provider is exactly what _provider_for must refuse.
# They remain in PaymentMethod for enum/DB history — see _OFFERED_METHODS for what we sell.
_PROVIDER_NAME = {
    PaymentMethod.BTC_ONCHAIN: "blockbee",
    PaymentMethod.LITECOIN: "blockbee",
    PaymentMethod.USDT_TRC20: "blockbee",
}

# Launching on 3 coins via BlockBee (BTC/LTC/USDT-TRC20).
# XMR is temporarily disabled (no Monero wallet) — add the row back once we set up an address.
# Lightning is disabled — BlockBee forwards on-chain, there is no separate LN payout.
# Order matters: _default_method_for preselects the FIRST method a plan clears, and the /buy form
# lists them top-down in this order. Litecoin is first ON PURPOSE — it is the only rail with a flat
# ~1% cost at any amount (BlockBee forwards the fee as a second output of the same tx), whereas
# USDT-TRC20 carries a FIXED ~$4.5 Tron network fee that eats 34% of a $13.50 payment. Steering the
# default to LTC is the single highest-leverage fee fix we have. Do not reorder without re-checking
# that economics: putting USDT first is what put the 2026-07-21 customer on the 34% rail.
_METHOD_DEFS = [
    ("litecoin", "Litecoin (LTC)", PaymentMethod.LITECOIN),
    ("usdt_trc20", "USDT (TRC-20)", PaymentMethod.USDT_TRC20),
    ("btc_onchain", "Bitcoin (on-chain)", PaymentMethod.BTC_ONCHAIN),
]

# What we ACTUALLY sell — the single gate for POST /buy. PaymentMethod still contains MONERO and
# LIGHTNING (kept for the enum/DB history). Nothing rejected them: the min-USD gate has no entry
# for them, so a hand-made POST method=monero minted a REAL invoice for a coin we do not sell and
# have no payout address for — the customer's coins would land nowhere recoverable. The form is
# the contract; enforce it. (They now also have no _PROVIDER_NAME entry, which is a second wall,
# but this gate is the one that returns a clean 400 instead of a "provider unavailable" page.)
_OFFERED_METHODS = frozenset(method for _value, _label, method in _METHOD_DEFS)

def _methods(tr):
    """Payment methods for the /buy form, with a localized hint.

    Built per request (NOT a module constant) because the hint must pass through the request's
    translator: a hardcoded Russian string would leak onto the English page. The minimum in the
    hint comes from min_tx, the same source the POST gate enforces, so the label can never drift
    from what is actually accepted.
    """
    out = []
    for value, label, method in _METHOD_DEFS:
        min_usd = min_tx.min_usd_for(method)
        sub = (
            tr("рекомендуем · любая сумма")
            if method is PaymentMethod.LITECOIN
            else tr("от {min}").format(min=f"${min_usd}")
        )
        out.append({"value": value, "label": label, "sub": sub})
    return out


def _default_method_for(plan, discount_percent: int = 0) -> str | None:
    """First offered method whose min-USD gate this plan actually clears (form order preserved).

    The /buy form defaults both radios to `loop.first`, and those two defaults contradicted each
    other: the first plan is the cheapest ($5) while the first method is USDT, gated at $12. A
    visitor who just pressed the button without touching anything got a 400 on the very first
    click — on the entry-level plan, i.e. the most common first interaction we have. The card
    already lists which coins can pay a plan; preselect one of them instead of a dead pair.

    Judged on the DISCOUNTED price: with a promo applied, the coins a plan can be paid with shrink,
    and preselecting one the discount just pushed out of range recreates the 400-on-first-click.
    """
    payable = discounted_cents(plan.price_fiat_cents, discount_percent)
    for value, _label, method in _METHOD_DEFS:
        if min_tx.gate_ok(method, payable):
            return value
    # Every offered coin is gated above this plan's price — reachable, because plan prices are
    # operator-editable in the admin (and now because a promo can push the amount down). Returning
    # None would drop the form back to `loop.first` (USDT, the highest gate) = exactly the
    # 400-on-first-click this function exists to prevent. Fall back to the LOWEST gate: still
    # unpayable, but the error names the smallest shortfall.
    return min(_METHOD_DEFS, key=lambda d: min_tx.min_usd_for(d[2]))[0]


def _plans_view(plans, tr, discount_percent: int = 0) -> list[dict]:
    """Plan cards for /buy: price, effective per-month price, and which coins can actually pay it.

    Why per-month: the ladder's whole point is that a longer plan is cheaper per month, and the
    card used to show only the total — the discount was invisible.

    Why the coin list: the cheapest plan physically cannot be paid with BTC/USDT (BlockBee will
    not forward below its min_tx — see services/min_tx). We used to let the user pick an
    impossible combination and only then throw an error. Better to say it upfront, on the card.

    With a promo applied, `price` is the amount that will actually be charged and `price_was` is
    the list price it replaces. `price_was` is the plan's REAL current price, not an invented
    anchor — the customer can open /buy without the code and read the same number. That is the
    line between showing a discount and faking one.
    """
    out = []
    for p in plans:
        months = max(1, round(p.duration_days / 30))
        payable = discounted_cents(p.price_fiat_cents, discount_percent)
        coins = [
            label
            for _value, label, method in _METHOD_DEFS
            if min_tx.gate_ok(method, payable)
        ]
        out.append(
            {
                "code": p.code,
                "name": p.name,
                "price": f"{payable / 100:.2f} {p.fiat_currency}",
                "price_was": (
                    f"{p.price_fiat_cents / 100:.2f} {p.fiat_currency}"
                    if payable != p.price_fiat_cents
                    else None
                ),
                "per_month": f"{payable / 100 / months:.2f} {p.fiat_currency}",
                "months": months,
                "device_limit": p.device_limit,
                "duration_days": p.duration_days,
                # short coin names for the card: "Litecoin (LTC)" -> "LTC"
                "coins": [c.split("(")[-1].rstrip(")") if "(" in c else c for c in coins],
                "coins_limited": len(coins) < len(_METHOD_DEFS),
            }
        )
    return out


def _provider_for(method: PaymentMethod):
    # BlockBee is the only provider; there is NO fallback since the NOWPayments adapter was
    # removed (2026-07-24). An unregistered provider returns None, and the caller turns that into
    # a friendly "payments unavailable" on /buy — the right outcome. Silently routing to a second
    # gateway would mean minting invoices against credentials nobody has verified recently.
    # The dev fake registers under "blockbee" too (see payments/bootstrap.py), so it lands here.
    name = _PROVIDER_NAME.get(method)
    if not name:
        return None
    try:
        return registry.get(name)
    except KeyError:
        pass
    return None


def _ctx(request: Request, **extra) -> dict:
    lang = get_lang(request)
    # inject the server-side translator into the context → overrides the global passthrough `_`
    # invoice_ttl_minutes is injected for EVERY page on purpose: /buy renders from five different
    # code paths (form, 403, 429, no-provider, below-minimum) and a per-path context would sooner
    # or later miss one, breaking the template. It is a plain int from settings — the single source
    # for the payment window we advertise, so the copy can never drift from the enforced clock.
    tr = translator(lang)
    # SEO/link-preview context, injected for EVERY page for the same reason as invoice_ttl_minutes:
    # a per-route context would eventually miss a route, and the miss is invisible (a page simply
    # ships without canonical/description). `origin` is whitelisted, never the raw Host — see
    # app/seo.py. On .onion it resolves to the onion itself, so og:image stays same-origin and the
    # "zero external resources" invariant holds there too.
    origin = seo.origin_for(request)
    return {
        "request": request,
        "lang": lang,
        "_": tr,
        "invoice_ttl_minutes": get_settings().invoice_ttl_minutes,
        "canonical_url": seo.canonical_url(request),
        "og_image": f"{origin}/static/og.png",
        "page_description": tr(seo.description_for(request.url.path)),
        **extra,
    }


def _masked(account: Account) -> str:
    return "•••• " + str(account.id).replace("-", "")[-4:]


def _fmt(dt: datetime | None) -> str | None:
    dt = as_utc(dt)
    return dt.strftime("%d.%m.%Y %H:%M UTC") if dt else None


def _pay_deadline(invoice) -> str | None:
    """When the coins must ARRIVE, as an absolute UTC stamp.

    The customer was never told a deadline existed. /buy promised a "15-minute rate lock" that no
    code implements (`rate_expires_at` is written in purchase.py and read nowhere), while the real
    clock is invoice.expires_at = created + invoice_ttl_minutes (60 on prod). Funding a wallet from
    an exchange easily takes longer than that, so a silent deadline is a money-losing trap.
    Absolute time, not "60 minutes left": the page is static (no-JS by design), so a relative
    countdown would be a lie the moment it is cached or reread.

    Past the deadline the invoice is NO LONGER a dead end: expiry routes an invoice that had a live
    deposit address to late_payment (not terminal EXPIRED), and reconcile polls it, so coins that
    land after the window still auto-credit. See invoice_expiry / billing_fsm.EXPIRED_FUNDABLE.
    """
    return _fmt(getattr(invoice, "expires_at", None))


@router.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    # landing prices are data-driven from the plans (single source, no hardcoded lies)
    plans = session.execute(
        select(PricePlan).where(PricePlan.is_active.is_(True)).order_by(PricePlan.sort_order)
    ).scalars().all()
    resp = T.TemplateResponse(
        request, "landing.html", _ctx(request, plans=plans, locations=_sellable_locations(session))
    )
    cid = request.query_params.get("subid") or request.query_params.get("click_id")
    if cid:
        resp.set_cookie("cid", cid, max_age=86400, httponly=True, samesite="lax")
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, session: Session = Depends(get_session)):
    if current_account(request, session):
        return RedirectResponse("/dashboard", status_code=303)
    return T.TemplateResponse(request, "login.html", _ctx(request))


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, account_number: str = Form(""), session: Session = Depends(get_session)):
    # We key by the hash of the submitted number, NOT by client-IP: Caddy strips the IP (no-logs)
    # → an IP key would be one single global bucket (5/60s for everyone) = a trivial DoS lock of
    # the whole login. Per-number bucket: an attack on one number does not block the others;
    # brute force is unrealistic anyway (66-bit keyspace). Symmetric to the admin login
    # (M7, per-username).
    tr = translator(get_lang(request))
    key = client_key(account_number.strip() or None, "cabinet-login")
    if not login_limiter.allow(key):
        return T.TemplateResponse(
            request, "login.html",
            _ctx(request, error=tr("Слишком много попыток. Подождите минуту.")), status_code=429,
        )
    account = None
    if is_valid_format(account_number):
        h = hash_account_number(account_number, get_settings().account_number_pepper)
        account = session.execute(
            select(Account).where(Account.account_number_hash == h)
        ).scalar_one_or_none()
    if account is None:
        return T.TemplateResponse(
            request, "login.html", _ctx(request, error=tr("Номер аккаунта не найден.")), status_code=401,
        )
    resp = RedirectResponse("/dashboard", status_code=303)
    set_cookie(
        resp, "session",
        sessions.issue(account.id, scope="cabinet", binding=str(account.session_epoch)),
    )
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("session")
    return resp


@router.post("/logout-all")
def logout_all(request: Request, session: Session = Depends(get_session)):
    """#17: "log out on all devices" — incrementing session_epoch invalidates ALL
    previously issued cabinet tokens (including a stolen cookie), because the binding stops matching."""
    account = current_account(request, session)
    resp = RedirectResponse("/", status_code=303)
    if account is not None:
        account.session_epoch += 1
        session.commit()
    resp.delete_cookie("session")
    return resp


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    account = current_account(request, session)
    if account is None:
        return RedirectResponse("/login", status_code=303)
    s = get_settings()
    now = datetime.now(UTC)
    status = evaluate_status(account, now=now, grace_days=s.grace_period_days)
    exp = as_utc(account.expires_at)

    peers = session.execute(
        select(Peer).where(Peer.account_id == account.id)
    ).scalars().all()
    node_loc = {
        n.id: n.location_name
        for n in session.execute(select(Node)).scalars().all()
    }
    devices = [
        {
            "id": p.id,
            "name": p.name or (p.public_key[:10] + "…"),
            "location": node_loc.get(p.node_id, "—"),
            "ip": p.ip_lease.address if p.ip_lease else "—",
            "added": as_utc(p.created_at).strftime("%d.%m.%Y") if p.created_at else "",
        }
        for p in peers
    ]

    days_left = (exp - now).days if exp and exp > now else None
    grace_until = _fmt(exp + timedelta(days=s.grace_period_days)) if exp else None

    # A live pending invoice, surfaced so the customer can get BACK to his payment. Losing the
    # invoice tab (Back → Firefox "Document Expired", closing the tab) used to strand the payment
    # address with no way to it: the dashboard never linked the invoice. The session cookie set at
    # POST /buy survives a tab close, so /dashboard is the recovery path — it now shows the invoice.
    pending = session.execute(
        select(Invoice)
        .where(
            Invoice.account_id == account.id,
            Invoice.status.in_((InvoiceStatus.CREATED, InvoiceStatus.AWAITING_CONFIRMATION)),
        )
        .order_by(Invoice.created_at.desc())
    ).scalars().first()
    # Only offer a live invoice. A missing or past deadline → do not advertise it as payable
    # (and never dereference a None deadline — that would 500 the whole dashboard).
    if pending is not None:
        exp_p = as_utc(pending.expires_at)
        if exp_p is None or exp_p <= now:
            pending = None
    pending_view = (
        {
            "id": pending.id,
            "amount": pending.crypto_amount,
            "currency": pending.crypto_currency,
            "deadline": _pay_deadline(pending),
            "awaiting": pending.status == InvoiceStatus.AWAITING_CONFIRMATION,
        }
        if pending is not None
        else None
    )

    return T.TemplateResponse(
        request, "dashboard.html",
        _ctx(
            request,
            page="dashboard",
            account=account,
            account_number_masked=_masked(account),
            status=status.value,
            expires_at=_fmt(exp),
            days_left=days_left,
            grace_until=grace_until,
            devices=devices,
            device_count=len(devices),
            device_limit=account.device_limit,
            pending_invoice=pending_view,
        ),
    )


def _render_add_device(request: Request, session: Session, *, error: str | None = None, status_code: int = 200):
    nodes = session.execute(
        select(Node).where(Node.available_for_sale.is_(True)).order_by(Node.location_name)
    ).scalars().all()
    return T.TemplateResponse(
        request, "add_device.html",
        _ctx(request, page="devices", nodes=nodes, error=error), status_code=status_code,
    )


@router.get("/devices/add", response_class=HTMLResponse)
def device_add_form(request: Request, session: Session = Depends(get_session)):
    account = current_account(request, session)
    if account is None:
        return RedirectResponse("/login", status_code=303)
    return _render_add_device(request, session)


@router.post("/devices/add", response_class=HTMLResponse)
def device_add(
    request: Request,
    node_id: str = Form(...),
    public_key: str = Form(""),
    name: str = Form(""),
    session: Session = Depends(get_session),
):
    tr = translator(get_lang(request))
    account = current_account(request, session)
    if account is None:
        return RedirectResponse("/login", status_code=303)
    # guard: an empty/broken node_id (e.g. an empty select during a node reboot) → not a 500 but a
    # banner; plus we don't let a device bind to a node withdrawn from sale (bypassing the sale gate).
    try:
        node = session.get(Node, uuid.UUID(node_id)) if node_id else None
    except (ValueError, TypeError):
        node = None
    if node is None or not node.available_for_sale:
        return _render_add_device(
            request, session, status_code=400,
            error=tr("Локация сейчас недоступна. Обновите страницу и попробуйте снова."),
        )
    is_vless = node.protocol == NodeProtocol.VLESS_REALITY
    try:
        if is_vless:
            # VLESS: no key needed — the server issues a UUID; there is no IP lease (Xray NATs through the node's IP)
            peer, _client = create_vless_peer(session, account, node, name=name or None)
            cfg = None
        else:
            if not public_key.strip():
                return _render_add_device(
                    request, session, status_code=400,
                    error=tr("Не передан ключ устройства. Обновите страницу и попробуйте снова."),
                )
            peer, cfg = create_peer(
                session, account, node, public_key=public_key.strip(), name=name or None
            )
    except DeviceLimitReached:
        return _render_add_device(
            request, session, status_code=400,
            error=tr("Достигнут лимит устройств. Удалите одно, чтобы добавить новое."),
        )
    except AccountNotActive:
        return _render_add_device(
            request, session, status_code=400,
            error=tr("Аккаунт ещё не активен — ждём подтверждения оплаты, обычно 10–40 минут."),
        )
    except IntegrityError:
        # uq_peer_node_pubkey: this key is already registered on this node. Reachable by simply
        # resubmitting the form (Back + resend, double click), and it used to escape as a bare 500 —
        # the customer sees "Internal Server Error" on a page that was working a second ago.
        session.rollback()
        return _render_add_device(
            request, session, status_code=400,
            error=tr("Это устройство уже добавлено — сгенерируйте новый ключ."),
        )
    if node.agent_secret:
        from app.services import node_client

        try:
            # dispatch branches on peer.protocol_type: VLESS → xray api, WG → wg set (lease=None for VLESS)
            node_client.dispatch_add_peer(node, peer, peer.ip_lease)
        except Exception as e:  # noqa: BLE001 — any dispatch failure = the peer was not created on the node
            # Do NOT hand out a dead config: the peer was not applied on the node. We roll back the
            # peer (+ the IP lease for WG, not yet committed) and show a clear error with a retry.
            log.warning("dispatch add_peer failed node=%s: %s", node.id, type(e).__name__)
            session.rollback()
            return _render_add_device(
                request, session, status_code=503,
                error=tr("Не удалось активировать устройство на сервере — попробуйте снова через минуту."),
            )
    session.commit()
    if is_vless:
        return T.TemplateResponse(
            request, "device_result.html",
            _ctx(
                request,
                page="devices",
                device_name=peer.name or "privacyvpn",
                location=node.location_name,
                vless_link=to_vless_link(node, peer, label=f"privacyvpn {node.location_name}"),
            ),
        )
    return T.TemplateResponse(
        request, "device_result.html",
        _ctx(
            request,
            page="devices",
            device_name=peer.name or "privacyvpn",
            location=node.location_name,
            # tr: the .conf carries a user-facing comment (the browser-keygen privacy claim).
            # Without it every English customer downloads a file with a Russian line in it.
            conf=render_conf(cfg, tr=tr),
            conf_marker=render_conf(cfg, private_key_placeholder="__PRIV__", tr=tr),
            # The pubkey we actually stored for this peer. tt-glue.js injects the browser's private
            # key only if it derives to this one — a stash left over from another tab/reload belongs
            # to a different keypair, and that .conf can never handshake (the node drops unknown
            # keys silently, so the customer just sees "Received 0 B").
            device_public_key=peer.public_key,
        ),
    )


@router.post("/devices/{peer_id}/remove")
def device_remove(peer_id: str, request: Request, session: Session = Depends(get_session)):
    account = current_account(request, session)
    if account is None:
        return RedirectResponse("/login", status_code=303)
    peer = session.get(Peer, uuid.UUID(peer_id))
    if peer is not None and peer.account_id == account.id:
        node = session.get(Node, peer.node_id)
        # snapshot before deletion (remove_device drops the peer): what to dispatch the removal to the node with
        is_vless = peer.protocol_type == NodeProtocol.VLESS_REALITY
        vless_email = peer.vless_email
        pubkey = remove_device(session, peer)
        if node and node.agent_secret:
            from app.services import node_client

            try:
                if is_vless:
                    node_client.dispatch_remove_peer(node, protocol="vless_reality", email=vless_email)
                else:
                    node_client.dispatch_remove_peer(node, pubkey)
            except Exception as e:
                # The node did NOT confirm the removal. Committing here would drop the DB row while
                # the peer keeps forwarding traffic on the node: access survives "deletion", invisible
                # to the cabinet and to billing, and unremovable afterwards (nothing left to dispatch
                # from). Keep the device instead and say so — a failed delete the customer can retry
                # beats a silent one he cannot.
                log.warning("dispatch remove_peer failed node=%s peer=%s: %s", node.id, peer.id, e)
                session.rollback()
                return RedirectResponse("/dashboard?remove_failed=1", status_code=303)
        session.commit()
    return RedirectResponse("/dashboard", status_code=303)


# Why a promo code needs its OWN error keys and not just a string: cabinet code that builds a
# user-facing message must hand the translator a literal it can look up (scripts/check_i18n.py
# enforces this), so the mapping lives here, once, next to the page that renders it.
def _promo_error_text(reason: str, tr) -> str:
    return {
        # unknown and operator-disabled deliberately share a message — see services/promo._check
        "unknown": tr("Промокод не найден или больше не действует."),
        "expired": tr("Срок действия промокода истёк."),
        "exhausted": tr("Лимит использований промокода исчерпан."),
        "throttled": tr("Слишком много попыток с промокодом. Подождите минуту."),
    }.get(reason, tr("Промокод не найден или больше не действует."))


def _active_plans(session):
    return session.execute(
        select(PricePlan).where(PricePlan.is_active.is_(True)).order_by(PricePlan.sort_order)
    ).scalars().all()


def _sellable_locations(session) -> list[str]:
    """Exit locations we actually sell — for the PUBLIC pages.

    Until now the country list existed only behind the login, in the add-device picker, so a
    visitor (and anything reading the page for them) could not learn where the exits are — one of
    the first things anyone asks about a VPN. Data-driven for the same reason the landing prices
    are: a hardcoded list is a lie waiting to happen the first time a node is added or pulled from
    sale. Same predicate as _render_add_device, so what the public pages promise and what the
    cabinet actually offers cannot drift apart.
    """
    return list(
        session.execute(
            select(Node.location_name)
            .where(Node.available_for_sale.is_(True))
            .distinct()
            .order_by(Node.location_name)
        ).scalars().all()
    )


def _render_buy(
    request: Request,
    session: Session,
    tr,
    *,
    status_code: int = 200,
    error: str | None = None,
    sel_plan: str | None = None,
    sel_method: str | None = None,
    promo_code: str = "",
    promo_percent: int = 0,
    promo_error: str | None = None,
    account_number: str = "",
    pending_invoice_id=None,
):
    """The ONE renderer for /buy. Every path — form, 400, 404, 429, applied promo — goes through it.

    /buy used to be re-assembled inline at six call sites, each repeating the plan query and the
    context. Adding a discount to that shape means six places that must remember to carry the promo
    through; the first one that forgets shows the customer a full price while the code is still in
    the form. One function, one set of arguments, no path that can quietly drop the state.

    account_number is echoed back for the same reason. Every error re-render used to wipe a typed
    renewal number, so a customer who hit any /buy error had to retype a 20-digit credential from
    their password manager — and «Применить» is now a second way to reach a re-render, which would
    have made that far more frequent. It is the customer's own number going back into the customer's
    own page (the invoice page already prints it in full); the admin console is the place where
    echoing someone's number back is wrong, and it deliberately does not.
    """
    plans = _active_plans(session) if get_settings().purchases_open else []
    return T.TemplateResponse(
        request, "buy.html",
        _ctx(
            request,
            page="renew",
            plans=_plans_view(plans, tr, promo_percent),
            locations=_sellable_locations(session),
            methods=_methods(tr),
            sel_plan=sel_plan,
            # keep the default plan/method pair BUYABLE — see _default_method_for
            sel_method=sel_method or (
                _default_method_for(plans[0], promo_percent) if plans else None
            ),
            purchases_open=get_settings().purchases_open,
            error=error,
            promo_code=promo_code,
            promo_percent=promo_percent,
            promo_error=promo_error,
            account_number=account_number.strip()[:32],
            pending_invoice_id=pending_invoice_id,
        ),
        status_code=status_code,
    )


def _resolve_promo(request: Request, session: Session, raw: str):
    """(promo, reason) for a customer-entered code. Both None → no code was entered.

    Failures — and only failures — draw on a global budget, so a customer with a working code is
    never throttled. `buy_limiter` does not cover this: the preview path creates no invoice, so a
    code-guesser never touches that bucket. The budget is global because Caddy strips the client IP
    by design (no-logs), and a global ceiling is the honest version of what we can actually enforce.

    Be clear about what this does NOT do: it does not stop enumeration. Whether a code is real is
    visible from whether the price moved, and that stays true with the budget spent — verified.
    Closing it would mean refusing VALID codes while throttled, which just converts the oracle into
    a cheap way to switch promo codes off for every real customer. So this is a flood ceiling, and
    the actual defences are keyspace (promo.MIN_CODE_LEN) and the fact that a guessed code only
    buys the discount the operator already decided to give away, bounded by max_uses.
    """
    if not raw.strip():
        return None, None
    try:
        return promo_service.lookup(session, raw), None
    except PromoError as e:
        if not promo_limiter.allow("promo"):
            return None, "throttled"
        return None, e.reason


@router.get("/buy", response_class=HTMLResponse)
def buy_form(request: Request, session: Session = Depends(get_session)):
    return _render_buy(request, session, translator(get_lang(request)))


@router.post("/buy", response_class=HTMLResponse)
def buy(
    request: Request,
    plan_code: str = Form(...),
    method: str = Form(...),
    account_number: str = Form(""),
    promo_code: str = Form(""),
    # Which button was pressed. "apply" only re-prices the page; anything else (including a form
    # posted without the field at all) creates the invoice — so the pre-promo contract of this
    # endpoint, and every caller written against it, keeps working unchanged.
    action: str = Form("create"),
    session: Session = Depends(get_session),
):
    tr = translator(get_lang(request))
    if not get_settings().purchases_open:
        return _render_buy(request, session, tr, status_code=403)

    # The code is validated on BOTH paths and mutates nothing here: Apply must be repeatable
    # (a customer will press it, change the plan, press it again) and a rejected code must never
    # leave a half-priced invoice behind. The reservation happens once, far below, right before
    # the purchase.
    promo, promo_reason = _resolve_promo(request, session, promo_code)
    percent = promo.percent_off if promo is not None else 0
    # What goes back into the input. A valid code is echoed NORMALIZED (so the customer sees the
    # canonical form we matched); a rejected one is echoed as typed, so they can spot their own
    # typo — capped, because it is reflected into the page.
    promo_shown = (
        promo_service.normalize(promo_code) if promo is not None else promo_code.strip()[:32]
    )

    # The Apply button (action="apply") is the FIRST submit button in the form, which by the HTML
    # implicit-submission rules makes it the form's default — so with JS off (the mandated Tor
    # "Safest" setting) ENTER anywhere in the form lands here, not on the Create-invoice submit.
    # With an empty promo field that
    # rendered a byte-identical page: a keyboard user on Tor pressed ENTER, waited out a circuit
    # round trip, saw the same form, and concluded checkout was broken. Nothing was created, so the
    # abandonment is invisible in every metric we have. There is nothing to preview without a code,
    # so treat it as the purchase they meant.
    previewing = action == "apply" and bool(promo_code.strip())

    if previewing or promo_reason is not None:
        # A bad code stops here even if the customer pressed Create invoice: minting an invoice at
        # full price while the form still shows their code would charge them the thing they were
        # trying not to pay. Say why, keep their selection, let them fix it.
        #
        # Metered, because this branch returns BEFORE buy_limiter: a DB lookup plus a full Jinja
        # render, unauthenticated and unbounded, is the cheapest DoS surface on the site — and it is
        # reachable over .onion where there is no client IP to key on. Guessing codes and flooding
        # the renderer are the same request, so one budget covers both.
        if not preview_limiter.allow("preview"):
            return _render_buy(
                request, session, tr, status_code=429,
                sel_plan=plan_code, sel_method=method, account_number=account_number,
                promo_code=promo_shown,
                error=tr("Слишком много запросов. Подождите минуту и попробуйте снова."),
            )
        # Do not hand back a selection the discount just made unpayable. _render_buy's
        # `sel_method or default` can never reach its discount-aware fallback here, because every
        # POST supplies `method` as a required field — so without this the customer applies a code
        # and the very next click is a guaranteed 400 on a coin the new price cannot clear. This is
        # a preview, nothing is committed, and the card already lists which coins can pay.
        shown_method = method
        if promo_reason is None and percent:
            plan = session.execute(
                select(PricePlan).where(PricePlan.code == plan_code, PricePlan.is_active.is_(True))
            ).scalar_one_or_none()
            try:
                chosen = PaymentMethod(method)
            except ValueError:
                chosen = None
            if plan is not None and chosen is not None and not min_tx.gate_ok(
                chosen, discounted_cents(plan.price_fiat_cents, percent)
            ):
                shown_method = _default_method_for(plan, percent)
        return _render_buy(
            request, session, tr,
            status_code=200 if promo_reason is None else 400,
            sel_plan=plan_code, sel_method=shown_method, account_number=account_number,
            promo_code=promo_shown, promo_percent=percent,
            promo_error=_promo_error_text(promo_reason, tr) if promo_reason else None,
        )

    # anti-abuse: a global cap on invoice creation (see buy_limiter). Checked only on the create
    # path — the preview above touches no provider and mints nothing.
    if not buy_limiter.allow("buy"):
        return _render_buy(
            request, session, tr, status_code=429,
            sel_plan=plan_code, sel_method=method, account_number=account_number,
            promo_code=promo_shown, promo_percent=percent,
            error=tr("Слишком много запросов. Подождите минуту и попробуйте снова."),
        )
    plan = session.execute(
        select(PricePlan).where(PricePlan.code == plan_code, PricePlan.is_active.is_(True))
    ).scalar_one_or_none()
    if plan is None:
        return RedirectResponse("/buy", status_code=303)
    try:
        m = PaymentMethod(method)
    except ValueError:
        return RedirectResponse("/buy", status_code=303)
    if m not in _OFFERED_METHODS:
        # A valid enum member we do not sell (monero / lightning). Must be refused BEFORE
        # _provider_for, which would happily route it to the NOWPayments gateway and mint a real
        # invoice for coins with no payout address configured.
        return RedirectResponse("/buy", status_code=303)
    provider = _provider_for(m)
    if provider is None:
        return _render_buy(
            request, session, tr, status_code=400,
            sel_plan=plan_code, sel_method=method, account_number=account_number,
            promo_code=promo_shown, promo_percent=percent,
            error=tr("Способ оплаты сейчас недоступен."),
        )
    # Crypto minimum, measured against the amount the customer will REALLY send. Below BlockBee's
    # forwarding threshold the transfer does not go through and the coins are stuck at the deposit
    # address — unrecoverable, by us or by them. This is the reason the gate runs on the discounted
    # figure and BEFORE any invoice exists: $13.50 −15% = $11.48 clears the plan's list price
    # against the $12 USDT threshold and fails the real one. Nothing has been created yet, so the
    # customer just picks another coin.
    payable_cents = discounted_cents(plan.price_fiat_cents, percent)
    if not min_tx.gate_ok(m, payable_cents):
        return _render_buy(
            request, session, tr, status_code=400,
            sel_plan=plan_code, sel_method=method, account_number=account_number,
            promo_code=promo_shown, promo_percent=percent,
            error=(
                tr("С учётом промокода к оплате {amount} — эта монета принимает от {min}. "
                   "Возьмите план подлиннее или оплатите в Litecoin (LTC).")
                if percent
                else tr("Эта монета — от {min}. Возьмите план подлиннее или оплатите в Litecoin (LTC).")
            ).format(
                min=f"${min_tx.min_usd_for(m)}",
                amount=f"${payable_cents / 100:.2f}",
            ),
        )
    # Resubmit guard. This handler answers POST with a rendered page, not a redirect, because the
    # account number is displayed exactly once and the server cannot re-derive it (it keeps only a
    # hash) — so classic POST-Redirect-GET is not available here. That leaves F5 / Back+resend
    # re-running the purchase: it used to mint a SECOND account and silently swap the session
    # cookie to it, so the customer ends up saving account #2 while his coins pay invoice #1.
    # The reload carries the cookie the first response set, so "has a session AND a live pending
    # invoice, and is not renewing" identifies a resubmit precisely. Send him back to the invoice
    # he already has instead of minting another. It also keeps a resubmit from spending a SECOND
    # use of the promo code on an invoice the customer never asked for twice.
    #
    # But "has a pending invoice" is NOT the same as "pressed F5". The guard matched on nothing but
    # the account and the invoice's existence, so it also swallowed a genuinely DIFFERENT purchase:
    # buy at list price, then get a code, press Apply, watch the page quote $10.80, press
    # Create invoice — and get redirected to the old $13.50 invoice with no error, no banner and no
    # promo badge. The customer pays a price they were shown they would not pay, in non-reversible
    # crypto. buy.html promises in as many words that the discount reaches the invoice; this broke
    # that promise silently. So: only a request that would produce the SAME invoice is a resubmit.
    if not account_number.strip():
        prior = current_account(request, session)
        if prior is not None:
            pending = session.execute(
                select(Invoice)
                .where(
                    Invoice.account_id == prior.id,
                    Invoice.status.in_((InvoiceStatus.CREATED, InvoiceStatus.AWAITING_CONFIRMATION)),
                )
                .order_by(Invoice.created_at.desc())
            ).scalars().first()
            if pending is not None and as_utc(pending.expires_at) > datetime.now(UTC):
                same = (
                    # A resubmit produces the SAME invoice — so compare EVERY field of the pending
                    # invoice that the customer will be charged for or provisioned from, and treat
                    # any divergence as a different order (409, never a silent redirect). This guard
                    # has been a game of dimension whack-a-mole: it redirected on any live invoice
                    # (charged list price to someone shown a discount), then compared price but not
                    # the plan (5 devices delivered for a 10-device purchase). The rule that ends it
                    # is not "find the missing field" but "compare all of them".
                    #
                    # The two settlement reads are asymmetric, which is why both an id AND value
                    # comparisons are needed: device_limit is read LIVE off the plan at settlement
                    # (purchase.py) so price_plan_id must pin the plan row, while days_granted and
                    # amount_fiat_cents are FROZEN on the invoice at creation so they must be matched
                    # against the plan's current values. days_granted specifically covers an operator
                    # lengthening/shortening a plan in place (admin_service.update_plan permits it,
                    # though the admin route does not expose it today) — without it a resubmit would
                    # settle the stale frozen term while the page advertised the new one.
                    pending.price_plan_id == plan.id
                    and pending.days_granted == plan.duration_days
                    and pending.amount_fiat_cents == payable_cents
                    and pending.method == m
                    and pending.discount_percent == percent
                )
                if same:
                    return RedirectResponse(f"/invoice/{pending.id}", status_code=303)
                # A different purchase than the live invoice. We cannot just mint the new one: with
                # no account number in hand start_purchase would mint a second ACCOUNT and swap the
                # session cookie to it — the original bug this guard exists for. And we must not
                # silently redirect either. So say it plainly, name the amount they would actually
                # be paying, and link the invoice they already have.
                return _render_buy(
                    request, session, tr, status_code=409,
                    sel_plan=plan_code, sel_method=method, account_number=account_number,
                    promo_code=promo_shown, promo_percent=percent,
                    error=tr(
                        "У вас уже есть неоплаченный счёт на {amount} — этот заказ отличается, "
                        "и второй счёт мы не создаём. Оплатите текущий или дождитесь, когда он "
                        "закроется, и оформите заказ заново."
                    ).format(
                        amount=f"{pending.amount_fiat_cents / 100:.2f} {pending.fiat_currency}"
                    ),
                    pending_invoice_id=pending.id,
                )
    try:
        if promo is not None:
            # Takes the promo row lock and re-validates: another buyer may have taken the last use,
            # or the operator switched the code off, since this customer loaded the page.
            #
            # The lock IS held across the provider HTTP call below, until this transaction commits.
            # That is on purpose and it is the cost of an exact max_uses: releasing early would mean
            # committing the reservation before we know the purchase succeeds, and a process death
            # in that window leaks a use permanently (no invoice would exist for expiry to refund
            # it from). What it costs is that two people redeeming the SAME code at the same instant
            # serialize behind one BlockBee round-trip — per code, not per customer, and only while
            # a quote is in flight. Everything below rolls back on failure, which returns the use.
            promo_service.reserve(session, promo)
        res = start_purchase(
            session, price_plan=plan, method=m,
            account_number=account_number.strip() or None, provider=provider,
            discount_percent=percent,
            promo_code_id=promo.id if promo is not None else None,
        )
    except PromoError as e:
        session.rollback()
        return _render_buy(
            request, session, tr, status_code=400,
            sel_plan=plan_code, sel_method=method, account_number=account_number,
            promo_code=promo_shown,
            promo_error=_promo_error_text(e.reason, tr),
        )
    except UnknownAccount:
        # rollback, not just an early return: the promo use reserved above must go back, otherwise
        # a customer fat-fingering their account number silently burns one redemption per attempt.
        session.rollback()
        return _render_buy(
            request, session, tr, status_code=404,
            sel_plan=plan_code, sel_method=method, account_number=account_number,
            promo_code=promo_shown, promo_percent=percent,
            error=tr("Номер аккаунта не найден."),
        )
    except (ValueError, httpx.HTTPError) as e:
        # a provider-side invoice creation failure (no payout / BlockBee API unavailable / broken
        # quote) — we don't throw a 500, we degrade to a friendly error. The invoice is not
        # committed, and the rollback also returns the reserved promo use.
        log.warning("start_purchase failed method=%s: %s", m.value, type(e).__name__)
        session.rollback()
        return _render_buy(
            request, session, tr, status_code=400,
            sel_plan=plan_code, sel_method=method, account_number=account_number,
            promo_code=promo_shown, promo_percent=percent,
            error=tr("Способ оплаты сейчас недоступен. Попробуйте другую монету или позже."),
        )
    cid = request.cookies.get("cid")
    if res.is_new and cid:
        res.account.attribution_click_id = cid
    session.commit()
    resp = T.TemplateResponse(
        request, "invoice.html",
        _ctx(request, invoice=res.invoice, account_number=res.account_number, is_new=res.is_new,
             pay_deadline=_pay_deadline(res.invoice)),
    )
    if res.is_new:
        set_cookie(
            resp, "session",
            sessions.issue(res.account.id, scope="cabinet", binding=str(res.account.session_epoch)),
        )
    return resp


@router.get("/invoice/{invoice_id}", response_class=HTMLResponse)
def invoice_status(invoice_id: str, request: Request, session: Session = Depends(get_session)):
    from app.models import Invoice

    # ownership guard (IDOR): without it anyone knowing an invoice UUID would see its payment
    # metadata. A new account gets a cabinet cookie in POST /buy, a renewing one is already
    # logged in → owner.
    account = current_account(request, session)
    if account is None:
        return RedirectResponse("/login", status_code=303)
    try:
        inv = session.get(Invoice, uuid.UUID(invoice_id))
    except ValueError:
        return RedirectResponse("/dashboard", status_code=303)
    if inv is None or inv.account_id != account.id:
        return RedirectResponse("/dashboard", status_code=303)
    return T.TemplateResponse(
        request, "invoice.html",
        _ctx(request, invoice=inv, account_number=None, is_new=False,
             pay_deadline=_pay_deadline(inv)),
    )


# --- Gift tokens ----------------------------------------------------------------------------------
# Same contract as _promo_error_text: the service raises stable keys, the page owns the wording.
def _gift_error_text(reason: str, tr) -> str:
    return {
        "unknown": tr("Код не найден. Проверьте, что он введён целиком, вместе с TT-."),
        "redeemed": tr("Этот код уже погашен."),
        "disabled": tr("Код отключён оператором — напишите в поддержку."),
        "expired": tr("Срок действия кода истёк."),
        "throttled": tr("Слишком много попыток. Подождите минуту."),
        "empty": tr("Введите подарочный код."),
    }.get(reason, tr("Код не найден. Проверьте, что он введён целиком, вместе с TT-."))


def _render_redeem(
    request: Request,
    session: Session,
    *,
    status_code: int = 200,
    token: str = "",
    error: str | None = None,
    preview: dict | None = None,
    created: dict | None = None,
    extended: dict | None = None,
):
    """The ONE renderer for /redeem — form, preview, both results and every error.

    `logged_in` comes from the SESSION and nowhere else: an account-number field here would make the
    page answer "does this account exist?" to anyone who asks.
    """
    return T.TemplateResponse(
        request, "redeem.html",
        _ctx(
            request,
            logged_in=current_account(request, session) is not None,
            # echoed so a typo can be fixed instead of retyped; capped, it is reflected into the page
            token=token.strip()[:64],
            error=error,
            preview=preview,
            created=created,
            extended=extended,
        ),
        status_code=status_code,
    )


@router.get("/redeem", response_class=HTMLResponse)
def redeem_form(request: Request, session: Session = Depends(get_session)):
    return _render_redeem(request, session)


@router.post("/redeem", response_class=HTMLResponse)
def redeem_submit(
    request: Request,
    token: str = Form(""),
    # "check" previews, anything else redeems. Two steps because the second is irreversible and
    # shows a credential that cannot be shown twice.
    action: str = Form("check"),
    session: Session = Depends(get_session),
):
    """Redeem a gift token. Answers with a BODY, never a redirect: a 303 would park a live account
    number in history and in every proxy log (same rule as admin_ui.accounts_create).

    `purchases_open` is deliberately not checked — the money changed hands elsewhere, possibly weeks
    ago, and refusing while the shop is shut would strand a customer who already paid.
    """
    tr = translator(get_lang(request))
    account = current_account(request, session)  # the session, never a form field
    if not token.strip():
        return _render_redeem(
            request, session, status_code=400, error=_gift_error_text("empty", tr)
        )

    def _refuse(reason: str):
        # only FAILURES draw on the budget — see webui.redeem_limiter
        if not redeem_limiter.allow("redeem"):
            reason = "throttled"
        return _render_redeem(
            request, session,
            status_code=429 if reason == "throttled" else 400,
            token=token, error=_gift_error_text(reason, tr),
        )

    try:
        row = gift.lookup(session, token)
    except GiftError as e:
        return _refuse(e.reason)

    if action != "redeem":
        return _render_redeem(
            request, session, token=token,
            preview={
                "days": row.days,
                # a token issued without an explicit limit hands out the server default
                "device_limit": row.device_limit or get_settings().default_device_limit,
            },
        )

    try:
        res = gift.redeem(session, token, account=account)
    except GiftError as e:
        # redeemed between the preview and this submit, or the batch was pulled — nothing was written
        session.rollback()
        return _refuse(e.reason)
    # committed BEFORE the number is rendered — see gift.redeem
    session.commit()

    if res.number is None:
        return _render_redeem(
            request, session,
            extended={"days": res.days, "expires_at": _fmt(res.expires_at)},
        )
    resp = _render_redeem(
        request, session,
        created={
            "number": res.number,
            "days": res.days,
            "device_limit": res.device_limit,
            "expires_at": _fmt(res.expires_at),
        },
    )
    # Logged in on this device, as a fresh purchase does (POST /buy). Convenience only: the cookie
    # expires, the number is the only way back, and the page says so.
    set_cookie(
        resp, "session",
        sessions.issue(res.account.id, scope="cabinet", binding=str(res.account.session_epoch)),
    )
    return resp


@router.post("/email")
def email_update(
    request: Request,
    email: str = Form(""),
    action: str = Form("save"),
    session: Session = Depends(get_session),
):
    account = current_account(request, session)
    if account is None:
        return RedirectResponse("/login", status_code=303)
    try:
        if action == "clear":
            account_email.clear_email(session, account)
        elif email:
            account_email.set_email(session, account, email)
        session.commit()
    except Exception:
        pass
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/guides", response_class=HTMLResponse)
def guides(request: Request):
    return T.TemplateResponse(request, "guides.html", _ctx(request))


# Guides are server-side Jinja templates (i18n via _()), NOT static: static files lived outside
# the translation dictionary and stayed Russian on the EN site.
_GUIDE_PAGES = ("windows", "macos", "ios", "android", "linux")

# `ru-vless` is the RU-transport guide (VLESS+Reality, Hiddify client). Its template ships, but the
# route is CLOSED until a live RU test clears (docs/ru-validation.md §4) — advertising a transport
# we cannot yet sell is the overclaim rule 6 of CLAUDE.md forbids.
#
# This used to be a tuple entry that had to be hand-deleted on every single deploy of cabinet.py
# (rule 2 of CLAUDE.md). A manual step on a file we touch often, with nothing asserting the result,
# is a mine: shipping the file whole silently published the guide, and the whole test suite stayed
# green. The env gate makes the safe state the DEFAULT and the risky one an explicit opt-in.
_GATED_GUIDE_PAGES = {"ru-vless": "guides_ru_vless_enabled"}


def _guide_is_public(os_name: str) -> bool:
    if os_name in _GUIDE_PAGES:
        return True
    flag = _GATED_GUIDE_PAGES.get(os_name)
    return bool(flag) and bool(getattr(get_settings(), flag, False))


@router.get("/guides/{os_name}", response_class=HTMLResponse)
def guide_page(os_name: str, request: Request):
    if not _guide_is_public(os_name):
        raise HTTPException(status_code=404)
    return T.TemplateResponse(request, f"guides/{os_name}.html", _ctx(request))


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots(request: Request):
    """Serving this at all is the point: until now it 404'd, and a 404 is not "allowed" to every
    crawler — Ahmia in particular will not index an onion whose robots.txt it cannot read."""
    return PlainTextResponse(seo.robots_txt(seo.origin_for(request)))


@router.get("/sitemap.xml")
def sitemap(request: Request):
    return Response(seo.sitemap_xml(seo.origin_for(request)), media_type="application/xml")


@router.get("/lang")
def switch_lang(request: Request, to: str = "en", back: str = "/"):
    """Language switcher without JS: a link in the topbar → cookie → redirect back.

    `back` is a local path only (anti open-redirect): absolute URLs,
    protocol-relative `//` and backslash tricks collapse to `/`.
    """
    if to not in ("ru", "en"):
        to = "en"
    if not back.startswith("/") or back.startswith("//") or "\\" in back:
        back = "/"
    resp = RedirectResponse(back, status_code=303)
    resp.set_cookie(
        "lang",
        to,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
    )
    return resp


@router.get("/transparency", response_class=HTMLResponse)
def transparency(request: Request):
    return T.TemplateResponse(request, "transparency.html", _ctx(request))


def _support_ctx() -> dict:
    """Optional public support channels. None → the channel is not rendered."""
    s = get_settings()
    return {
        "sup_bot": s.support_telegram_bot,
        "sup_tg_url": s.support_telegram_url,
        "sup_tg_dm": s.support_telegram_dm,
        "sup_x_url": s.support_x_url,
        "sup_pgp": s.legal_pgp_fingerprint,
    }


@router.get("/support", response_class=HTMLResponse)
def support(request: Request):
    return T.TemplateResponse(request, "support.html", _ctx(request, **_support_ctx()))


@router.get("/faq", response_class=HTMLResponse)
def faq(request: Request, session: Session = Depends(get_session)):
    return T.TemplateResponse(
        request, "faq.html", _ctx(request, locations=_sellable_locations(session))
    )


def _legal_ctx(tr) -> dict:
    # `tr` is required: without incorporation `entity` falls back to a Russian literal, and a
    # a non-translated default leaked onto the EN pages (the operator fallback rendered in Russian)
    # (/privacy, /terms, /aup). Same class as the _METHODS bug — a user-facing string in Python.
    s = get_settings()
    return {
        "entity": s.legal_entity or tr("оператор privacyvpn"),  # honest default until incorporation
        "jurisdiction": s.legal_jurisdiction,
        "pgp": s.legal_pgp_fingerprint,
    }


@router.get("/aup", response_class=HTMLResponse)
def aup(request: Request):
    return T.TemplateResponse(request, "aup.html", _ctx(request, **_legal_ctx(translator(get_lang(request)))))


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return T.TemplateResponse(request, "privacy.html", _ctx(request, **_legal_ctx(translator(get_lang(request)))))


@router.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return T.TemplateResponse(request, "terms.html", _ctx(request, **_legal_ctx(translator(get_lang(request)))))
