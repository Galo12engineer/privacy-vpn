"""An invoice whose deposit address was LIVE when the window closed must stay rescuable.

The money bug this guards (verified on the prod branch): expiry emitted PaymentEvent.EXPIRED for
every stale invoice, and from CREATED that lands in EXPIRED — which is terminal AND outside
invoice_reconcile's poll scope. So the routine BTC case (an exchange withdrawal not seen in the
mempool within the 60-min TTL) went: invoice EXPIRED -> coins land -> BlockBee forwards them to the
operator payout and calls the webhook -> IllegalTransition, swallowed, route still answers `*ok*` so
BlockBee stops retrying -> reconcile never polls an EXPIRED invoice. The customer paid real coins
and got nothing, with no automated path to notice or fix it.

Adding LATE_PAYMENT to reconcile's scope (done earlier) did NOT cover this: that only helps invoices
already SEEN in the mempool (AWAITING/UNDERPAID -> LATE_PAYMENT). The CREATED -> EXPIRED path — the
common one — still fell through. Expiry now routes by FUNDABILITY: a live deposit address means the
money can still arrive, so the invoice goes to LATE_PAYMENT, where reconcile does poll it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import get_settings
from app.models import Account, Invoice, PricePlan
from app.models.enums import AccountStatus, InvoiceStatus, PaymentMethod
from app.payments import registry
from app.scheduler import invoice_reconcile
from app.scheduler.invoice_expiry import expire_stale_invoices
from app.services.account_number import generate_account_number, hash_account_number
from app.services.billing_fsm import PaymentEvent

NOW = datetime(2026, 6, 1, tzinfo=UTC)
ADDR = "MKNagQTh1uVRMCkASJ2xLkbVVTaVHapprH"


class _PollProvider:
    """blockbee-shaped fake whose reconcile() reports the full amount as settled."""

    name = "blockbee"

    def __init__(self, event=PaymentEvent.SETTLED, amount=Decimal("0.111643")):
        self._event, self._amount = event, amount

    def reconcile(self, *, order_id, coin, expected):
        from app.payments.base import ProviderStatus

        return ProviderStatus(
            external_id=ADDR, event=self._event, amount_paid=self._amount, confirmations=6
        )


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.clear()
    yield
    registry.clear()


def _invoice(session, *, status, expires_at, address=None, account=None, plan=None):
    inv = Invoice(
        account_id=account.id if account else None,
        price_plan_id=plan.id if plan else None,
        method=PaymentMethod.LITECOIN,
        status=status,
        days_granted=30,
        amount_fiat_cents=500,
        fiat_currency="USD",
        expires_at=expires_at,
        payment_address=address,
        external_id=address,
        crypto_amount=Decimal("0.111643") if address else None,
        crypto_currency="LTC" if address else None,
    )
    session.add(inv)
    session.commit()
    return inv


def _account_and_plan(session):
    acc = Account(
        account_number_hash=hash_account_number(
            generate_account_number(), get_settings().account_number_pepper
        ),
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
    return acc, plan


# --- routing -------------------------------------------------------------------------------

def test_created_with_live_address_expires_to_late_payment(session):
    """The core fix: a fundable invoice must NOT go terminal."""
    inv = _invoice(
        session, status=InvoiceStatus.CREATED,
        expires_at=NOW - timedelta(minutes=1), address=ADDR,
    )
    assert expire_stale_invoices(session, now=NOW) == 1
    session.commit()
    assert inv.status == InvoiceStatus.LATE_PAYMENT, (
        "a live deposit address means coins can still land — EXPIRED would strand them"
    )


def test_created_without_address_still_expires_terminal(session):
    """No address was ever issued → money cannot arrive → terminal EXPIRED stays correct."""
    inv = _invoice(session, status=InvoiceStatus.CREATED, expires_at=NOW - timedelta(minutes=1))
    assert expire_stale_invoices(session, now=NOW) == 1
    session.commit()
    assert inv.status == InvoiceStatus.EXPIRED


def test_fresh_fundable_invoice_is_untouched(session):
    inv = _invoice(
        session, status=InvoiceStatus.CREATED,
        expires_at=NOW + timedelta(minutes=30), address=ADDR,
    )
    assert expire_stale_invoices(session, now=NOW) == 0
    assert inv.status == InvoiceStatus.CREATED


# --- the money proof: expire → reconcile → paid ---------------------------------------------

def test_late_coins_on_an_expired_invoice_are_auto_credited(session):
    """End to end: the exact scenario that used to eat a real payment."""
    acc, plan = _account_and_plan(session)
    inv = _invoice(
        session, status=InvoiceStatus.CREATED, expires_at=NOW - timedelta(minutes=1),
        address=ADDR, account=acc, plan=plan,
    )

    expire_stale_invoices(session, now=NOW)          # window closes, address was live
    session.commit()
    assert inv.status == InvoiceStatus.LATE_PAYMENT

    registry.register(_PollProvider(event=PaymentEvent.SETTLED))
    # same clock as the expiry above — otherwise the age bound (correctly) treats this fixture as a
    # six-week-old invoice and skips it
    moved = invoice_reconcile.reconcile_pending_invoices(session, now=NOW)  # coins landed after

    assert moved == 1
    session.refresh(inv)
    session.refresh(acc)
    assert inv.status == InvoiceStatus.PAID, "a fully-received late payment must settle"
    assert acc.status == AccountStatus.ACTIVE, "the customer paid — he must get his subscription"


# --- bounding the poll tail ------------------------------------------------------------------

def test_reconcile_skips_an_invoice_settled_mid_poll(session):
    """The webhook can settle this very invoice while our HTTP poll is in flight.

    Reconcile re-reads the row under a lock before touching money — but the re-read is only worth
    anything with populate_existing: without it SQLAlchemy hands back the SAME identity-mapped
    object with its stale, pre-poll attributes, the guard sails through, and on_payment_settled
    then reads a stale paid_at=None and credits a second time. This test drives exactly that race.
    """
    from sqlalchemy import update as sa_update

    from app.models import Subscription
    from app.models.base import utcnow

    acc = Account(
        account_number_hash=hash_account_number(
            generate_account_number(), get_settings().account_number_pepper
        ),
        status=AccountStatus.EXPIRED, expires_at=None, device_limit=5,
    )
    plan = PricePlan(
        code="1m", name="1m", duration_days=30, price_fiat_cents=500,
        fiat_currency="USD", device_limit=5, is_active=True, sort_order=1,
    )
    session.add_all([acc, plan])
    session.flush()
    inv = _invoice(
        session, status=InvoiceStatus.LATE_PAYMENT,
        expires_at=datetime.now(UTC) - timedelta(hours=1), address=ADDR, account=acc, plan=plan,
    )

    class _RacingProvider:
        name = "blockbee"

        def reconcile(self, *, order_id, coin, expected):
            from app.payments.base import ProviderStatus

            # The webhook lands mid-poll and settles it. synchronize_session=False is what makes
            # this a FAITHFUL simulation: the real webhook runs in another process, so our session's
            # identity map knows nothing about its commit. Without this flag SQLAlchemy helpfully
            # refreshes the mapped object for us and the test would pass even with a broken guard.
            session.execute(
                sa_update(Invoice).where(Invoice.id == inv.id)
                .values(status=InvoiceStatus.PAID, paid_at=utcnow()),
                execution_options={"synchronize_session": False},
            )
            return ProviderStatus(
                external_id=ADDR, event=PaymentEvent.SETTLED,
                amount_paid=Decimal("0.111643"), confirmations=6,
            )

    registry.register(_RacingProvider())
    moved = invoice_reconcile.reconcile_pending_invoices(session)
    session.commit()

    assert moved == 0, "the invoice was already settled — reconcile must not act on stale state"
    subs = session.query(Subscription).filter(Subscription.account_id == acc.id).all()
    assert subs == [], "reconcile must not credit on top of the webhook"


def test_reconcile_polls_a_recent_late_payment(session):
    _invoice(
        session, status=InvoiceStatus.LATE_PAYMENT,
        expires_at=datetime.now(UTC) - timedelta(days=1), address=ADDR,
    )
    registry.register(_PollProvider(event=PaymentEvent.SETTLED))
    assert invoice_reconcile.reconcile_pending_invoices(session) == 1


def test_reconcile_skips_a_stale_late_payment(session):
    """Expiry now parks every fundable invoice in late_payment, and the linkage is kept 30 days.

    Polling each abandoned one every cycle for a month would hammer the gateway with calls that can
    never settle, so auto-polling is bounded; the row stays for MANUAL rescue via the admin page.
    """
    s = get_settings()
    inv = _invoice(
        session, status=InvoiceStatus.LATE_PAYMENT,
        expires_at=datetime.now(UTC) - timedelta(hours=2), address=ADDR,
    )
    # The bound is anchored on updated_at — WHEN the row entered late_payment — not on expires_at
    # (= created + TTL), so that a long scheduler outage cannot park invoices already past the
    # cutoff. `onupdate` would stamp "now" on an ORM flush, so age the column with an explicit UPDATE.
    from sqlalchemy import update as sa_update

    old = datetime.now(UTC) - timedelta(days=s.reconcile_late_max_age_days + 1)
    session.execute(sa_update(Invoice).where(Invoice.id == inv.id).values(updated_at=old))
    session.commit()

    registry.register(_PollProvider(event=PaymentEvent.SETTLED))
    assert invoice_reconcile.reconcile_pending_invoices(session) == 0
