"""#6 reconcile of a lost payment: poll the provider for pending invoices that have an issued
deposit address, in case the BlockBee callback never arrived (network/CP restart/filter).

Runs the same path as the webhook (billing_fsm.apply_event → on_payment_settled), therefore it is
idempotent and safe alongside the normal callback: if the callback already arrived — the invoice is
no longer in the pending statuses and simply will not be picked up by the query; if it arrives LATER
than the reconcile — the billing FSM and the paid_at guard in on_payment_settled make the repeat a no-op.

Only BlockBee can be polled (it is now the only provider; a push-only one would not get here —
`provider.reconcile` exists only on BlockBeeProvider).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Invoice
from app.models.base import utcnow
from app.models.enums import InvoiceStatus
from app.payments import registry
from app.services import billing_fsm, purchase

log = logging.getLogger("invoice_reconcile")

_PENDING = (
    InvoiceStatus.CREATED,
    InvoiceStatus.AWAITING_CONFIRMATION,
    InvoiceStatus.UNDERPAID,
    # LATE_PAYMENT: the coins arrived, but only after the 60-min window closed (a slow BTC confirm,
    # a lost callback that surfaced late). The FSM allows LATE_PAYMENT --SETTLED--> PAID; polling
    # BlockBee and, if it reports the full amount received, crediting it is exactly the auto-rescue
    # the operator asked for. Scrub only touches PAID invoices, so a LATE_PAYMENT still carries
    # external_id/crypto_amount/crypto_currency — the fields the poll needs. NOT added to
    # invoice_expiry's own _PENDING (that would re-expire it); this set is reconcile-only.
    InvoiceStatus.LATE_PAYMENT,
)


def reconcile_pending_invoices(session: Session, *, now: datetime | None = None) -> int:
    """Returns the number of invoices whose status actually moved as a result of the poll."""
    now = now or utcnow()
    try:
        provider = registry.get("blockbee")
    except KeyError:
        # Loud on purpose. This used to `return 0` in silence, and that silence hid a permanent
        # no-op in production: the scheduler process never registered any provider, so the entire
        # lost-callback safety net did nothing for weeks and looked exactly like "nothing to do".
        # If BlockBee is the live gateway, this line means real payments cannot be rescued.
        log.warning(
            "invoice_reconcile: no 'blockbee' provider in this process — lost-payment recovery is "
            "DISABLED (registered: %s)",
            registry.names() or "none",
        )
        return 0
    poll = getattr(provider, "reconcile", None)
    if poll is None:
        log.warning("invoice_reconcile: provider 'blockbee' has no reconcile() — recovery DISABLED")
        return 0

    # Bound the LATE_PAYMENT tail. Expiry now parks every invoice that had a live deposit address
    # in late_payment (so late coins auto-credit), and the unpaid retention keeps that row for 30
    # days — polling each abandoned one every cycle for a month would hammer the gateway with calls
    # that can never settle. A genuinely late on-chain payment lands within hours. Past the window
    # the row is still there for MANUAL rescue via the admin extend page. Other pending statuses are
    # short-lived by construction (expiry moves them within the TTL), so they need no bound.
    late_cutoff = now - timedelta(days=get_settings().reconcile_late_max_age_days)
    invoices = session.execute(
        select(Invoice).where(
            Invoice.status.in_(_PENDING),
            Invoice.external_id.is_not(None),
            Invoice.crypto_amount.is_not(None),
            Invoice.crypto_currency.is_not(None),
            or_(
                Invoice.status != InvoiceStatus.LATE_PAYMENT,
                # anchor on WHEN THE ROW ENTERED late_payment (updated_at is bumped by that
                # transition), not on expires_at = created + TTL. With expires_at, a scheduler
                # outage longer than the window would park every invoice that expired during the
                # outage already past the cutoff — silently skipping the rescue for exactly the
                # invoices the outage endangered.
                Invoice.updated_at.is_(None),
                Invoice.updated_at >= late_cutoff,
            ),
        )
    ).scalars().all()

    changed = 0
    for inv in invoices:
        try:
            status = poll(order_id=str(inv.id), coin=inv.crypto_currency, expected=inv.crypto_amount)
        except Exception as e:  # noqa: BLE001 — one failing poll must not bring down the rest
            log.warning("reconcile poll failed for invoice %s: %s", inv.id, type(e).__name__)
            continue
        if status is None or status.event == billing_fsm.PaymentEvent.SEEN:
            continue  # nothing new / still in mempool — the normal path (webhook) is not lost
        # Re-read the row UNDER A LOCK before touching money. The webhook runs in a different
        # container (control-plane) than this job (scheduler), and BlockBee fires the callback and
        # flips its /logs/ endpoint at the same instant — so the webhook can settle this very
        # invoice while our HTTP poll is in flight. Both racers would then read paid_at=None and
        # both would credit. We deliberately poll OUTSIDE the lock: holding a row lock across
        # network I/O would block the webhook for the whole poll. (SQLite ignores FOR UPDATE, so
        # tests are unaffected.)
        # populate_existing is LOAD-BEARING, not decoration: without it SQLAlchemy returns the same
        # identity-mapped object with its ALREADY-LOADED (stale) attributes, so the guard below
        # would re-read the pre-poll status and sail straight through — and `expire_on_commit=False`
        # (db.py) means nothing else refreshes it either. The lock would be taken and its value
        # thrown away.
        inv = session.execute(
            select(Invoice)
            .where(Invoice.id == inv.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()
        if inv.status not in _PENDING:
            session.rollback()  # webhook got there first — release the row lock, nothing to persist
            continue
        prev = inv.status
        try:
            moved = billing_fsm.apply_event(
                inv, status.event, confirmations=status.confirmations, amount_paid=status.amount_paid,
            )
        except billing_fsm.IllegalTransition:
            session.rollback()  # release the row lock; the pre-check attribute writes go with it
            continue
        if moved:
            changed += 1
            log.warning(
                "invoice %s reconciled via poll: %s -> %s (the callback seems not to have arrived)",
                inv.id, prev, inv.status,
            )
        if billing_fsm.is_activating(inv.status):
            purchase.on_payment_settled(session, inv, now=now)
        # Commit PER INVOICE. The caller commits only after the whole batch returns, so without this
        # the FOR UPDATE lock taken above would be held across every REMAINING poll in the batch —
        # a webhook for this invoice would block for seconds-to-minutes and risk a gateway timeout.
        session.commit()
    return changed
