"""Time-credit billing: a purchase = N days, stacks up. Account lifecycle statuses.

The full invoice state machine — Phase 3. Here — time crediting, status evaluation,
scrubbing of the invoice↔account linkage (no-logs).
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, Invoice, Subscription
from app.models.base import as_utc
from app.models.enums import AccountStatus, InvoiceStatus


def _utcnow() -> datetime:
    return datetime.now(UTC)


def apply_credit(
    session: Session,
    account: Account,
    *,
    days: int,
    now: datetime | None = None,
    price_plan_code: str | None = None,
    invoice_id=None,
    device_limit: int | None = None,
) -> Subscription:
    """Credit N days. Stacks up: from max(now, current expires_at).

    Takes a row lock on the account and holds it until the caller commits — two credits for the
    same account are serialized, whichever paths they arrive by (see the comment on the lock).

    IDEMPOTENT PER INVOICE: if this invoice already produced a credit, return that row and change
    nothing. This one invariant — one invoice, at most one credit — is what keeps a manual operator
    grant and the automatic late-payment rescue from paying out the SAME coins twice: the grant
    records the invoice it settles, so reconcile later settling that very invoice is a no-op.

    Note what this deliberately does NOT do: mark the invoice PAID up front. An early PAID would
    strand a genuine late payment (PAID is outside reconcile's scan and trips the paid_at guard) and
    would hand the row to the immediate-scrub branch (invoice_link_retention_minutes=0), destroying
    the payment_address/crypto_amount_paid evidence within a cycle. The invoice stays exactly as it
    is; only the credit is deduplicated.
    """
    now = now or _utcnow()
    # Lock the ACCOUNT row before reading expires_at. Everything else here is deduplicated per
    # INVOICE (the guard below + the unique index from migration 0009), and that is no defence at
    # all against two credits for DIFFERENT invoices: both read the same expires_at, both write
    # base+days, and the last writer wins — the customer paid for two terms and got one, while the
    # Subscription ledger records both. Silent, and reachable three ways: two webhooks in the
    # threadpool (_dispatch locks the invoice row, not the account), a webhook racing the reconcile
    # job, and — likeliest — an operator pressing "extend" exactly while a payment lands, which is
    # precisely when they press it.
    #
    # Reading expires_at back as a SCALAR is load-bearing, for the same reason populate_existing is
    # in promo.reserve: `account.expires_at` comes out of the identity map with whatever was loaded
    # BEFORE the lock, so using it would take the lock and then throw its result away. A column
    # select bypasses the identity map. It is preferred over populate_existing here because that
    # would overwrite the whole instance — and on SQLite a re-read DateTime comes back NAIVE, which
    # silently turns account.expires_at from aware to naive for every later comparison.
    # (On SQLite FOR UPDATE is a no-op and the tests are single-threaded; the lock works on Postgres.
    # The staleness half is what the test covers, because that is where the bug would come back.)
    locked_expires_at = session.execute(
        select(Account.expires_at).where(Account.id == account.id).with_for_update()
    ).scalar_one()
    if invoice_id is not None:
        already = session.execute(
            select(Subscription).where(Subscription.invoice_id == invoice_id)
        ).scalars().first()
        if already is not None:
            return already
    exp = as_utc(locked_expires_at)
    base = exp if (exp and exp > now) else now
    starts_at = base
    ends_at = base + timedelta(days=days)
    account.expires_at = ends_at
    account.status = AccountStatus.ACTIVE
    account.peers_revoked_at = None
    # new term → reminders must be sent anew
    account.reminder_3d_sent_at = None
    account.reminder_1d_sent_at = None
    if device_limit is not None:
        account.device_limit = device_limit
    sub = Subscription(
        account_id=account.id,
        days=days,
        starts_at=starts_at,
        ends_at=ends_at,
        price_plan_code=price_plan_code,
        invoice_id=invoice_id,
    )
    session.add(sub)
    return sub


def evaluate_status(
    account: Account, *, now: datetime | None = None, grace_days: int = 3
) -> AccountStatus:
    """Compute the lifecycle status from expires_at and the grace period."""
    now = now or _utcnow()
    exp = as_utc(account.expires_at)
    if exp and now < exp:
        return AccountStatus.ACTIVE
    if exp:
        if now < exp + timedelta(days=grace_days):
            return AccountStatus.GRACE
    return AccountStatus.EXPIRED


def mark_invoice_paid(
    session: Session,
    invoice: Invoice,
    *,
    now: datetime | None = None,
    confirmations: int | None = None,
) -> None:
    invoice.status = InvoiceStatus.PAID
    invoice.paid_at = now or _utcnow()
    if confirmations is not None:
        invoice.confirmations_seen = confirmations


def scrub_invoice_linkage(
    session: Session, invoice: Invoice, *, now: datetime | None = None
) -> None:
    """Wipe the invoice↔account linkage, the payment identifiers AND the on-chain fingerprint (no-logs).

    Zeroing out account_id is not enough: amount/rate/currency + the exact paid_at make it
    possible to re-link the invoice to a single on-chain tx (public blockchain) and, via the
    paid_at≈Subscription.starts_at timing, get back to account_id. That is why we also clear
    the financial fields and the exact timestamps.
    Idempotency of a repeated webhook after the scrub is held by the guard account_id is None +
    the absence of external_id (a replay will not find the invoice), and NOT by paid_at
    (see on_payment_settled).
    """
    scrub_ts = now or _utcnow()
    invoice.account_id = None
    # Same lifetime as account_id: which promo code a purchase used is payment-window bookkeeping
    # (it exists so an unpaid invoice can return the use — promo.release), not something to keep.
    # Cleared, NOT released: reaching here means this invoice was activated, so the customer got
    # the discount and the use is genuinely spent. The release path is scheduler/invoice_expiry.
    invoice.promo_code_id = None
    invoice.payment_address = None
    invoice.external_id = None
    invoice.checkout_url = None
    # on-chain fingerprint: amount/amount paid/currency/rate/rate window
    invoice.crypto_currency = None
    invoice.crypto_amount = None
    invoice.crypto_amount_paid = None
    # the txids are the STRONGEST link of all — they name the on-chain transactions outright
    invoice.crypto_txs = None
    invoice.exchange_rate = None
    invoice.rate_locked_at = None
    invoice.rate_expires_at = None
    # the exact paid_at = a timing link to Subscription.starts_at → we break it; scrubbed_at
    # is coarsened to a day (it is only needed as "already scrubbed", not as an exact time)
    invoice.paid_at = None
    day = scrub_ts.replace(hour=0, minute=0, second=0, microsecond=0)
    invoice.scrubbed_at = day
    # The payment deadline (created + TTL) is a precise mirror of created_at and has no meaning once
    # the invoice is settled or terminally unpaid. Left in place it also narrows an on-chain search
    # for this payment to a ~1h window. Both scrub branches in scheduler/invoice_scrub filter on
    # `scrubbed_at IS NULL`, so nulling it cannot strand a row mid-decision.
    invoice.expires_at = None
    # Zeroing paid_at alone was not enough, and for a while we believed it was: the row keeps its own
    # clocks. created_at is exact, and updated_at is bumped BY THIS VERY UPDATE (TimestampMixin's
    # onupdate=utcnow) — i.e. the scrub was itself stamping the row with the exact instant it was
    # trying to forget, to the microsecond. Coarsening both to the day is what actually degrades the
    # invoice↔account join from "unique at any volume" to "same day".
    #
    # Deliberately NOT touching the ACCOUNT side (account.updated_at, subscription.created_at /
    # starts_at): account.expires_at minus subscription.days gives the exact instant back, and that
    # subtraction cannot be closed without changing how much time the customer actually bought.
    # Coarsening the account side would therefore buy nothing while risking the ledger. One side of
    # a join at day resolution is enough to make the whole join day-resolution — see
    # docs/no-logs-inventory.md §1a for what this does and does not achieve.
    invoice.created_at = day
    invoice.updated_at = day
    # break the subscription → invoice bridge
    subs = session.execute(
        select(Subscription).where(Subscription.invoice_id == invoice.id)
    ).scalars().all()
    for sub in subs:
        sub.invoice_id = None
