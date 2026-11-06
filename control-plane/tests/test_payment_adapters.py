import hashlib
import hmac
import json
from decimal import Decimal

import httpx

from app.payments.base import InvoiceRequest
from app.payments.blockbee import BlockBeeProvider
from app.services.billing_fsm import PaymentEvent

# The NOWPayments/OxaPay/BTCPay/Monero adapters were removed on 2026-07-24 (BlockBee is the only
# provider — see app/payments/bootstrap.py), and their tests went with them. Everything below
# covers BlockBee.


# --- BlockBee -----------------------------------------------------------------

def _bb_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _blockbee(client=None, **kw) -> BlockBeeProvider:
    defaults = dict(
        api_key="bb-key",
        callback_secret="sekret-nonce",
        payout_addresses={"USDT-TRC20": "TOperatorUSDT", "BTC": "bc1qoperator"},
        callback_base_url="https://privacyvpn.example.com",
        confirmations={"USDT-TRC20": 20, "BTC": 2},
        client=client,
    )
    defaults.update(kw)
    return BlockBeeProvider(**defaults)


def test_blockbee_create_returns_address_in():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/convert/"):
            seen["convert"] = dict(request.url.params)
            return httpx.Response(
                200, json={"status": "success", "value_coin": "0.012", "exchange_rate": "100"}
            )
        if path.endswith("/create/"):
            seen["create"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "address_in": "TPayHere123",
                    "address_out": "TOperatorUSDT",
                    "callback_url": request.url.params.get("callback"),
                },
            )
        return httpx.Response(404, json={"status": "error", "error": "bad endpoint"})

    p = _blockbee(_bb_client(handler))
    inv = p.create_invoice(
        InvoiceRequest(
            order_id="o1", amount_fiat_cents=1300, fiat_currency="USD",
            coin="USDT-TRC20", days_granted=30,
        )
    )
    assert inv.external_id == "TPayHere123"
    assert inv.payment_address == "TPayHere123"
    assert inv.crypto_amount == Decimal("0.012")
    assert inv.exchange_rate == Decimal("100")
    assert inv.confirmations_required == 20
    # our own payout address went into create; the callback carries our webhook + a per-invoice TOKEN
    # (not the master secret) + order_id + expected
    cb = seen["create"]["callback"]
    assert seen["create"]["address"] == "TOperatorUSDT"
    assert "/v1/payments/webhook/blockbee" in cb
    assert "order_id=o1" in cb
    assert "expected=0.012" in cb
    assert f"token={_bb_token('o1', '0.012')}" in cb
    assert "sekret-nonce" not in cb  # master secret NOT in the URL


def test_blockbee_create_uses_usdt_tron_path():
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/convert/"):
            return httpx.Response(200, json={"status": "success", "value_coin": "0.5"})
        return httpx.Response(200, json={"status": "success", "address_in": "TX"})

    p = _blockbee(_bb_client(handler))
    p.create_invoice(
        InvoiceRequest(order_id="o1", amount_fiat_cents=1300, fiat_currency="USD",
                       coin="USDT-TRC20", days_granted=30)
    )
    assert "/trc20/usdt/convert/" in paths
    assert "/trc20/usdt/create/" in paths


def _bb_token(order_id: str, expected: str, secret: str = "sekret-nonce") -> str:
    return hmac.new(secret.encode(), f"{order_id}:{expected}".encode(), hashlib.sha256).hexdigest()


def _bb_callback(**over) -> bytes:
    order_id = over.pop("order_id", "o1")
    expected = over.pop("expected", "0.012")
    params = {
        "order_id": order_id,
        "expected": expected,
        "token": over.pop("token", _bb_token(order_id, expected)),
        "address_in": "TPayHere123",
        "txid_in": "abc123",
        "confirmations": "25",
        "value_coin": "0.012",
        "pending": "0",
    }
    params.update(over)
    return json.dumps(params).encode()


def test_blockbee_callback_settled():
    p = _blockbee()
    body = _bb_callback()
    assert p.verify_webhook({}, body) is True
    st = p.parse_webhook({}, body)
    assert st.external_id == "TPayHere123"
    assert st.event == PaymentEvent.SETTLED
    assert st.amount_paid == Decimal("0.012")
    assert st.confirmations == 25


def test_blockbee_callback_underpaid():
    p = _blockbee()
    st = p.parse_webhook({}, _bb_callback(value_coin="0.006"))
    assert st.event == PaymentEvent.UNDERPAID
    assert st.amount_paid == Decimal("0.006")


def test_blockbee_callback_pending_is_seen():
    p = _blockbee()
    st = p.parse_webhook({}, _bb_callback(pending="1"))
    assert st.event == PaymentEvent.SEEN


def test_blockbee_tolerance_absorbs_tiny_shortfall():
    # 1% tolerance: 0.0119 against 0.012 (-0.83%) -> still SETTLED
    p = _blockbee()
    st = p.parse_webhook({}, _bb_callback(value_coin="0.0119"))
    assert st.event == PaymentEvent.SETTLED


def test_blockbee_verify_rejects_bad_token():
    p = _blockbee()
    assert p.verify_webhook({}, _bb_callback(token="attacker")) is False
    assert p.verify_webhook({}, _bb_callback(token="")) is False
    assert p.verify_webhook({}, b"not-json") is False
    # the master secret as a token does not pass (token = HMAC(secret, order_id:expected), not the secret itself)
    assert p.verify_webhook({}, _bb_callback(token="sekret-nonce")) is False


def test_blockbee_verify_rejects_tampered_expected():
    # an attacker takes a valid token (for expected=0.012) and substitutes expected=0,
    # in order to push through any value as SETTLED -> the token covers expected -> rejected.
    p = _blockbee()
    forged = _bb_callback(expected="0", token=_bb_token("o1", "0.012"))
    assert p.verify_webhook({}, forged) is False


def test_blockbee_create_rejects_bad_quote():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/convert/"):
            return httpx.Response(200, json={"status": "success", "value_coin": "0"})
        return httpx.Response(200, json={"status": "success", "address_in": "TX"})

    p = _blockbee(_bb_client(handler))
    try:
        p.create_invoice(
            InvoiceRequest(order_id="o1", amount_fiat_cents=1300, fiat_currency="USD",
                           coin="USDT-TRC20", days_granted=30)
        )
        assert False, "expected ValueError on zero quote"
    except ValueError:
        pass


def test_blockbee_get_sanitizes_http_error():
    # the apikey goes into the query -> the httpx exception text MUST NOT leak (no URL/key)
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"status": "error", "error": "rate limited"})

    p = _blockbee(_bb_client(handler))
    try:
        p.create_invoice(
            InvoiceRequest(order_id="o1", amount_fiat_cents=1300, fiat_currency="USD",
                           coin="USDT-TRC20", days_granted=30)
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert "bb-key" not in str(e)
        assert "apikey" not in str(e)
        assert "429" in str(e)


# --- BlockBee #6 reconcile (poll /logs) -----------------------------------------

def _bb_logs_handler(callbacks, *, address_in="TPayHere123", seen_callback=None):
    if seen_callback is None:
        seen_callback = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/logs/"):
            seen_callback["callback"] = request.url.params.get("callback")
            return httpx.Response(
                200, json={"status": "success", "address_in": address_in, "callbacks": callbacks}
            )
        return httpx.Response(404, json={"status": "error", "error": "bad endpoint"})
    return handler


def test_blockbee_reconcile_settled_from_logs():
    seen = {}
    handler = _bb_logs_handler(
        [{"result": "done", "value_coin": "0.012", "confirmations": 20, "txid_in": "tx1"}],
        seen_callback=seen,
    )
    p = _blockbee(_bb_client(handler))
    st = p.reconcile(order_id="o1", coin="USDT-TRC20", expected=Decimal("0.012"))
    assert st is not None
    assert st.event == PaymentEvent.SETTLED
    assert st.amount_paid == Decimal("0.012")
    assert st.confirmations == 20
    assert st.external_id == "TPayHere123"
    # the reconstructed callback matches the one that was sent at create_invoice
    assert f"token={_bb_token('o1', '0.012')}" in seen["callback"]


def test_blockbee_reconcile_underpaid_from_logs():
    handler = _bb_logs_handler([{"result": "done", "value_coin": "0.006", "confirmations": 20}])
    p = _blockbee(_bb_client(handler))
    st = p.reconcile(order_id="o1", coin="USDT-TRC20", expected=Decimal("0.012"))
    assert st.event == PaymentEvent.UNDERPAID


def test_blockbee_reconcile_pending_result_is_seen():
    handler = _bb_logs_handler([{"result": "pending", "value_coin": "0.012", "confirmations": 0}])
    p = _blockbee(_bb_client(handler))
    st = p.reconcile(order_id="o1", coin="USDT-TRC20", expected=Decimal("0.012"))
    assert st.event == PaymentEvent.SEEN


def test_blockbee_reconcile_no_callbacks_returns_none():
    handler = _bb_logs_handler([])
    p = _blockbee(_bb_client(handler))
    assert p.reconcile(order_id="o1", coin="USDT-TRC20", expected=Decimal("0.012")) is None


def test_blockbee_reconcile_picks_most_advanced_entry():
    # several updates for the same tx: pending -> sent -> done -- we take "done"
    handler = _bb_logs_handler([
        {"result": "pending", "value_coin": "0.012", "confirmations": 0},
        {"result": "done", "value_coin": "0.012", "confirmations": 20},
        {"result": "sent", "value_coin": "0.012", "confirmations": 5},
    ])
    p = _blockbee(_bb_client(handler))
    st = p.reconcile(order_id="o1", coin="USDT-TRC20", expected=Decimal("0.012"))
    assert st.event == PaymentEvent.SETTLED
    assert st.confirmations == 20


def test_blockbee_reconcile_unknown_coin_returns_none():
    p = _blockbee()
    assert p.reconcile(order_id="o1", coin="XMR", expected=Decimal("1")) is None


def test_blockbee_reconcile_defensive_on_malformed_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "callbacks": "not-a-list"})

    p = _blockbee(_bb_client(handler))
    assert p.reconcile(order_id="o1", coin="USDT-TRC20", expected=Decimal("0.012")) is None


def test_blockbee_reconcile_http_error_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status": "error", "error": "not found"})

    p = _blockbee(_bb_client(handler))
    assert p.reconcile(order_id="o1", coin="USDT-TRC20", expected=Decimal("0.012")) is None
