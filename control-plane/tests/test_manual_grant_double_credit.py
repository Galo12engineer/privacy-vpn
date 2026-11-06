"""A manual operator grant and the automatic late-payment rescue must never pay out the same coins.

The trap appeared when two safe-looking changes met:
  1. the admin console got "find account by number → extend by hand" (for late/split payments), a
     grant that carried no invoice link and no idempotency;
  2. expiry started parking every invoice with a live deposit address in LATE_PAYMENT, so reconcile
     keeps polling it and can settle it on its own.
Together: customer writes "I paid late" → operator extends by hand → minutes later reconcile sees
the coins on the same invoice and credits the days AGAIN.

The first attempt at a guard was worse than the bug: it marked EVERY late invoice on the account
PAID, so a genuine late payment bundled with an abandoned cart was silently stranded (PAID is out of
reconcile's scan and trips the paid_at guard) and its evidence was scrubbed on the next cycle
(invoice_link_retention_minutes=0 on prod). Hence the current design: the grant only RECORDS which
invoice it settles, apply_credit is idempotent per invoice_id, and the invoice itself is untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import get_settings
from app.models import Account, Invoice, PricePlan, Subscription
from app.models.base import as_utc
from app.models.enums import AccountStatus, InvoiceStatus, PaymentMethod
from app.payments import registry
from app.scheduler import invoice_reconcile
from app.services import admin_service
from app.services.account_number import generate_account_number, hash_account_number
from app.services.billing import apply_credit
from app.services.billing_fsm import PaymentEvent

ADDR = "MKNagQTh1uVRMCkASJ2xLkbVVTaVHapprH"


class _PollProvider:
    """Reports whatever invoice it is asked about as fully settled."""

    name = "blockbee"

    def reconcile(self, *, order_id, coin, expected):
        from app.payments.base import ProviderStatus

        return ProviderStatus(
            external_id=ADDR, event=PaymentEvent.SETTLED,
            amount_paid=expected, confirmations=6,
        )


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.clear()
    yield
    registry.clear()


def _account(session):
    acc = Account(
        account_number_hash=hash_account_number(
            generate_account_number(), get_settings().account_number_pepper
        ),
        status=AccountStatus.EXPIRED, expires_at=None, device_limit=5,
    )
    session.add(acc)
    session.flush()
    return acc


def _plan(session, *, code="1m", days=30, cents=500):
    p = PricePlan(
        code=code, name=code, duration_days=days, price_fiat_cents=cents,
        fiat_currency="USD", device_limit=5, is_active=True, sort_order=1,
    )
    session.add(p)
    session.flush()
    return p


def _late_invoice(session, acc, plan, *, address, seen=False, amount="0.111643"):
    inv = Invoice(
        account_id=acc.id, price_plan_id=plan.id, method=PaymentMethod.LITECOIN,
        status=InvoiceStatus.LATE_PAYMENT, days_granted=plan.duration_days,
        amount_fiat_cents=plan.price_fiat_cents,
        crypto_amount=Decimal(amount), crypto_currency="LTC",
        crypto_amount_paid=Decimal(amount) if seen else None,
        payment_address=address, external_id=address,
        expires_at=datetime.now(UTC) - timedelta(minutes=90),
    )
    session.add(inv)
    session.commit()
    return inv


# --- the credit invariant ---------------------------------------------------------------------

def test_apply_credit_is_idempotent_per_invoice(session):
    acc = _account(session)
    plan = _plan(session)
    inv = _late_invoice(session, acc, plan, address=ADDR)

    first = apply_credit(session, acc, days=30, invoice_id=inv.id)
    session.commit()
    after = as_utc(acc.expires_at)

    second = apply_credit(session, acc, days=30, invoice_id=inv.id)
    session.commit()

    assert second is first, "the same invoice must not mint a second ledger row"
    assert as_utc(acc.expires_at) == after, "the term must not move twice for one invoice"


def test_manual_grant_then_reconcile_credits_only_once(session):
    """The money regression: one payment, one credit — even though both paths run."""
    acc = _account(session)
    plan = _plan(session)
    inv = _late_invoice(session, acc, plan, address=ADDR, seen=True)

    admin_service.grant_days(session, acc, days=30, plan=plan, settle_invoice=inv)
    session.commit()
    session.refresh(acc)
    after_grant = as_utc(acc.expires_at)

    registry.register(_PollProvider())
    invoice_reconcile.reconcile_pending_invoices(session)  # coins confirm afterwards
    session.commit()
    session.refresh(acc)

    assert as_utc(acc.expires_at) == after_grant, "the same payment must not extend twice"
    subs = session.query(Subscription).filter(Subscription.account_id == acc.id).all()
    assert len(subs) == 1, f"one payment → one ledger row, got {len(subs)}"


def test_the_grant_does_not_touch_the_invoice(session):
    """Marking it PAID up front is what stranded genuine payments and destroyed the evidence."""
    acc = _account(session)
    plan = _plan(session)
    inv = _late_invoice(session, acc, plan, address=ADDR, seen=True)

    admin_service.grant_days(session, acc, days=30, plan=plan, settle_invoice=inv)
    session.commit()
    session.refresh(inv)

    assert inv.status == InvoiceStatus.LATE_PAYMENT, "must stay pollable"
    assert inv.paid_at is None
    assert inv.payment_address == ADDR, "forensic fields must survive for the operator"
    assert inv.crypto_amount_paid is not None


# --- the N1 regression: never settle what the operator did not pick ------------------------------

def test_an_unrelated_invoice_stays_fully_rescuable(session):
    """An abandoned cart and a REAL late payment can sit on one account.

    Granting for one must leave the other completely intact — it used to be marked PAID too, which
    stranded the customer's coins for good.
    """
    acc = _account(session)
    cheap = _plan(session, code="1m", days=30, cents=500)
    big = _plan(session, code="12m", days=365, cents=4800)
    abandoned = _late_invoice(session, acc, cheap, address="addr-abandoned")
    genuine = _late_invoice(session, acc, big, address=ADDR, seen=True, amount="0.9")

    # operator extends for the abandoned one only
    admin_service.grant_days(session, acc, days=30, plan=cheap, settle_invoice=abandoned)
    session.commit()
    session.refresh(genuine)

    assert genuine.status == InvoiceStatus.LATE_PAYMENT
    assert genuine.paid_at is None
    assert genuine.crypto_amount_paid is not None, "evidence must not be scrubbed away"
    assert genuine.id in [i.id for i in admin_service.rescuable_invoices(session, acc)]

    # and the real payment can still be credited on its own
    registry.register(_PollProvider())
    invoice_reconcile.reconcile_pending_invoices(session)
    session.commit()
    session.refresh(genuine)
    assert genuine.status == InvoiceStatus.PAID, "the genuine late payment must still settle"
    subs = session.query(Subscription).filter(Subscription.account_id == acc.id).all()
    assert len(subs) == 2, "two distinct invoices → two credits (not one, not three)"


def test_goodwill_grant_leaves_every_invoice_alone(session):
    acc = _account(session)
    plan = _plan(session)
    inv = _late_invoice(session, acc, plan, address=ADDR, seen=True)

    admin_service.grant_days(session, acc, days=7, plan=plan, settle_invoice=None)
    session.commit()
    session.refresh(inv)

    assert inv.status == InvoiceStatus.LATE_PAYMENT
    assert inv.paid_at is None
