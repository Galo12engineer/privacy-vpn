import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app
from app.models import Subscription
from app.models.enums import AccountStatus, InvoiceStatus, PaymentMethod
from app.payments import registry
from app.payments.base import ProviderStatus
from app.scheduler.invoice_reconcile import reconcile_pending_invoices
from app.services.billing_fsm import PaymentEvent, is_activating
from app.services.purchase import start_purchase
from tests._fakes import FakeProvider

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture()
def client(session):
    def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    registry.clear()
    yield TestClient(app)
    app.dependency_overrides.clear()
    registry.clear()


def test_webhook_activates_account(client, session, price_plan):
    fake = FakeProvider(external_id="ext-aaa")
    registry.register(fake)
    res = start_purchase(
        session, price_plan=price_plan, method=PaymentMethod.MONERO, provider=fake, now=NOW
    )
    session.commit()

    body = json.dumps({"external_id": "ext-aaa", "event": "settled"}).encode()
    resp = client.post("/v1/payments/webhook/fake", content=body)
    assert resp.status_code == 200
    session.refresh(res.account)
    assert res.account.status == AccountStatus.ACTIVE


def test_webhook_is_idempotent(client, session, price_plan):
    fake = FakeProvider(external_id="ext-bbb")
    registry.register(fake)
    res = start_purchase(
        session, price_plan=price_plan, method=PaymentMethod.MONERO, provider=fake, now=NOW
    )
    session.commit()
    account_id = res.account.id

    body = json.dumps({"external_id": "ext-bbb", "event": "settled"}).encode()
    client.post("/v1/payments/webhook/fake", content=body)
    client.post("/v1/payments/webhook/fake", content=body)  # repeat

    subs = session.query(Subscription).filter(Subscription.account_id == account_id).all()
    assert len(subs) == 1  # credited once


def test_unknown_provider_404(client):
    resp = client.post("/v1/payments/webhook/nope", content=b"{}")
    assert resp.status_code == 404


def test_unknown_invoice_ignored(client, session):
    fake = FakeProvider(external_id="does-not-exist")
    registry.register(fake)
    body = json.dumps({"external_id": "does-not-exist", "event": "settled"}).encode()
    resp = client.post("/v1/payments/webhook/fake", content=body)
    assert resp.status_code == 200
    assert resp.json().get("ignored") is True


def _register_blockbee():
    from app.payments.blockbee import BlockBeeProvider

    bb = BlockBeeProvider(
        api_key="bb-key",
        callback_secret="sekret-nonce",
        payout_addresses={},
        callback_base_url="https://privacyvpn.example.com",
        confirmations={},
    )
    registry.register(bb)
    return bb


def _bb_token(order_id: str, expected: str, secret: str = "sekret-nonce") -> str:
    return hmac.new(secret.encode(), f"{order_id}:{expected}".encode(), hashlib.sha256).hexdigest()


def test_blockbee_get_callback_activates_account(client, session, price_plan):
    # invoice with external_id = address_in (seeded via FakeProvider, so we don't hit the BlockBee network)
    seed = FakeProvider(name="seed", external_id="TPayAddr-1")
    registry.register(seed)
    res = start_purchase(
        session, price_plan=price_plan, method=PaymentMethod.USDT_TRC20, provider=seed, now=NOW
    )
    session.commit()
    order_id = str(res.invoice.id)

    _register_blockbee()
    # BlockBee sends a GET to the callback URL, appending its own fields + our token/order_id/expected
    resp = client.get(
        "/v1/payments/webhook/blockbee",
        params={
            "token": _bb_token(order_id, "0.5"),
            "order_id": order_id,
            "expected": "0.5",
            "address_in": "TPayAddr-1",
            "txid_in": "deadbeef",
            "confirmations": "25",
            "value_coin": "0.5",
            "pending": "0",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "*ok*"
    session.refresh(res.account)
    assert res.account.status == AccountStatus.ACTIVE


def test_blockbee_get_callback_bad_token_rejected(client, session):
    _register_blockbee()
    resp = client.get(
        "/v1/payments/webhook/blockbee",
        params={"token": "WRONG", "order_id": "o1", "expected": "1",
                "address_in": "whatever", "value_coin": "1", "pending": "0"},
    )
    assert resp.status_code == 401


# --- fail-safe: an underpayment does NOT grant time via the LIVE webhook path -----------------
# Adapter tests prove that parse maps a shortfall to UNDERPAID; the reconcile test proves that
# the reconcile branch does not activate. But the main prod path (route → _dispatch → apply_event →
# is_activating → on_payment_settled) with an UNDERPAID event was never exercised end-to-end. A
# regression that made UNDERPAID activating (S.UNDERPAID in _ACTIVATING / a broken is_activating)
# would pass the whole rest of the suite — that is direct loss of money / free time.

def test_underpaid_and_late_payment_are_not_activating():
    assert not is_activating(InvoiceStatus.UNDERPAID)
    assert not is_activating(InvoiceStatus.LATE_PAYMENT)
    assert is_activating(InvoiceStatus.PAID)
    assert is_activating(InvoiceStatus.OVERPAID)


def test_fake_underpaid_webhook_does_not_activate(client, session, price_plan):
    fake = FakeProvider(external_id="ext-under")
    registry.register(fake)
    res = start_purchase(
        session, price_plan=price_plan, method=PaymentMethod.MONERO, provider=fake, now=NOW
    )
    session.commit()
    account_id = res.account.id
    assert res.account.status == AccountStatus.EXPIRED  # freshly minted account with no time

    body = json.dumps({"external_id": "ext-under", "event": "underpaid"}).encode()
    resp = client.post("/v1/payments/webhook/fake", content=body)
    assert resp.status_code == 200

    session.refresh(res.account)
    assert res.account.status == AccountStatus.EXPIRED           # NOT activated
    assert res.invoice.status == InvoiceStatus.UNDERPAID
    assert session.query(Subscription).filter(Subscription.account_id == account_id).count() == 0


def test_blockbee_underpaid_webhook_does_not_activate(client, session, price_plan):
    seed = FakeProvider(name="seed", external_id="TAddrUnder")
    registry.register(seed)
    res = start_purchase(
        session, price_plan=price_plan, method=PaymentMethod.USDT_TRC20, provider=seed, now=NOW
    )
    session.commit()
    order_id = str(res.invoice.id)

    _register_blockbee()
    # paid 0.4 against an expected 0.5 (threshold = 0.5·0.99) → UNDERPAID, token is valid for (order_id, 0.5)
    resp = client.get(
        "/v1/payments/webhook/blockbee",
        params={
            "token": _bb_token(order_id, "0.5"), "order_id": order_id, "expected": "0.5",
            "address_in": "TAddrUnder", "txid_in": "beef", "confirmations": "25",
            "value_coin": "0.4", "pending": "0",
        },
    )
    assert resp.status_code == 200
    session.refresh(res.account)
    assert res.account.status == AccountStatus.EXPIRED
    assert res.invoice.status == InvoiceStatus.UNDERPAID
    assert session.query(Subscription).filter(
        Subscription.account_id == res.account.id
    ).count() == 0


# --- reconcile job AND webhook callback for the SAME invoice = exactly one crediting -------
# Both entry points (scheduler.invoice_reconcile and api.payments._dispatch) call on_payment_settled.
# In prod both can fire for the same payment (a lost-and-resent callback, or a callback during a
# reconcile pass). The protection is the paid_at guard + scrubbing external_id + a DB unique on
# subscription.invoice_id. The test runs both entry points in both orders and requires exactly 1 Subscription.

class _ReconcilableFake:
    """A blockbee-shaped provider: both .reconcile (poll) and webhook verify/parse — without network."""

    name = "blockbee"

    def create_invoice(self, req):
        raise NotImplementedError

    def get_status(self, external_id):
        return ProviderStatus(external_id, PaymentEvent.SEEN)

    def reconcile(self, *, order_id, coin, expected):
        return ProviderStatus(
            external_id="addr-race", event=PaymentEvent.SETTLED,
            amount_paid=Decimal("0.012"), confirmations=6,
        )

    def verify_webhook(self, headers, body):
        return True

    def parse_webhook(self, headers, body):
        data = json.loads(body or b"{}")
        return ProviderStatus(
            external_id=data.get("external_id", "addr-race"),
            event=PaymentEvent(data.get("event", "settled")),
            amount_paid=Decimal("0.012"),
        )


def _pending_race_invoice(session, price_plan):
    res = start_purchase(session, price_plan=price_plan, method=PaymentMethod.BTC_ONCHAIN, now=NOW)
    res.invoice.external_id = "addr-race"
    res.invoice.crypto_amount = Decimal("0.012")
    res.invoice.crypto_currency = "BTC"
    session.commit()
    return res


def test_reconcile_then_webhook_credits_once(client, session, price_plan):
    registry.register(_ReconcilableFake())
    res = _pending_race_invoice(session, price_plan)
    account_id = res.account.id

    reconcile_pending_invoices(session, now=NOW)   # entry point #1: reconcile
    session.commit()

    body = json.dumps({"external_id": "addr-race", "event": "settled"}).encode()
    client.post("/v1/payments/webhook/blockbee", content=body)  # entry point #2: late callback

    subs = session.query(Subscription).filter(Subscription.account_id == account_id).all()
    assert len(subs) == 1                     # exactly one crediting
    assert subs[0].days == price_plan.duration_days
    session.refresh(res.account)
    assert res.account.status == AccountStatus.ACTIVE


def test_webhook_then_reconcile_credits_once(client, session, price_plan):
    registry.register(_ReconcilableFake())
    res = _pending_race_invoice(session, price_plan)
    account_id = res.account.id

    body = json.dumps({"external_id": "addr-race", "event": "settled"}).encode()
    client.post("/v1/payments/webhook/blockbee", content=body)  # entry point #1: callback

    reconcile_pending_invoices(session, now=NOW)   # entry point #2: reconcile (invoice is no longer pending)
    session.commit()

    subs = session.query(Subscription).filter(Subscription.account_id == account_id).all()
    assert len(subs) == 1
    session.refresh(res.account)
    assert res.account.status == AccountStatus.ACTIVE
