"""DEV-ONLY routes for local testing (payment simulation). Mounted only when
settings.dev_fake_payments is set. NEVER in prod.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Invoice
from app.services import billing_fsm, purchase

router = APIRouter(prefix="/dev", tags=["dev"])


@router.get("/pay/{invoice_id}")
def fake_pay(invoice_id: str, session: Session = Depends(get_session)):
    """Simulate a confirmed invoice payment → activate the subscription (like the real webhook)."""
    inv = session.get(Invoice, uuid.UUID(invoice_id))
    if inv is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    try:
        billing_fsm.apply_event(inv, billing_fsm.PaymentEvent.SETTLED)
    except billing_fsm.IllegalTransition:
        pass  # already paid/terminal — idempotent
    if billing_fsm.is_activating(inv.status):
        purchase.on_payment_settled(session, inv)
    session.commit()
    return RedirectResponse(f"/invoice/{invoice_id}", status_code=303)
