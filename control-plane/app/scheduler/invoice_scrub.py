"""Deferred scrub of the invoice↔account linkage per the retention window (no-logs)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Invoice
from app.models.base import as_utc, utcnow
from app.models.enums import InvoiceStatus
from app.services import promo
from app.services.billing import scrub_invoice_linkage

# terminally unpaid: no money was credited to the account, but the account↔address linkage is still hanging around
_TERMINAL_UNPAID = (
    InvoiceStatus.EXPIRED,
    InvoiceStatus.LATE_PAYMENT,
    InvoiceStatus.CANCELLED,
)


def scrub_due_invoices(
    session: Session,
    *,
    now: datetime | None = None,
    retention_minutes: int | None = None,
    unpaid_retention_minutes: int | None = None,
) -> int:
    now = now or utcnow()
    s = get_settings()
    rm = retention_minutes if retention_minutes is not None else s.invoice_link_retention_minutes
    urm = (
        unpaid_retention_minutes
        if unpaid_retention_minutes is not None
        else s.invoice_unpaid_retention_minutes
    )
    count = 0
    # (1) paid (activated) — scrub after the retention window counted from paid_at
    invoices = session.execute(
        select(Invoice).where(
            Invoice.paid_at.is_not(None),
            Invoice.scrubbed_at.is_(None),
            Invoice.account_id.is_not(None),
        )
    ).scalars().all()
    for inv in invoices:
        paid = as_utc(inv.paid_at)
        if paid is not None and paid + timedelta(minutes=rm) <= now:
            scrub_invoice_linkage(session, inv, now=now)
            count += 1
    # (2) #4: terminally unpaid ones hold account_id + (for LATE_PAYMENT) a funded address.
    # We keep the unpaid_retention window (reconcile/refund of a possible incoming payment), then clean up —
    # we do not store the payment↔account linkage forever. The baseline for counting is expires_at.
    unpaid = session.execute(
        select(Invoice).where(
            Invoice.status.in_(_TERMINAL_UNPAID),
            Invoice.scrubbed_at.is_(None),
            Invoice.account_id.is_not(None),
            Invoice.expires_at.is_not(None),
        )
    ).scalars().all()
    for inv in unpaid:
        exp = as_utc(inv.expires_at)
        if exp is not None and exp + timedelta(minutes=urm) <= now:
            # Backstop for the promo allowance, BEFORE the scrub wipes the link that identifies
            # which code to credit. Expiry already refunds the common case within the hour, but it
            # is not the only way into this set: CANCELLED arrives from a provider webhook and
            # never passes through expiry, so without this the use would be held for the whole
            # retention window and then lost for good when promo_code_id was nulled below.
            # Funded invoices keep their use — release_if_unfunded owns that distinction.
            promo.release_if_unfunded(session, inv)
            scrub_invoice_linkage(session, inv, now=now)
            count += 1
    return count
