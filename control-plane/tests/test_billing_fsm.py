import pytest

from app.models import Invoice
from app.models.enums import InvoiceStatus, PaymentMethod
from app.services.billing_fsm import (
    IllegalTransition,
    PaymentEvent,
    apply_event,
    is_activating,
    is_terminal,
)

S = InvoiceStatus
E = PaymentEvent


def _inv(status: S = S.CREATED) -> Invoice:
    return Invoice(
        method=PaymentMethod.LIGHTNING, status=status, days_granted=30, amount_fiat_cents=500
    )


def test_lightning_instant_settles():
    inv = _inv()
    assert apply_event(inv, E.SETTLED) is True
    assert inv.status == S.PAID
    assert is_activating(inv.status)


def test_onchain_seen_then_settled():
    inv = _inv()
    apply_event(inv, E.SEEN, confirmations=1)
    assert inv.status == S.AWAITING_CONFIRMATION
    assert not is_activating(inv.status)
    apply_event(inv, E.SETTLED, confirmations=10)
    assert inv.status == S.PAID


def test_underpaid_then_topup():
    inv = _inv()
    apply_event(inv, E.UNDERPAID)
    assert inv.status == S.UNDERPAID
    apply_event(inv, E.SETTLED)
    assert inv.status == S.PAID


def test_overpaid_is_activating():
    inv = _inv()
    apply_event(inv, E.OVERPAID)
    assert inv.status == S.OVERPAID
    assert is_activating(inv.status)


def test_expired_from_created():
    inv = _inv()
    apply_event(inv, E.EXPIRED)
    assert inv.status == S.EXPIRED
    assert is_terminal(inv.status)


def test_late_payment_when_expired_after_seen():
    inv = _inv()
    apply_event(inv, E.SEEN)
    apply_event(inv, E.EXPIRED)
    assert inv.status == S.LATE_PAYMENT
    # a late payment can still be credited
    apply_event(inv, E.SETTLED)
    assert inv.status == S.PAID


def test_illegal_transition_raises():
    inv = _inv(S.PAID)
    with pytest.raises(IllegalTransition):
        apply_event(inv, E.SETTLED)


def test_confirmations_and_amount_recorded():
    inv = _inv()
    apply_event(inv, E.SEEN, confirmations=3)
    assert inv.confirmations_seen == 3
