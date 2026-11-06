from datetime import UTC, datetime, timedelta

from app.models import Account, Invoice
from app.models.enums import InvoiceStatus, PaymentMethod
from app.scheduler.invoice_scrub import scrub_due_invoices

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _paid_invoice(session, suffix, paid_at) -> Invoice:
    a = Account(account_number_hash=f"is{suffix}".ljust(64, "0"))
    session.add(a)
    session.flush()
    inv = Invoice(
        account_id=a.id,
        method=PaymentMethod.MONERO,
        status=InvoiceStatus.PAID,
        days_granted=30,
        amount_fiat_cents=500,
        payment_address="addr",
        external_id=f"ext{suffix}",
        paid_at=paid_at,
    )
    session.add(inv)
    session.commit()
    return inv


def test_scrub_after_retention(session):
    inv = _paid_invoice(session, "1", NOW - timedelta(minutes=120))
    n = scrub_due_invoices(session, now=NOW, retention_minutes=60)
    session.commit()
    assert n == 1
    assert inv.account_id is None
    assert inv.payment_address is None
    assert inv.scrubbed_at is not None


def test_not_scrubbed_before_retention(session):
    inv = _paid_invoice(session, "2", NOW - timedelta(minutes=10))
    n = scrub_due_invoices(session, now=NOW, retention_minutes=60)
    assert n == 0
    assert inv.account_id is not None


def _unpaid_invoice(session, suffix, status, expires_at) -> Invoice:
    a = Account(account_number_hash=f"iu{suffix}".ljust(64, "0"))
    session.add(a)
    session.flush()
    inv = Invoice(
        account_id=a.id,
        method=PaymentMethod.MONERO,
        status=status,
        days_granted=30,
        amount_fiat_cents=500,
        payment_address="addr",
        external_id=f"uext{suffix}",
        expires_at=expires_at,
    )
    session.add(inv)
    session.commit()
    return inv


def test_scrub_terminal_unpaid_after_window(session):
    """#4: EXPIRED with account_id+address is scrubbed after unpaid_retention."""
    inv = _unpaid_invoice(session, "u1", InvoiceStatus.EXPIRED, NOW - timedelta(minutes=100))
    n = scrub_due_invoices(session, now=NOW, unpaid_retention_minutes=60)
    session.commit()
    assert n == 1
    assert inv.account_id is None
    assert inv.payment_address is None
    assert inv.external_id is None


def test_terminal_unpaid_kept_within_window(session):
    """LATE_PAYMENT within the reconcile window — the link is KEPT (a refund/credit is possible)."""
    inv = _unpaid_invoice(session, "u2", InvoiceStatus.LATE_PAYMENT, NOW - timedelta(minutes=10))
    n = scrub_due_invoices(session, now=NOW, unpaid_retention_minutes=60)
    assert n == 0
    assert inv.account_id is not None


# --- the row must not keep its own clocks -------------------------------------------------------
#
# Zeroing paid_at was believed to remove the exact instant of payment. It did not: the invoice row
# carries created_at and updated_at (TimestampMixin), and updated_at is bumped BY THE SCRUB'S OWN
# UPDATE — so the scrub stamped the row with the very microsecond it was erasing. That made the
# invoice↔account join unique at any database size (invoice.updated_at ≈ subscription.created_at).
# These tests pin the coarsening. They are about the ATTACK, not about the fields: hence the sweep
# over every datetime column rather than a list of names, so a column added later is covered too.

def test_no_timestamp_on_a_scrubbed_invoice_reveals_the_settle_instant(session):
    from app.models.base import as_utc
    from app.services.billing import scrub_invoice_linkage

    settle = datetime(2026, 6, 1, 14, 5, 14, 123456, tzinfo=UTC)
    inv = _paid_invoice(session, "ts1", settle)
    inv.expires_at = settle - timedelta(minutes=60)  # created + TTL, as purchase sets it
    session.commit()

    scrub_invoice_linkage(session, inv, now=settle)
    session.commit()
    session.refresh(inv)

    leaks = []
    for col in inv.__table__.columns:
        if "DATETIME" not in str(col.type).upper():
            continue
        value = as_utc(getattr(inv, col.name))
        if value is None:
            continue
        # anything that still carries a time-of-day can be compared against the account ledger
        if (value.hour, value.minute, value.second, value.microsecond) != (0, 0, 0, 0):
            leaks.append(f"{col.name}={value.isoformat()}")

    assert not leaks, f"scrubbed invoice still carries sub-day timestamps: {leaks}"


def test_scrub_coarsens_the_rows_own_clocks_and_drops_the_deadline(session):
    from app.models.base import as_utc
    from app.services.billing import scrub_invoice_linkage

    settle = datetime(2026, 6, 1, 14, 5, 14, 123456, tzinfo=UTC)
    inv = _paid_invoice(session, "ts2", settle)
    inv.expires_at = settle - timedelta(minutes=60)
    session.commit()
    created_before = as_utc(inv.created_at)

    scrub_invoice_linkage(session, inv, now=settle)
    session.commit()
    session.refresh(inv)

    day = settle.replace(hour=0, minute=0, second=0, microsecond=0)
    assert as_utc(inv.created_at) == day
    assert as_utc(inv.updated_at) == day, "the scrub's own UPDATE must not re-stamp the row"
    assert as_utc(inv.scrubbed_at) == day
    assert inv.expires_at is None, "the payment deadline mirrors created_at + TTL"
    assert created_before != day, "sanity: created_at really was precise before the scrub"


def test_coarsening_does_not_take_a_late_payment_out_of_reconcile(session):
    """The rescue path keys on updated_at. A live LATE_PAYMENT is never scrubbed, so it must keep
    its precise clock — this is the money-losing regression the coarsening could have caused."""
    from app.models.base import as_utc

    inv = _unpaid_invoice(session, "r1", InvoiceStatus.LATE_PAYMENT, NOW - timedelta(minutes=10))
    scrub_due_invoices(session, now=NOW, unpaid_retention_minutes=60)
    session.commit()
    session.refresh(inv)

    assert inv.scrubbed_at is None, "still inside the window — must not be scrubbed"
    updated = as_utc(inv.updated_at)
    assert (updated.hour, updated.minute) != (0, 0) or updated.microsecond != 0, (
        "a rescuable invoice must keep the precise updated_at reconcile anchors on"
    )


def test_the_invoice_listing_orders_by_more_than_the_coarsened_date(session):
    """created_at collapses to midnight → every scrubbed invoice of one day shares a sort key, and
    an ORDER BY with no tie-breaker leaves their relative order up to the planner: the console's
    LIMIT could then drop a different row on each render.

    Asserted on the QUERY, not on returned rows, and deliberately so: SQLite answers this table in
    insertion order whatever the ORDER BY, so a behavioural test passes just as happily without the
    tie-breaker (verified by mutation) — it would be a test that is green on the broken code.
    Postgres, which is what prod runs, gives no such guarantee.
    """
    from sqlalchemy import event

    from app.services import admin_service

    emitted = []

    def _spy(conn, cur, statement, *a):
        emitted.append(statement)

    event.listen(session.get_bind(), "before_cursor_execute", _spy)
    try:
        admin_service.list_invoices(session, limit=3)
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", _spy)

    selects = [s for s in emitted if "ORDER BY" in s.upper()]
    assert selects, "list_invoices must actually emit an ordered query"
    order_by = selects[-1].upper().split("ORDER BY", 1)[1]

    assert "CREATED_AT" in order_by, "sanity: we are looking at the right clause"
    assert "ID" in order_by.replace("CREATED_AT", ""), (
        "list_invoices must break created_at ties on something unique — after the scrub every "
        f"invoice of one day shares created_at. Emitted: ORDER BY{order_by}"
    )
