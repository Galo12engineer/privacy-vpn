from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Account, Invoice, Subscription
from app.models.enums import AccountStatus, InvoiceStatus, PaymentMethod
from app.services.billing import (
    apply_credit,
    evaluate_status,
    mark_invoice_paid,
    scrub_invoice_linkage,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _account(session, suffix: str, expires_at=None) -> Account:
    a = Account(
        account_number_hash=f"hash-{suffix}".ljust(64, "0"),
        status=AccountStatus.EXPIRED,
        expires_at=expires_at,
        device_limit=5,
    )
    session.add(a)
    session.commit()
    return a


def test_apply_credit_new_account_starts_from_now(session):
    a = _account(session, "new")
    apply_credit(session, a, days=30, now=NOW)
    assert a.expires_at == NOW + timedelta(days=30)
    assert a.status == AccountStatus.ACTIVE


def test_apply_credit_stacks_from_future_expiry(session):
    a = _account(session, "stack", expires_at=NOW + timedelta(days=10))
    apply_credit(session, a, days=30, now=NOW)
    # stacks from the current expires_at, not from now
    assert a.expires_at == NOW + timedelta(days=40)


def test_apply_credit_lapsed_account_starts_from_now(session):
    a = _account(session, "lapsed", expires_at=NOW - timedelta(days=5))
    apply_credit(session, a, days=30, now=NOW)
    assert a.expires_at == NOW + timedelta(days=30)


def test_apply_credit_creates_ledger_row(session):
    a = _account(session, "ledger")
    apply_credit(session, a, days=30, now=NOW, price_plan_code="1m")
    session.commit()
    subs = session.query(Subscription).filter(Subscription.account_id == a.id).all()
    assert len(subs) == 1
    assert subs[0].days == 30
    assert subs[0].price_plan_code == "1m"


def test_apply_credit_updates_device_limit(session):
    a = _account(session, "dev")
    apply_credit(session, a, days=30, now=NOW, device_limit=7)
    assert a.device_limit == 7


@pytest.mark.parametrize(
    "delta_days,expected",
    [
        (10, AccountStatus.ACTIVE),   # time still remains
        (-1, AccountStatus.GRACE),    # expired 1 day ago, grace=3
        (-5, AccountStatus.EXPIRED),  # grace has run out
    ],
)
def test_evaluate_status(session, delta_days, expected):
    a = _account(session, f"st{delta_days}", expires_at=NOW + timedelta(days=delta_days))
    assert evaluate_status(a, now=NOW, grace_days=3) == expected


def test_evaluate_status_never_funded_is_expired(session):
    a = _account(session, "never")
    assert evaluate_status(a, now=NOW, grace_days=3) == AccountStatus.EXPIRED


def test_scrub_breaks_invoice_account_link(session):
    a = _account(session, "scrub")
    inv = Invoice(
        account_id=a.id,
        method=PaymentMethod.MONERO,
        status=InvoiceStatus.PAID,
        days_granted=30,
        amount_fiat_cents=500,
        payment_address="addr-123",
        external_id="ext-123",
        checkout_url="https://pay/ext-123",
        crypto_currency="XMR",
        crypto_amount=Decimal("1.234"),
        crypto_amount_paid=Decimal("1.234"),
        exchange_rate=Decimal("405.00"),
    )
    session.add(inv)
    session.commit()
    sub = apply_credit(session, a, days=30, now=NOW, invoice_id=inv.id)
    session.commit()

    mark_invoice_paid(session, inv, now=NOW, confirmations=10)
    scrub_invoice_linkage(session, inv, now=NOW)
    session.commit()

    assert inv.account_id is None
    assert inv.payment_address is None
    assert inv.external_id is None
    assert inv.scrubbed_at is not None
    # on-chain fingerprint and exact timestamps are wiped (cannot be re-linked to a tx/subscription)
    assert inv.checkout_url is None
    assert inv.crypto_currency is None
    assert inv.crypto_amount is None
    assert inv.crypto_amount_paid is None
    assert inv.exchange_rate is None
    assert inv.paid_at is None
    # account-level aggregate accounting is preserved (not linkable to a tx)
    assert inv.amount_fiat_cents == 500
    assert inv.days_granted == 30
    session.refresh(sub)
    assert sub.invoice_id is None  # the subscription→invoice bridge is broken
    # the account's time is nevertheless preserved
    assert a.expires_at == NOW + timedelta(days=30)


def test_second_credit_for_one_invoice_is_a_no_op(session):
    """#2 one invoice → at most one credit, now enforced in code as well as by the UNIQUE index.

    apply_credit used to let the duplicate through to the DB and blow up with IntegrityError. That
    backstop still stands (see the raw-insert test below), but the app path must not rely on an
    exception: a manual operator grant and the automatic late-payment rescue legitimately BOTH run
    for the same invoice, and the second one has to be a quiet no-op, not a 500.
    """
    a = _account(session, "uniq")
    inv = Invoice(
        account_id=a.id,
        method=PaymentMethod.MONERO,
        status=InvoiceStatus.PAID,
        days_granted=30,
        amount_fiat_cents=500,
    )
    session.add(inv)
    session.commit()
    first = apply_credit(session, a, days=30, now=NOW, invoice_id=inv.id)
    second = apply_credit(session, a, days=30, now=NOW, invoice_id=inv.id)  # the same invoice_id
    session.commit()  # must NOT raise
    assert second is first
    assert a.expires_at == NOW + timedelta(days=30), "the term must not stack twice"


def test_subscription_invoice_id_unique_backstop(session):
    """The DB backstop stays: anything bypassing apply_credit still cannot double-credit."""
    a = _account(session, "uniq2")
    inv = Invoice(
        account_id=a.id,
        method=PaymentMethod.MONERO,
        status=InvoiceStatus.PAID,
        days_granted=30,
        amount_fiat_cents=500,
    )
    session.add(inv)
    session.commit()
    apply_credit(session, a, days=30, now=NOW, invoice_id=inv.id)
    session.commit()
    # a raw duplicate (not via apply_credit) must still hit the UNIQUE index
    session.add(
        Subscription(
            account_id=a.id, days=30, starts_at=NOW, ends_at=NOW + timedelta(days=30),
            invoice_id=inv.id,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    a = _account(session, "uniq3")
    # NULL invoice_id (scrubbed subscriptions) is allowed many times over
    apply_credit(session, a, days=1, now=NOW)
    apply_credit(session, a, days=1, now=NOW)
    session.commit()  # does not fail


def test_apply_credit_rereads_expires_at_under_the_lock(session):
    """A second credit must stack on the CURRENT expires_at, not on a stale in-memory copy.

    Regression: apply_credit read `account.expires_at` straight off the ORM object. Everything in
    the money path is deduplicated per INVOICE (the guard + the unique index from 0009), which is no
    defence against two credits for DIFFERENT invoices: both read the same expires_at, both write
    base+days, last writer wins. The customer paid for two terms and got one, while the Subscription
    ledger recorded both — silent, and reachable when an operator grants days exactly as a payment
    lands. The fix locks the account row and reads expires_at back as a SCALAR — the identity map
    would otherwise hand back the pre-lock value and the lock would mean nothing. That staleness
    half is the one a single-threaded SQLite test can actually prove.
    """
    account = _account(session, "relock", expires_at=NOW + timedelta(days=30))
    # Somebody else's credit lands and commits while we hold a stale object: push expires_at out
    # behind the ORM's back, exactly what a concurrent transaction would leave us looking at.
    session.execute(
        Account.__table__.update()
        .where(Account.__table__.c.id == account.id)
        .values(expires_at=NOW + timedelta(days=60))
    )
    assert account.expires_at == NOW + timedelta(days=30)  # identity map is still stale

    apply_credit(session, account, days=30, now=NOW)
    session.commit()

    # stacked on 60, not on the stale 30 → 90. Reading expires_at off the ORM object gives 60 here,
    # i.e. a paid month has silently vanished.
    assert account.expires_at == NOW + timedelta(days=90)
