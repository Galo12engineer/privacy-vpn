"""A payment split across several transactions must be judged on its SUM.

The money bug (confirmed on the prod branch): BlockBee reports the received value PER TRANSACTION,
and the code assigned that value straight to `crypto_amount_paid`. So paying an invoice in two
transfers went: tx1 0.6 → UNDERPAID, amount_paid=0.6; tx2 0.4 → amount_paid OVERWRITTEN with 0.4,
still UNDERPAID (and `UNDERPAID --UNDERPAID-->` was not even a legal edge, so the transition was
swallowed). The customer had sent the full amount to the operator's payout address and got nothing;
the reconcile poll could not rescue it either, because it compared the same per-tx value. Recovery
required a human. Now the transactions are accumulated by txid and the verdict is taken on the total.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Account, Invoice, PricePlan
from app.models.enums import AccountStatus, InvoiceStatus, PaymentMethod
from app.payments import registry
from app.payments.base import ProviderStatus
from app.services.account_number import generate_account_number, hash_account_number
from app.services.billing_fsm import PaymentEvent

EXT = "addr-split-1"
EXPECTED = Decimal("1.000000")


class _PerTxProvider:
    """Blockbee-shaped: every callback describes ONE transaction."""

    name = "pertx"
    tolerance = Decimal("0.01")

    def verify_webhook(self, headers, body):
        return True

    def parse_webhook(self, headers, body):
        p = json.loads(body)
        value = Decimal(p["value"])
        # the provider's own verdict is per-tx and deliberately provisional
        event = (
            PaymentEvent.SETTLED
            if value >= EXPECTED * (Decimal(1) - self.tolerance)
            else PaymentEvent.UNDERPAID
        )
        return ProviderStatus(
            external_id=EXT, event=event, amount_paid=value, confirmations=6, txid=p["txid"]
        )


@pytest.fixture()
def client(session):
    def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    registry.clear()
    registry.register(_PerTxProvider())
    yield TestClient(app)
    app.dependency_overrides.clear()
    registry.clear()


@pytest.fixture()
def invoice(session):
    acc = Account(
        account_number_hash=hash_account_number(
            generate_account_number(), get_settings().account_number_pepper
        ),
        status=AccountStatus.EXPIRED, expires_at=None, device_limit=5,
    )
    plan = PricePlan(
        code="1m", name="1m", duration_days=30, price_fiat_cents=500,
        fiat_currency="USD", device_limit=5, is_active=True, sort_order=1,
    )
    session.add_all([acc, plan])
    session.flush()
    inv = Invoice(
        account_id=acc.id, price_plan_id=plan.id, method=PaymentMethod.BTC_ONCHAIN,
        status=InvoiceStatus.CREATED, days_granted=30, amount_fiat_cents=500,
        crypto_amount=EXPECTED, crypto_currency="BTC",
        payment_address=EXT, external_id=EXT,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    session.add(inv)
    session.commit()
    return inv


def _callback(client, *, txid, value):
    return client.post(
        "/v1/payments/webhook/pertx",
        content=json.dumps({"txid": txid, "value": value}).encode(),
    )


def test_partials_accumulate_instead_of_overwriting(client, session, invoice):
    """The core defect, isolated: two parts that still fall short must ADD UP.

    Kept below the threshold on purpose so the invoice does NOT settle — activation scrubs the
    on-chain fields immediately (retention 0), which would hide the very number under test.
    """
    _callback(client, txid="tx-a", value="0.3")
    session.refresh(invoice)
    assert invoice.crypto_amount_paid == Decimal("0.3")

    _callback(client, txid="tx-b", value="0.3")
    session.refresh(invoice)

    assert invoice.crypto_amount_paid == Decimal("0.6"), "0.3 + 0.3 — not overwritten with 0.3"
    assert invoice.status == InvoiceStatus.UNDERPAID
    assert set(invoice.crypto_txs.keys()) == {"tx-a", "tx-b"}


def test_two_partials_settle_the_invoice(client, session, invoice):
    """The money proof: 0.6 + 0.4 = the full amount, so the customer gets his subscription."""
    account_id = invoice.account_id
    _callback(client, txid="tx-a", value="0.6")
    session.refresh(invoice)
    assert invoice.status == InvoiceStatus.UNDERPAID, "half the money is genuinely underpaid"
    assert invoice.crypto_amount_paid == Decimal("0.6")

    _callback(client, txid="tx-b", value="0.4")
    session.refresh(invoice)

    assert invoice.status == InvoiceStatus.PAID
    account = session.get(Account, account_id)
    assert account.status == AccountStatus.ACTIVE, "the customer paid in full — he must be active"
    # activation scrubs the on-chain fingerprint, txids included — by design (no-logs), not a loss
    assert invoice.crypto_txs is None


def test_a_repeated_callback_for_one_tx_does_not_inflate_the_total(client, session, invoice):
    """A gateway repeats a callback for the same txid as it advances pending→sent→done."""
    _callback(client, txid="tx-a", value="0.6")
    _callback(client, txid="tx-a", value="0.6")
    session.refresh(invoice)

    assert invoice.crypto_amount_paid == Decimal("0.6"), "one transaction counted once"
    assert invoice.status == InvoiceStatus.UNDERPAID
    account = session.get(Account, invoice.account_id)
    assert account.status != AccountStatus.ACTIVE, "half a payment must not activate"


def test_scrub_erases_the_onchain_txids(session, invoice):
    """txids name the on-chain transactions outright — the strongest linkage of all."""
    from app.services.billing import scrub_invoice_linkage
    from app.services.payment_txs import record_tx

    record_tx(invoice, txid="tx-a", amount=Decimal("1.0"))
    session.commit()
    scrub_invoice_linkage(session, invoice)
    session.commit()
    session.refresh(invoice)
    assert invoice.crypto_txs is None


# --- the poll must sum too, or it cannot rescue what the webhook missed ------------------------

def test_webhook_treats_a_blank_txid_as_absent():
    """The same inflation trap on the PRIMARY path: a whitespace-only txid_in.

    Left raw it passed the route's `if status.txid` guard, normalized to "" inside record_tx, and
    opened a bucket separate from the real txid — so one 0.5 payment summed to 1.0 and settled a
    half-paid invoice, unattended. It must read as absent, so the route falls back to the
    conservative per-transaction path.
    """
    from app.payments.blockbee import BlockBeeProvider

    provider = BlockBeeProvider(
        api_key="k", callback_secret="s", payout_addresses={"BTC": "bc1q"},
        callback_base_url="https://example.test", confirmations={"BTC": 2},
    )
    body = json.dumps(
        {"address_in": EXT, "value_coin": "0.5", "expected": "1.0", "txid_in": "   ",
         "confirmations": "3", "pending": "0"}
    ).encode()

    assert provider.parse_webhook({}, body).txid is None

    # and a real id is normalized the same way the poll buckets it
    body2 = json.dumps(
        {"address_in": EXT, "value_coin": "0.5", "expected": "1.0", "txid_in": " ABC123 ",
         "confirmations": "3", "pending": "0"}
    ).encode()
    assert provider.parse_webhook({}, body2).txid == "abc123"


def test_a_blank_txid_never_opens_its_own_bucket(client, session, invoice):
    """Defence in depth at the accumulation layer, whatever a provider hands us."""
    from app.services.payment_txs import record_tx, total_paid

    record_tx(invoice, txid="tx-a", amount=Decimal("0.5"))
    total = record_tx(invoice, txid="   ", amount=Decimal("0.5"))

    assert total == Decimal("0.5"), "one payment must not be counted twice"
    assert total_paid(invoice) == Decimal("0.5")
    assert set(invoice.crypto_txs.keys()) == {"tx-a"}


def test_reconcile_never_sums_an_unidentified_entry_with_an_identified_one():
    """The inflation trap: the SAME transaction reported once with a txid and once without.

    Bucketing the unidentified one separately turned a 0.5 payment into a settled 1.0 — a free
    subscription, granted unattended, with nothing downstream comparing paid against expected.
    Under-counting is the safe direction: the invoice merely stays rescuable.
    """
    from app.payments.blockbee import BlockBeeProvider

    provider = BlockBeeProvider(
        api_key="k", callback_secret="s", payout_addresses={"BTC": "bc1q"},
        callback_base_url="https://example.test", confirmations={"BTC": 2},
    )
    provider._get = lambda ticker, path, params: {  # noqa: ARG005
        "address_in": EXT,
        "callbacks": [
            {"txid_in": "tx-a", "value_coin": "0.5", "result": "done", "confirmations": 3},
            {"value_coin": "0.5", "result": "done", "confirmations": 3},  # same tx, no txid
        ],
    }

    status = provider.reconcile(order_id="o1", coin="BTC", expected=EXPECTED)

    assert status.amount_paid == Decimal("0.5"), "one payment must not be counted twice"
    assert status.event == PaymentEvent.UNDERPAID, "half a payment must not settle the invoice"


def test_reconcile_sums_confirmed_transactions():
    from app.payments.blockbee import BlockBeeProvider

    provider = BlockBeeProvider(
        api_key="k", callback_secret="s", payout_addresses={"BTC": "bc1q"},
        callback_base_url="https://example.test", confirmations={"BTC": 2},
    )
    provider._get = lambda ticker, path, params: {  # noqa: ARG005
        "address_in": EXT,
        "callbacks": [
            # the same tx reported twice as it advances — must count once
            {"txid_in": "tx-a", "value_coin": "0.6", "result": "sent", "confirmations": 1},
            {"txid_in": "tx-a", "value_coin": "0.6", "result": "done", "confirmations": 3},
            {"txid_in": "tx-b", "value_coin": "0.4", "result": "done", "confirmations": 2},
        ],
    }

    status = provider.reconcile(order_id="o1", coin="BTC", expected=EXPECTED)

    assert status is not None
    assert status.amount_paid == Decimal("1.0"), "0.6 + 0.4, with the duplicate counted once"
    assert status.event == PaymentEvent.SETTLED
