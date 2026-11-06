"""Invoice staleness (B1): a pending invoice past expires_at → EXPIRED/LATE_PAYMENT."""

from datetime import UTC, datetime, timedelta

from app.models import Invoice
from app.models.enums import InvoiceStatus, PaymentMethod
from app.scheduler.invoice_expiry import expire_stale_invoices

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _invoice(session, *, status, expires_at):
    inv = Invoice(
        method=PaymentMethod.BTC_ONCHAIN, status=status, days_granted=30,
        amount_fiat_cents=500, fiat_currency="USD", expires_at=expires_at,
    )
    session.add(inv)
    session.commit()
    return inv


def test_expires_stale_created_invoice(session):
    inv = _invoice(session, status=InvoiceStatus.CREATED, expires_at=NOW - timedelta(minutes=1))
    n = expire_stale_invoices(session, now=NOW)
    session.commit()
    assert n == 1
    assert inv.status == InvoiceStatus.EXPIRED


def test_fresh_invoice_not_expired(session):
    inv = _invoice(session, status=InvoiceStatus.CREATED, expires_at=NOW + timedelta(minutes=30))
    assert expire_stale_invoices(session, now=NOW) == 0
    assert inv.status == InvoiceStatus.CREATED


def test_awaiting_past_expiry_goes_late_payment(session):
    inv = _invoice(
        session, status=InvoiceStatus.AWAITING_CONFIRMATION, expires_at=NOW - timedelta(minutes=1)
    )
    expire_stale_invoices(session, now=NOW)
    session.commit()
    assert inv.status == InvoiceStatus.LATE_PAYMENT  # the money arrived, but the window has closed


def test_paid_invoice_untouched(session):
    inv = _invoice(session, status=InvoiceStatus.PAID, expires_at=NOW - timedelta(minutes=1))
    assert expire_stale_invoices(session, now=NOW) == 0
    assert inv.status == InvoiceStatus.PAID


def test_late_payment_emits_reconcile_warning(session, caplog):
    """#6: AWAITING→LATE_PAYMENT (the money arrived) does not stay silent — an alert for monitoring."""
    _invoice(
        session, status=InvoiceStatus.AWAITING_CONFIRMATION, expires_at=NOW - timedelta(minutes=1)
    )
    with caplog.at_level("WARNING", logger="invoice_expiry"):
        expire_stale_invoices(session, now=NOW)
    assert any("late_payment" in r.getMessage() for r in caplog.records)


def test_expired_with_deposit_address_is_rescuable_and_warns(session, caplog):
    """CREATED + a live address → LATE_PAYMENT (reconcilable), not terminal EXPIRED.

    This test used to assert the opposite — EXPIRED plus a "check whether money arrived" warning for
    a HUMAN. That was the bug: EXPIRED is terminal and outside reconcile's poll scope, so coins that
    landed after the window were credited to nobody. The warning stays (monitoring still wants to
    know), but the invoice is now parked where the poll can auto-settle it.
    See test_expired_fundable_rescue.py for the end-to-end money proof.
    """
    inv = _invoice(session, status=InvoiceStatus.CREATED, expires_at=NOW - timedelta(minutes=1))
    inv.payment_address = "addr-xyz"
    session.commit()
    with caplog.at_level("WARNING", logger="invoice_expiry"):
        expire_stale_invoices(session, now=NOW)
    assert inv.status == InvoiceStatus.LATE_PAYMENT
    assert any("late_payment" in r.getMessage() for r in caplog.records)


def test_expired_without_address_no_warning(session, caplog):
    """An invoice without an address (no payment could have happened) — we do not make noise."""
    _invoice(session, status=InvoiceStatus.CREATED, expires_at=NOW - timedelta(minutes=1))
    with caplog.at_level("WARNING", logger="invoice_expiry"):
        expire_stale_invoices(session, now=NOW)
    assert not [r for r in caplog.records if r.name == "invoice_expiry"]
