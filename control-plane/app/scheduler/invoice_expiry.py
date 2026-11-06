"""Invoice expiry. Without this, an unpaid/underpaid invoice hangs in CREATED/UNDERPAID
forever, the rate is "locked in" at a stale quote, and a late payment triggers a false UNDERPAID.

After expires_at we apply PaymentEvent.EXPIRED: CREATED→EXPIRED, AWAITING/UNDERPAID→LATE_PAYMENT
(the money arrived, but the window closed — the operator/logic decides on the late payment).
See billing_fsm.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Invoice
from app.models.base import as_utc, utcnow
from app.models.enums import InvoiceStatus
from app.services import billing_fsm, promo

log = logging.getLogger("invoice_expiry")

_PENDING = (
    InvoiceStatus.CREATED,
    InvoiceStatus.AWAITING_CONFIRMATION,
    InvoiceStatus.UNDERPAID,
)


def expire_stale_invoices(session: Session, *, now: datetime | None = None) -> int:
    """Mark stale pending invoices with an EXPIRED event. Returns the number changed."""
    now = now or utcnow()
    count = 0
    invoices = session.execute(
        select(Invoice).where(
            Invoice.status.in_(_PENDING),
            Invoice.expires_at.is_not(None),
        )
    ).scalars().all()
    for inv in invoices:
        exp = as_utc(inv.expires_at)  # SQLite tz nuance: we compare in Python as aware-UTC
        if exp is None or exp > now:
            continue
        prev = inv.status
        # a deposit address was issued (a real invoice) → a "silent" loss of payment is possible
        had_address = inv.payment_address is not None
        # Route by FUNDABILITY, not just by the clock. This used to always emit EXPIRED, and from
        # CREATED that lands in a TERMINAL state outside invoice_reconcile's poll scope — so an
        # invoice whose deposit address was still live when the hour ran out ate a late payment in
        # silence (the old code knew: it logged "expired with a deposit address issued" and left the
        # rest to a human). With a live address we emit EXPIRED_FUNDABLE → LATE_PAYMENT, which
        # reconcile does poll, so late coins auto-credit instead of waiting on manual reconciliation.
        event = (
            billing_fsm.PaymentEvent.EXPIRED_FUNDABLE
            if had_address
            else billing_fsm.PaymentEvent.EXPIRED
        )
        try:
            if billing_fsm.apply_event(inv, event, now=now):
                count += 1
                # An abandoned cart gives its promo use back, so that reserving at creation cannot
                # be used to destroy a code: twenty unpaid POSTs zeroed a max_uses=20 code and
                # every real customer then saw "limit reached" (reproduced).
                #
                # The discriminator is WHETHER COINS WERE EVER SEEN, not which terminal state the
                # FSM picked. Keying on `status == EXPIRED` looked right and was dead code on prod:
                # EXPIRED requires that no deposit address was ever issued, and BlockBee is
                # address-per-order, so every real invoice carries one and lands in LATE_PAYMENT
                # instead. The test that "proved" the release hand-set payment_address=None — a
                # state production cannot reach — so it passed against a defence that never fired.
                # crypto_amount_paid/crypto_txs is the same "did money actually arrive" test the
                # admin console already uses to tell a real late payer from an abandoned cart
                # (admin_service.rescuable_invoices).
                #
                # Deliberate imprecision, in the safe direction: coins that arrive AFTER we release
                # still settle normally (reconcile polls LATE_PAYMENT), and the counter is then one
                # short. Under-counting hands out at most one extra discount per genuinely-late
                # payment; over-counting hands an attacker a permanent kill switch on the campaign.
                if promo.release_if_unfunded(session, inv):
                    log.warning("invoice %s expired with no payment seen: promo use returned", inv.id)
                # #6: without reconcile, a lost callback = the money arrived, but there is no subscription.
                # We do not stay silent: we log it (the invoice UUID is not PII; we do NOT write
                # the address/amount/account), so that monitoring/the operator can reconcile the payment manually.
                if inv.status == InvoiceStatus.LATE_PAYMENT:
                    log.warning(
                        "invoice %s %s->late_payment: the address was live — reconcile will poll "
                        "for the incoming payment",
                        inv.id, prev,
                    )
        except billing_fsm.IllegalTransition:
            pass
    return count
