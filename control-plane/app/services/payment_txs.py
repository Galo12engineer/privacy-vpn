"""Accumulate per-transaction payments against one invoice.

BlockBee reports the received value PER TRANSACTION (`value_coin` + `txid_in`), never cumulatively.
The code used to assign that per-tx value straight to `crypto_amount_paid`, so a payment split
across two transactions overwrote the first part with the second: the invoice stayed UNDERPAID even
though the customer had sent the full amount, and the reconcile poll could not rescue it either
because it compared the same per-tx value. Here we keep {txid: amount} on the invoice and judge the
payment on the SUM over unique transaction ids.
"""

from __future__ import annotations

from decimal import Decimal


def total_paid(invoice) -> Decimal:
    """Sum of every distinct transaction recorded for this invoice."""
    return sum((Decimal(v) for v in (invoice.crypto_txs or {}).values()), Decimal(0))


def record_tx(invoice, *, txid: str, amount: Decimal) -> Decimal:
    """Record one on-chain transaction (idempotent per txid) and return the running total.

    Re-recording the same txid overwrites its amount instead of adding — a gateway repeats the
    callback for one transaction as it advances pending→sent→done, and those repeats must not
    inflate the total.
    """
    # normalize: case/whitespace variance on the same id would create two buckets for one
    # transaction and inflate the total. Chain txids are hex, so lowering cannot merge distinct
    # ones — and if it ever did, the result is an UNDER-count, which is the safe direction.
    key = str(txid).strip().lower()
    if not key:
        # Defence in depth: an unusable id must never open a bucket of its own. Providers are
        # expected to hand us None instead (see blockbee.parse_webhook), but a blank slipping
        # through here is the difference between an under-count and a free subscription.
        return total_paid(invoice)
    txs = dict(invoice.crypto_txs or {})
    txs[key] = str(amount)
    # reassign rather than mutate: a plain JSON column is only marked dirty on assignment
    invoice.crypto_txs = txs
    return total_paid(invoice)
