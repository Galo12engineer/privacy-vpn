"""Invoice state machine. Provider-agnostic: adapters emit normalized events.

States: created/awaiting_confirmation/underpaid/overpaid/paid/expired/late_payment/cancelled.
- Lightning → instant path (SETTLED right away).
- On-chain → SEEN (0-conf) → SETTLED at N confirmations.
- Rate-lock window: payment after rate_expires_at with a shortfall → late_payment.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.models import Invoice
from app.models.enums import InvoiceStatus


class PaymentEvent(StrEnum):
    SEEN = "seen"          # payment spotted (mempool / <N confirmations)
    SETTLED = "settled"    # paid in full and confirmed
    UNDERPAID = "underpaid"
    OVERPAID = "overpaid"
    EXPIRED = "expired"
    # The window closed while a deposit address was LIVE, so coins can still land on it. Distinct
    # from EXPIRED because the destination must stay RECONCILABLE (late_payment), not terminal.
    EXPIRED_FUNDABLE = "expired_fundable"
    CANCELLED = "cancelled"


class IllegalTransition(Exception):
    pass


S = InvoiceStatus
E = PaymentEvent

# transition graph: {current status: {event: new status}}
_TRANSITIONS: dict[InvoiceStatus, dict[PaymentEvent, InvoiceStatus]] = {
    S.CREATED: {
        E.SEEN: S.AWAITING_CONFIRMATION,
        E.SETTLED: S.PAID,
        E.UNDERPAID: S.UNDERPAID,
        E.OVERPAID: S.OVERPAID,
        E.EXPIRED: S.EXPIRED,  # no address was ever issued → money cannot arrive → terminal is right
        # An address WAS live when the hour ran out. Sending this to EXPIRED silently ate late
        # payments: EXPIRED is terminal AND outside invoice_reconcile's poll scope, so a customer
        # whose BTC left the exchange late (not in mempool within 60 min — routine) paid for real,
        # the webhook hit IllegalTransition and still answered *ok* (BlockBee then stops retrying),
        # and nothing ever credited them. LATE_PAYMENT keeps it reconcilable: the poll sees the
        # confirmed full amount and auto-settles via LATE_PAYMENT --SETTLED--> PAID below.
        E.EXPIRED_FUNDABLE: S.LATE_PAYMENT,
        E.CANCELLED: S.CANCELLED,
    },
    S.AWAITING_CONFIRMATION: {
        E.SEEN: S.AWAITING_CONFIRMATION,
        E.SETTLED: S.PAID,
        E.UNDERPAID: S.UNDERPAID,
        E.OVERPAID: S.OVERPAID,
        E.EXPIRED: S.LATE_PAYMENT,  # the money arrived, but the window has closed
        E.EXPIRED_FUNDABLE: S.LATE_PAYMENT,  # same destination; the address was live either way
        E.CANCELLED: S.CANCELLED,
    },
    S.UNDERPAID: {
        E.SEEN: S.UNDERPAID,
        # another partial transaction arrived and the running total is still short. Legal and
        # status-preserving: without this edge the second part of a split payment raised
        # IllegalTransition, and the only reason the amount got recorded at all was that
        # apply_event writes it before checking the transition.
        E.UNDERPAID: S.UNDERPAID,
        E.SETTLED: S.PAID,        # topped up to the full amount
        E.OVERPAID: S.OVERPAID,
        E.EXPIRED: S.LATE_PAYMENT,
        E.EXPIRED_FUNDABLE: S.LATE_PAYMENT,
        E.CANCELLED: S.CANCELLED,
    },
    # terminal / near-terminal
    S.PAID: {},
    S.OVERPAID: {},
    S.EXPIRED: {},
    S.LATE_PAYMENT: {E.SETTLED: S.PAID},  # operator/automation decides whether to credit the late payment
    S.CANCELLED: {},
}

# statuses at which we grant time — i.e. "the money arrived". Public because it is also the ONLY
# honest answer to "was this invoice paid?" outside the FSM: paid_at cannot answer it (the scrub
# nulls it deliberately, see billing.scrub_invoice_linkage), so anything reporting payment — the
# admin console included — reads the status, and an SQL-side count needs the set itself.
ACTIVATING_STATUSES = frozenset({S.PAID, S.OVERPAID})


def is_activating(status: InvoiceStatus) -> bool:
    return status in ACTIVATING_STATUSES


def is_terminal(status: InvoiceStatus) -> bool:
    return not _TRANSITIONS.get(status)


def apply_event(
    invoice: Invoice,
    event: PaymentEvent,
    *,
    now: datetime | None = None,
    confirmations: int | None = None,
    amount_paid: Decimal | None = None,
) -> bool:
    """Apply an event to the invoice. True if the status changed. IllegalTransition — not allowed."""
    if confirmations is not None:
        invoice.confirmations_seen = confirmations
    if amount_paid is not None:
        invoice.crypto_amount_paid = amount_paid

    allowed = _TRANSITIONS.get(invoice.status, {})
    if event not in allowed:
        raise IllegalTransition(f"{invoice.status} -/-> {event}")
    new_status = allowed[event]
    changed = new_status != invoice.status
    invoice.status = new_status
    return changed
