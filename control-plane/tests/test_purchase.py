from datetime import UTC, datetime, timedelta

import pytest

from app.models import Subscription
from app.models.enums import AccountStatus, PaymentMethod
from app.services.billing import evaluate_status
from app.services.purchase import UnknownAccount, on_payment_settled, start_purchase

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def test_first_purchase_mints_account(session, price_plan):
    res = start_purchase(session, price_plan=price_plan, method=PaymentMethod.LIGHTNING, now=NOW)
    session.commit()
    assert res.is_new is True
    assert res.account_number is not None  # plaintext is returned ONCE
    assert res.invoice.days_granted == 30


def test_settle_activates_account(session, price_plan):
    res = start_purchase(session, price_plan=price_plan, method=PaymentMethod.LIGHTNING, now=NOW)
    session.commit()
    applied = on_payment_settled(session, res.invoice, now=NOW)
    session.commit()
    assert applied is True
    assert res.account.status == AccountStatus.ACTIVE
    assert res.account.expires_at == NOW + timedelta(days=30)


def test_double_settle_is_idempotent(session, price_plan):
    res = start_purchase(session, price_plan=price_plan, method=PaymentMethod.MONERO, now=NOW)
    session.commit()
    assert on_payment_settled(session, res.invoice, now=NOW) is True
    session.commit()
    assert on_payment_settled(session, res.invoice, now=NOW) is False  # repeat — no-op
    session.commit()
    subs = session.query(Subscription).filter(Subscription.account_id == res.account.id).all()
    assert len(subs) == 1  # time credited only once


def test_repurchase_extends_existing_account(session, price_plan):
    res1 = start_purchase(session, price_plan=price_plan, method=PaymentMethod.LIGHTNING, now=NOW)
    session.commit()
    on_payment_settled(session, res1.invoice, now=NOW)
    session.commit()
    number = res1.account_number

    res2 = start_purchase(
        session,
        price_plan=price_plan,
        method=PaymentMethod.LIGHTNING,
        account_number=number,
        now=NOW,
    )
    session.commit()
    assert res2.is_new is False
    assert res2.account.id == res1.account.id
    on_payment_settled(session, res2.invoice, now=NOW)
    session.commit()
    assert res2.account.expires_at == NOW + timedelta(days=60)  # stacks up


def test_unknown_account_rejected(session, price_plan):
    with pytest.raises(UnknownAccount):
        start_purchase(
            session,
            price_plan=price_plan,
            method=PaymentMethod.LIGHTNING,
            account_number="0000 0000 0000 0000 0000",
            now=NOW,
        )


def test_activation_scrubs_click_id_and_fires_postback(session, price_plan, monkeypatch):
    # #8: click_id is wiped on activation (we do not keep ad-click↔account forever); postback — once
    res = start_purchase(session, price_plan=price_plan, method=PaymentMethod.LIGHTNING, now=NOW)
    res.account.attribution_click_id = "click-xyz"
    session.commit()

    fired = {"n": 0}
    monkeypatch.setattr(
        "app.services.attribution.maybe_fire",
        lambda sess, acc, **kw: fired.__setitem__("n", fired["n"] + 1) or True,
    )
    assert on_payment_settled(session, res.invoice, now=NOW) is True
    session.commit()
    assert fired["n"] == 1
    assert res.account.attribution_click_id is None
    assert res.account.status == AccountStatus.ACTIVE


def test_tracker_failure_does_not_block_activation(session, price_plan, monkeypatch):
    # a tracker failure MUST NOT break activation; click_id is wiped anyway
    res = start_purchase(session, price_plan=price_plan, method=PaymentMethod.LIGHTNING, now=NOW)
    res.account.attribution_click_id = "click-xyz"
    session.commit()

    def boom(*a, **k):
        raise RuntimeError("tracker down")

    monkeypatch.setattr("app.services.attribution.maybe_fire", boom)
    assert on_payment_settled(session, res.invoice, now=NOW) is True
    session.commit()
    assert res.account.status == AccountStatus.ACTIVE
    assert res.account.attribution_click_id is None


def test_scrub_breaks_link_after_activation(session, price_plan):
    res = start_purchase(session, price_plan=price_plan, method=PaymentMethod.MONERO, now=NOW)
    session.commit()
    on_payment_settled(session, res.invoice, now=NOW)  # retention=0 → scrub immediately
    session.commit()
    assert res.invoice.account_id is None
    assert res.invoice.scrubbed_at is not None
    # after-scrub: paid_at and the on-chain fingerprint are nulled (no-logs, cannot be re-linked to the tx)
    assert res.invoice.paid_at is None
    assert res.invoice.external_id is None
    assert res.invoice.crypto_amount is None
    assert res.invoice.exchange_rate is None
    # scrubbed_at is coarsened to a day (no precise timing for a join with Subscription.starts_at)
    assert res.invoice.scrubbed_at.hour == 0 and res.invoice.scrubbed_at.minute == 0
    # a repeated webhook after the scrub — still a no-op (guard: account_id is None)
    assert on_payment_settled(session, res.invoice, now=NOW) is False
    # the account is still active
    assert evaluate_status(res.account, now=NOW) == AccountStatus.ACTIVE
