"""A late payment (coins after the 60-min window) must be auto-credited by reconcile.

Operator decision: don't strand a customer whose payment confirmed late. The invoice expired to
LATE_PAYMENT; the FSM already allows LATE_PAYMENT --SETTLED--> PAID, and reconcile already polls
BlockBee. Adding LATE_PAYMENT to reconcile's scan is the whole rescue: poll → full amount received
→ credit the days → account active. The exchange rate is not a concern — the amount was fixed in
the coin at creation, and we credit exactly what arrived.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models import Account, Invoice, PricePlan
from app.models.enums import AccountStatus, InvoiceStatus, PaymentMethod
from app.payments import registry
from app.scheduler import invoice_reconcile
from app.services.account_number import generate_account_number, hash_account_number
from app.services.billing_fsm import PaymentEvent


class _PollProvider:
    """A blockbee-shaped fake: its reconcile() reports the invoice as fully settled."""

    name = "blockbee"

    def __init__(self, event=PaymentEvent.SETTLED, amount=Decimal("0.111643")):
        self._event, self._amount = event, amount

    def reconcile(self, *, order_id, coin, expected):
        from app.payments.base import ProviderStatus

        return ProviderStatus(external_id="addr", event=self._event, amount_paid=self._amount, confirmations=6)


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.clear()
    yield
    registry.clear()


def _late_invoice(session, settings):
    acc = Account(
        account_number_hash=hash_account_number(generate_account_number(), settings.account_number_pepper),
        status=AccountStatus.EXPIRED,
        expires_at=None,
        device_limit=5,
    )
    plan = PricePlan(
        code="1m", name="1m", duration_days=30, price_fiat_cents=500,
        fiat_currency="USD", device_limit=5, is_active=True, sort_order=1,
    )
    session.add_all([acc, plan])
    session.flush()
    inv = Invoice(
        account_id=acc.id,
        price_plan_id=plan.id,
        method=PaymentMethod.LITECOIN,
        status=InvoiceStatus.LATE_PAYMENT,  # expired past its window, but money may have arrived
        days_granted=30,
        amount_fiat_cents=500,
        crypto_amount=Decimal("0.111643"),
        crypto_currency="LTC",
        payment_address="MKNagQTh1uVRMCkASJ2xLkbVVTaVHapprH",
        external_id="MKNagQTh1uVRMCkASJ2xLkbVVTaVHapprH",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    session.add(inv)
    session.commit()
    return acc, inv


def test_late_payment_is_rescued_and_the_account_activates(session):
    from app.config import get_settings

    acc, inv = _late_invoice(session, get_settings())
    registry.register(_PollProvider(event=PaymentEvent.SETTLED))

    moved = invoice_reconcile.reconcile_pending_invoices(session)

    assert moved == 1
    session.refresh(inv)
    session.refresh(acc)
    assert inv.status == InvoiceStatus.PAID, "a fully-received late payment must settle"
    assert acc.status == AccountStatus.ACTIVE, "the customer must get his subscription"
    from app.models.base import as_utc

    assert acc.expires_at is not None and as_utc(acc.expires_at) > datetime.now(UTC)


def test_late_payment_without_funds_is_left_alone(session):
    """If BlockBee still shows nothing (truly unpaid), reconcile must not fabricate a subscription."""
    from app.config import get_settings

    acc, inv = _late_invoice(session, get_settings())
    registry.register(_PollProvider(event=PaymentEvent.SEEN))  # nothing settled

    moved = invoice_reconcile.reconcile_pending_invoices(session)

    assert moved == 0
    session.refresh(inv)
    session.refresh(acc)
    assert inv.status == InvoiceStatus.LATE_PAYMENT
    assert acc.status == AccountStatus.EXPIRED


def test_late_payment_is_in_reconcile_scope():
    assert InvoiceStatus.LATE_PAYMENT in invoice_reconcile._PENDING
