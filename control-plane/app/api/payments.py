"""Payment webhooks. Idempotent, bound by external_id, provider signature verification."""

from __future__ import annotations

import json
import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Invoice
from app.payments import registry
from app.services import billing_fsm, payment_txs, purchase

router = APIRouter(prefix="/v1/payments", tags=["payments"])
log = logging.getLogger("payments")


def _dispatch(provider_name: str, headers: dict, body: bytes, session: Session) -> bool:
    """Provider-agnostic webhook parsing. True = invoice found and processed,
    False = unknown invoice (idempotent no-op). 404/401 → HTTPException."""
    try:
        provider = registry.get(provider_name)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown provider")

    if not provider.verify_webhook(headers, body):
        raise HTTPException(status_code=401, detail="bad signature")

    status = provider.parse_webhook(headers, body)
    # FOR UPDATE: otherwise two simultaneous SETTLED callbacks would both read paid_at=None
    # and credit twice. The lock serializes them; the second one, once unblocked, will see an
    # already activated/scrubbed invoice → no-op. (On SQLite it's a no-op, tests are single-threaded.)
    invoice = session.execute(
        select(Invoice).where(Invoice.external_id == status.external_id).with_for_update()
    ).scalar_one_or_none()
    if invoice is None:
        # unknown invoice (or an already scrubbed external_id) — idempotent no-op
        return False

    # Judge the payment on the TOTAL, not on the transaction that happened to arrive last. The
    # gateway reports value per tx, so a customer paying in two transfers used to have the second
    # callback overwrite the first amount, leaving a fully-paid invoice UNDERPAID forever (the poll
    # compared the same per-tx value, so it could not rescue it either). Recording by txid is also
    # what makes a repeated callback for one transaction harmless.
    event, amount = status.event, status.amount_paid
    if (
        not status.txid
        and status.event != billing_fsm.PaymentEvent.SEEN
        and invoice.crypto_txs
    ):
        # A confirmed callback we cannot attribute to a transaction, on an invoice that already has
        # some. We deliberately do NOT count it (an unidentifiable id must never open its own
        # bucket — that inflates the total and hands out a free subscription), which means the
        # running total may now UNDER-count a real payment and the invoice will not auto-settle.
        # That under-count is otherwise completely silent: nothing compares paid against expected
        # after the fact and the monitor does not look at invoices at all. Say it out loud so the
        # operator can reconcile by hand while the linkage is still retained. Invoice UUID only —
        # no address, no amount, no account (no-logs).
        log.warning(
            "invoice %s: confirmed callback without a usable txid — not counted, total may "
            "under-report a real payment; reconcile manually",
            invoice.id,
        )
    if status.txid and status.amount_paid is not None and status.event != billing_fsm.PaymentEvent.SEEN:
        amount = payment_txs.record_tx(invoice, txid=status.txid, amount=status.amount_paid)
        expected = invoice.crypto_amount
        if expected and expected > 0:
            tolerance = getattr(provider, "tolerance", Decimal(0))
            event = (
                billing_fsm.PaymentEvent.SETTLED
                if amount >= expected * (Decimal(1) - tolerance)
                else billing_fsm.PaymentEvent.UNDERPAID
            )

    try:
        billing_fsm.apply_event(
            invoice,
            event,
            confirmations=status.confirmations,
            amount_paid=amount,
        )
    except billing_fsm.IllegalTransition:
        pass  # out-of-order/duplicate event — ignore it (idempotency)

    if billing_fsm.is_activating(invoice.status):
        purchase.on_payment_settled(session, invoice)

    session.commit()
    return True


@router.post("/webhook/{provider_name}")
async def webhook(provider_name: str, request: Request, session: Session = Depends(get_session)):
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    # _dispatch is synchronous (sync DB + best-effort httpx attribution) → run it in a threadpool,
    # otherwise it blocks the single event loop and freezes other async requests/webhooks.
    processed = await run_in_threadpool(_dispatch, provider_name, headers, body, session)
    return {"ok": True, "ignored": not processed}


@router.get("/webhook/{provider_name}")
async def webhook_get(provider_name: str, request: Request, session: Session = Depends(get_session)):
    """GET callback (BlockBee sends GET by default). We synthesize the body from the query
    parameters so that the provider's verify/parse work uniformly, and answer `*ok*` (stops retries)."""
    headers = {k.lower(): v for k, v in request.headers.items()}
    body = json.dumps(dict(request.query_params)).encode()
    await run_in_threadpool(_dispatch, provider_name, headers, body, session)
    return PlainTextResponse("*ok*")
