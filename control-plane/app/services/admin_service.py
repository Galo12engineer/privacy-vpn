"""Admin operations: plans (data-driven, no code changes), nodes, invoices/subscriptions, peer blocking."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Account, Invoice, Node, Peer, PricePlan, PromoCode, Subscription
from app.models.base import as_utc, utcnow
from app.models.enums import AccountStatus, NodeProtocol
from app.services import awg_profile, billing_fsm, promo, reality_profile
from app.services.account_number import (
    generate_account_number,
    hash_account_number,
    is_valid_format,
)
from app.services.billing import apply_credit, evaluate_status
from app.services.node_registration import mint_registration_token
from app.services.provisioning import remove_device
from app.services.reality_dest import check_dest


# --- Plans (price/days/device limit are editable from the admin panel without code changes) ---
def create_plan(
    session: Session,
    *,
    code: str,
    name: str,
    duration_days: int,
    price_fiat_cents: int,
    fiat_currency: str = "USD",
    device_limit: int = 5,
    sort_order: int = 0,
) -> PricePlan:
    plan = PricePlan(
        code=code,
        name=name,
        duration_days=duration_days,
        price_fiat_cents=price_fiat_cents,
        fiat_currency=fiat_currency,
        device_limit=device_limit,
        sort_order=sort_order,
        is_active=True,
    )
    session.add(plan)
    session.flush()
    return plan


def update_plan(session: Session, plan: PricePlan, **fields) -> PricePlan:
    allowed = {
        "name", "duration_days", "price_fiat_cents", "fiat_currency",
        "device_limit", "sort_order", "is_active", "featured",
    }
    for k, v in fields.items():
        if k in allowed:
            setattr(plan, k, v)
    # invariant "no more than one recommended": featured was turned on for this one → clear it on the others
    if fields.get("featured"):
        others = session.execute(
            select(PricePlan).where(PricePlan.id != plan.id, PricePlan.featured.is_(True))
        ).scalars().all()
        for other in others:
            other.featured = False
    session.flush()
    return plan


def list_plans(session: Session, *, active_only: bool = False) -> list[PricePlan]:
    stmt = select(PricePlan).order_by(PricePlan.sort_order)
    if active_only:
        stmt = stmt.where(PricePlan.is_active.is_(True))
    return list(session.execute(stmt).scalars().all())


# --- Promo codes (percent-off, operator-managed) ---
def create_promo(
    session: Session,
    *,
    code: str,
    percent_off: int,
    expires_at=None,
    max_uses: int | None = None,
    note: str | None = None,
) -> PromoCode:
    """Create a code. Raises ValueError with an operator-readable reason on bad input.

    Validation lives HERE and not only in the form because the form is HTML and HTML validation is
    advisory: `min`/`max` on an <input> is a hint to a browser, not a constraint on a POST. The
    percent ceiling in particular is load-bearing — see promo.MAX_PERCENT for what a 100% code does.
    """
    normalized = promo.normalize(code)
    if len(normalized) < promo.MIN_CODE_LEN:
        raise ValueError(
            f"код слишком короткий: нужно минимум {promo.MIN_CODE_LEN} символов A-Z0-9"
        )
    if not (promo.MIN_PERCENT <= percent_off <= promo.MAX_PERCENT):
        raise ValueError(f"процент вне диапазона {promo.MIN_PERCENT}-{promo.MAX_PERCENT}")
    if max_uses is not None and max_uses < 1:
        raise ValueError("лимит использований должен быть ≥ 1 (пусто = без лимита)")
    if session.execute(
        select(PromoCode).where(PromoCode.code == normalized)
    ).scalar_one_or_none() is not None:
        raise ValueError(f"код {normalized} уже существует")
    row = PromoCode(
        code=normalized,
        percent_off=percent_off,
        expires_at=expires_at,
        max_uses=max_uses,
        # Truncated, not merely `maxlength`-ed in the form: HTML length limits are advisory and a
        # hand-made POST walks past them. Over-long text is a Postgres-ONLY 500 ("value too long for
        # type character varying") that SQLite accepts silently, so the tests would never see it —
        # the exact class of bug this repo has been bitten by before. `code` is safe already
        # (promo.normalize slices to 32).
        note=(note or "").strip()[:200] or None,
        is_active=True,
    )
    session.add(row)
    session.flush()
    return row


def update_promo(session: Session, code_row: PromoCode, **fields) -> PromoCode:
    """Edit an existing code. The code STRING itself is immutable — see below.

    Renaming a live code would silently orphan every copy of it already handed out (customers hold
    the old string; it would stop working with no trace of why). `used_count` is not editable
    either: it is the record of what was given away. To retire a code, switch it off — that keeps
    the counter, whereas deleting the row would erase the only evidence of how much was discounted.
    """
    allowed = {"percent_off", "expires_at", "max_uses", "is_active", "note"}
    if "percent_off" in fields and not (
        promo.MIN_PERCENT <= fields["percent_off"] <= promo.MAX_PERCENT
    ):
        raise ValueError(f"процент вне диапазона {promo.MIN_PERCENT}-{promo.MAX_PERCENT}")
    if fields.get("max_uses") is not None and fields["max_uses"] < 1:
        raise ValueError("лимит использований должен быть ≥ 1 (пусто = без лимита)")
    for k, v in fields.items():
        if k in allowed:
            setattr(code_row, k, v)
    session.flush()
    return code_row


def list_promos(session: Session) -> list[PromoCode]:
    return list(
        session.execute(select(PromoCode).order_by(PromoCode.created_at.desc())).scalars().all()
    )


# --- Accounts (operator manual grant/extend) ---
def find_account_by_number(session: Session, raw_number: str):
    """Look up an account by the number the CUSTOMER provides.

    The number is never stored — only its HMAC hash is (account_number_hash). We hash the operator's
    input the same way and match. Returns the Account or None. The lookup is by POST body, never a
    URL param, so the secret does not land in access logs.
    """
    if not is_valid_format(raw_number):
        return None
    h = hash_account_number(raw_number, get_settings().account_number_pepper)
    return session.execute(
        select(Account).where(Account.account_number_hash == h)
    ).scalar_one_or_none()


def mask_account_id(account_id) -> str:
    """The only identifier the console can print for an account.

    NOT a shortened account number — the number is unknown to the server (only its HMAC lives in the
    row), so a listing cannot show one however convenient that would be. Last 4 hex of the internal
    UUID: enough for an operator to tell two rows apart on screen and to quote in their own notes,
    useless to anyone who does not already hold the row.
    """
    return "•••• " + str(account_id).replace("-", "")[-4:]


ACCOUNT_PAGE_SIZE = 50
ACCOUNT_FILTERS = ("active", "grace", "expired")


def _account_status_clause(status: str, *, now: datetime, grace_days: int):
    """SQL mirror of billing.evaluate_status — or None for "no filter".

    Deliberately derived from expires_at and NOT from the account.status COLUMN: that column is a
    cached snapshot refreshed by scheduler/lifecycle.refresh_statuses, so between two runs (or on a
    scheduler that stalled) it lags reality. Filtering by it would quietly hide accounts that just
    fell into grace, and — worse — disagree with the status badge on the very same row, which is
    computed live by evaluate_status. One rule, expressed twice: here for the WHERE (pagination
    forces the filter into SQL — the console must survive thousands of rows), and in Python for the
    badge. test_admin_accounts asserts the two agree on the boundaries.
    """
    grace_edge = now - timedelta(days=grace_days)
    if status == "active":
        return and_(Account.expires_at.is_not(None), Account.expires_at > now)
    if status == "grace":
        # exp <= now (not active any more) AND now < exp + grace  ⟺  exp > now - grace
        return and_(
            Account.expires_at.is_not(None),
            Account.expires_at <= now,
            Account.expires_at > grace_edge,
        )
    if status == "expired":
        # never paid (expires_at IS NULL) or the grace window has run out
        return or_(Account.expires_at.is_(None), Account.expires_at <= grace_edge)
    return None


def count_accounts(session: Session, *, status: str | None = None, now=None, grace_days=None) -> int:
    now = now or utcnow()
    grace_days = grace_days if grace_days is not None else get_settings().grace_period_days
    stmt = select(func.count()).select_from(Account)
    clause = _account_status_clause(status, now=now, grace_days=grace_days) if status else None
    if clause is not None:
        stmt = stmt.where(clause)
    return int(session.execute(stmt).scalar_one())


def list_account_rows(
    session: Session,
    *,
    status: str | None = None,
    limit: int = ACCOUNT_PAGE_SIZE,
    offset: int = 0,
    now=None,
    grace_days=None,
) -> list[dict]:
    """One page of accounts as display rows. NEVER contains an account number — see mask_account_id.

    Paginated in SQL (LIMIT/OFFSET), and the device counts come from ONE grouped query over the
    page's ids rather than a peer lookup per row: at three accounts either shape works, at three
    thousand only this one does.
    """
    now = now or utcnow()
    grace_days = grace_days if grace_days is not None else get_settings().grace_period_days
    stmt = select(Account)
    clause = _account_status_clause(status, now=now, grace_days=grace_days) if status else None
    if clause is not None:
        stmt = stmt.where(clause)
    # id as the tie-breaker: created_at alone is not unique (a seed/import can stamp a batch with
    # the same instant), and a non-deterministic sort makes LIMIT/OFFSET drop or repeat rows.
    accounts = list(
        session.execute(
            stmt.order_by(Account.created_at.desc(), Account.id).limit(limit).offset(offset)
        ).scalars().all()
    )
    ids = [a.id for a in accounts]
    counts: dict = {}
    if ids:
        counts = dict(
            session.execute(
                select(Peer.account_id, func.count(Peer.id))
                .where(Peer.account_id.in_(ids))
                .group_by(Peer.account_id)
            ).all()
        )
    rows = []
    for a in accounts:
        exp = as_utc(a.expires_at)
        created = as_utc(a.created_at)
        rows.append(
            {
                "id": a.id,
                "masked": mask_account_id(a.id),
                "status": evaluate_status(a, now=now, grace_days=grace_days).value,
                "expires_at": exp.strftime("%d.%m.%Y %H:%M UTC") if exp else "—",
                "created_at": created.strftime("%d.%m.%Y") if created else "—",
                "devices": counts.get(a.id, 0),
                "device_limit": a.device_limit,
            }
        )
    return rows


OPERATOR_GRANT_CODE = "operator-grant"


def create_account(
    session: Session,
    *,
    days: int,
    device_limit: int | None = None,
    price_plan_code: str = OPERATOR_GRANT_CODE,
    now: datetime | None = None,
) -> tuple[Account, str]:
    """Mint an account and credit `days` to it. Returns (account, number_plaintext).

    The plaintext number is generated here, hashed into the row and HANDED BACK ONCE — it is stored
    nowhere and cannot be recovered (models/account.py keeps only the HMAC). Every caller is
    therefore obliged to show it to a human immediately and to keep it out of logs, URLs and
    redirects. Shared by the admin console and scripts/grant_time.py --new so that the two cannot
    drift apart on what "create an account with time on it" means.

    Money-equivalent: this hands out paid time for free. It is not idempotent by design — calling it
    twice makes two accounts, which is what "create another one" has to mean.
    """
    if days <= 0:
        raise ValueError("дней должно быть больше нуля")
    settings = get_settings()
    number = generate_account_number()
    account = Account(
        account_number_hash=hash_account_number(number, settings.account_number_pepper),
        status=AccountStatus.EXPIRED,
        device_limit=device_limit or settings.default_device_limit,
    )
    session.add(account)
    session.flush()
    apply_credit(
        session,
        account,
        days=days,
        now=now,
        price_plan_code=price_plan_code,
        device_limit=device_limit,
    )
    return account, number


def rescuable_invoices(session: Session, account):
    """This account's invoices that reconcile may still AUTO-credit on its own.

    Load-bearing for the manual grant. Expiry parks every invoice that had a live deposit address in
    LATE_PAYMENT so late-arriving coins settle themselves; reconcile then polls it for a while. If
    the operator ALSO credits the customer by hand ("I paid late" → support → extend), the same
    payment would land twice — so the console surfaces these and the operator LINKS the grant to the
    one it pays off. The double payout is then impossible because apply_credit is idempotent per
    invoice_id (with a DB UNIQUE behind it). Note what we deliberately do NOT do: touch the invoice.
    Marking it settled up front stranded genuine payments and scrubbed their evidence.
    """
    from app.models import Invoice
    from app.models.enums import InvoiceStatus

    return session.execute(
        select(Invoice)
        .where(Invoice.account_id == account.id, Invoice.status == InvoiceStatus.LATE_PAYMENT)
        .order_by(Invoice.created_at.desc())
    ).scalars().all()


def grant_days(
    session: Session, account, *, days: int, plan: PricePlan | None = None, settle_invoice=None
):
    """Manually credit N days to an account (operator action, e.g. a payment handled off-band).

    Reuses the exact same apply_credit path as a real payment: days stack on max(now, expires_at),
    the account goes ACTIVE, reminder timers reset. If a plan is given, its device_limit and code
    are recorded on the subscription (so the ledger shows what was granted).

    settle_invoice: the ONE invoice this grant pays off, picked explicitly by the operator (never
    inferred — an account can hold both an abandoned cart and a real late payment, and guessing
    wrong strands the customer's coins). It is only RECORDED on the ledger row; the invoice itself
    is left untouched, so it stays pollable and keeps its forensic fields. Because apply_credit is
    idempotent per invoice_id, reconcile later settling that same invoice credits nothing extra —
    which is what makes "customer says I paid late" → manual extend safe from a double payout.
    Pass None for an unrelated goodwill grant.
    """
    return apply_credit(
        session,
        account,
        days=days,
        price_plan_code=plan.code if plan else None,
        device_limit=plan.device_limit if plan else None,
        invoice_id=settle_invoice.id if settle_invoice is not None else None,
    )


# --- Nodes ---
def add_node(
    session: Session,
    *,
    name: str,
    endpoint_host: str,
    location_code: str,
    location_name: str,
    subnet_v4: str,
    dns_ip: str,
    subnet_v6: str | None = None,
    endpoint_port: int = 51820,
    endpoint_port_alt: int = 443,
    mtu: int = 1420,
    protocol: NodeProtocol = NodeProtocol.WIREGUARD,
    reality_dest: str | None = None,
    reality_server_names: list[str] | None = None,
    reality_fingerprint: str = reality_profile.DEFAULT_FINGERPRINT,
    validate_dest: bool = False,
) -> tuple[Node, str]:
    """Create a node and issue a one-time registration token (for cloud-init).

    For protocol=amneziawg an obfuscation profile (Jc,Jmin,Jmax,S1,S2,H1-H4) is generated —
    it goes both into the node config (via registration) and into the client configs. The port for
    AmneziaWG should be set to a non-standard one (51820+Sx=8 — an instant-block signature of TSPU).

    For protocol=vless_reality a server-side Reality profile (x25519+shortId) is generated from
    the given reality_dest ('host:port') and reality_server_names (SNI). The port is usually 443.
    """
    node = Node(
        name=name,
        endpoint_host=endpoint_host,
        endpoint_port=endpoint_port,
        endpoint_port_alt=endpoint_port_alt,
        location_code=location_code,
        location_name=location_name,
        subnet_v4=subnet_v4,
        subnet_v6=subnet_v6,
        dns_ip=dns_ip,
        mtu=mtu,
        protocol=protocol,
    )
    if protocol == NodeProtocol.AMNEZIAWG:
        for key, value in awg_profile.generate_profile().items():
            setattr(node, f"awg_{key}", value)
    elif protocol == NodeProtocol.VLESS_REALITY:
        if not reality_dest or not reality_server_names:
            raise ValueError("для vless_reality нужны reality_dest и reality_server_names")
        if validate_dest:
            # network check: TLS1.3 + h2 + non-CDN. DestError (ValueError) if unsuitable —
            # we do not allow creating a node with an unusable dest (our own pain from the live test).
            check_dest(reality_dest)
        node.reality_config = reality_profile.dump_profile(
            reality_profile.generate_server_profile(
                dest=reality_dest,
                server_names=reality_server_names,
                fingerprint=reality_fingerprint,
            )
        )
    token = mint_registration_token(node)
    session.add(node)
    session.flush()
    return node, token


def list_nodes(session: Session) -> list[Node]:
    return list(session.execute(select(Node).order_by(Node.name)).scalars().all())


# --- Billing overview ---
def invoice_is_paid(invoice: Invoice) -> bool:
    """Did the money for this invoice arrive?

    Read from STATUS, never from paid_at. paid_at is NOT a payment flag: scrub_invoice_linkage nulls
    it on purpose, because the exact instant of payment correlates with Subscription.starts_at and
    rebuilds the payment↔account link the scrub exists to destroy. A paid-then-scrubbed invoice
    therefore has paid_at IS NULL while being fully paid — the console used to count exactly those
    as unpaid and told the operator "Paid 0 / 4" with two PAID rows on screen.
    """
    return billing_fsm.is_activating(invoice.status)


def invoice_stats(session: Session) -> dict[str, int]:
    """Totals over ALL invoices (not just the page shown) — counted in SQL, so the headline figure
    stays true past the table's row cap."""
    total = session.execute(select(func.count()).select_from(Invoice)).scalar_one()
    paid = session.execute(
        select(func.count())
        .select_from(Invoice)
        .where(Invoice.status.in_(billing_fsm.ACTIVATING_STATUSES))
    ).scalar_one()
    scrubbed = session.execute(
        select(func.count()).select_from(Invoice).where(Invoice.scrubbed_at.is_not(None))
    ).scalar_one()
    return {"total": int(total), "paid": int(paid), "scrubbed": int(scrubbed)}


def list_invoices(session: Session, *, limit: int = 100) -> list[Invoice]:
    # id as the tie-breaker: the scrub coarsens created_at to the day, so every scrubbed invoice of
    # one day shares a timestamp. Without a second sort key their order is undefined and the LIMIT
    # could drop a different row on each render.
    return list(
        session.execute(
            select(Invoice).order_by(Invoice.created_at.desc(), Invoice.id).limit(limit)
        ).scalars().all()
    )


def list_subscriptions(session: Session, *, limit: int = 100) -> list[Subscription]:
    return list(
        session.execute(
            select(Subscription).order_by(Subscription.created_at.desc()).limit(limit)
        ).scalars().all()
    )


# --- Abuse ---
def block_peer(session: Session, peer: Peer) -> str:
    """Remove a specific peer (abuse). Returns the pubkey for deletion on the node."""
    return remove_device(session, peer)
