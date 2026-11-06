from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Account, Invoice, Peer
from app.models.enums import AccountStatus, NodeStatus
from app.payments import registry
from app.services.account_number import generate_account_number, hash_account_number
from tests._fakes import FakeProvider


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False  # TestClient goes over http
    get_settings().purchases_open = True  # the default is now False (fail-safe); tests do sell
    from app.webui import login_limiter

    login_limiter._buckets.clear()

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    app.dependency_overrides.clear()
    registry.clear()


def _make_account(session, *, active=True) -> tuple[Account, str]:
    number = generate_account_number()
    a = Account(
        account_number_hash=hash_account_number(number, get_settings().account_number_pepper),
        status=AccountStatus.ACTIVE if active else AccountStatus.EXPIRED,
        expires_at=(datetime.now(UTC) + timedelta(days=30)) if active else None,
        device_limit=5,
    )
    session.add(a)
    session.commit()
    return a, number


def test_login_success_then_dashboard(client, session):
    _, number = _make_account(session)
    resp = client.post("/login", data={"account_number": number})
    assert resp.status_code == 200
    assert resp.url.path == "/dashboard"


def test_login_invalid_is_generic(client, session):
    _make_account(session)
    # correct format but a non-existent number → 401 (same as a wrong format: anti-enumeration)
    resp = client.post("/login", data={"account_number": "1234 5678 9012 3456 7890"})
    assert resp.status_code == 401


def test_login_rate_limited(client, session):
    for _ in range(5):
        client.post("/login", data={"account_number": "0000 0000 0000 0000 0001"})
    resp = client.post("/login", data={"account_number": "0000 0000 0000 0000 0001"})
    assert resp.status_code == 429


def test_login_rate_limit_is_per_account(client, session):
    # #4: the login bucket is per-number, NOT global. Exhausting number A's bucket does not block B.
    from app.webui import login_limiter

    login_limiter._buckets.clear()
    for _ in range(5):
        client.post("/login", data={"account_number": "0000 0000 0000 0000 000A"})
    a = client.post("/login", data={"account_number": "0000 0000 0000 0000 000A"})
    assert a.status_code == 429  # number A is exhausted
    b = client.post("/login", data={"account_number": "0000 0000 0000 0000 000B"})
    assert b.status_code != 429  # number B — an independent bucket (previously this would be 429)


def test_dashboard_requires_session(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert resp.url.path == "/login"  # redirect to login


def test_session_epoch_binding_revokes_old_cookie(client, session):
    # #17: "log out everywhere" (epoch++) invalidates a previously issued cabinet cookie
    a, number = _make_account(session)
    r = client.post("/login", data={"account_number": number})
    assert r.url.path == "/dashboard"
    assert client.cookies.get("session") is not None
    a.session_epoch += 1  # simulate logout-all from another device
    session.commit()
    r2 = client.get("/dashboard")
    assert r2.url.path == "/login"  # the old cookie is no longer valid


def test_logout_all_bumps_epoch(client, session):
    a, number = _make_account(session)
    client.post("/login", data={"account_number": number})
    before = a.session_epoch
    client.post("/logout-all")
    session.refresh(a)
    assert a.session_epoch == before + 1


def test_add_and_remove_device(client, session, node):
    _, number = _make_account(session)
    client.post("/login", data={"account_number": number})
    node.available_for_sale = True
    node.status = NodeStatus.ACTIVE
    node.public_key = "SERVER_PUBKEY="
    session.commit()

    resp = client.post(
        "/devices/add",
        data={"public_key": "CLIENT_PUBKEY", "node_id": str(node.id), "name": "laptop"},
    )
    assert resp.status_code == 200
    assert session.query(Peer).count() == 1

    peer = session.query(Peer).first()
    rem = client.post(f"/devices/{peer.id}/remove")
    assert rem.status_code == 200
    assert session.query(Peer).count() == 0


def test_devices_add_never_echoes_or_stores_private_key(client, session, node):
    """HARD invariant: the WG private key stays in the browser only. The server must neither accept,
    nor store, nor echo the private key. The test hits the HTTP boundary: we send an EXTRA
    private_key field and make sure it surfaces nowhere, and the config is served with a
    placeholder."""
    _, number = _make_account(session)
    client.post("/login", data={"account_number": number})
    node.available_for_sale = True
    node.status = NodeStatus.ACTIVE
    node.public_key = "SERVER_PUBKEY="
    session.commit()

    resp = client.post(
        "/devices/add",
        data={
            "public_key": "CLIENT_PUBKEY=",
            "node_id": str(node.id),
            "name": "laptop",
            "private_key": "LEAKME_PRIVATE_KEY=",  # a private key slipped in maliciously/by mistake
        },
    )
    assert resp.status_code == 200
    # 1) the server did not echo the private key back into the rendered config/page
    assert "LEAKME_PRIVATE_KEY=" not in resp.text
    # 2) the config contains a PLACEHOLDER for the private key (substituted in the browser),
    #    not the real key
    assert "__PRIV__" in resp.text or "INSERT_PRIVATE_KEY" in resp.text
    # 3) in the DB the peer holds only the public key; the private key is in no string field
    peer = session.query(Peer).one()
    assert peer.public_key == "CLIENT_PUBKEY="
    for value in vars(peer).values():
        assert not (isinstance(value, str) and "LEAKME" in value)


def test_device_add_dispatch_failure_rolls_back(client, session, node, monkeypatch):
    # D1: dispatch to the node fails → the peer is rolled back (does not stay dead), user sees 503
    _, number = _make_account(session)
    client.post("/login", data={"account_number": number})
    node.available_for_sale = True
    node.status = NodeStatus.ACTIVE
    node.public_key = "SRV="
    node.agent_secret = "secret"  # with a secret → the dispatch will run (and fail)
    session.commit()

    def boom(*a, **k):
        raise RuntimeError("node unreachable")

    monkeypatch.setattr("app.services.node_client.dispatch_add_peer", boom)
    resp = client.post(
        "/devices/add", data={"public_key": "CLIENT=", "node_id": str(node.id), "name": "x"}
    )
    assert resp.status_code == 503
    assert session.query(Peer).count() == 0  # the peer did not settle in the DB as dead


def test_buy_mints_account(client, session, price_plan):
    # LTC: a coin we actually sell, and the only one the $5 plan can be paid with (BTC/USDT sit
    # below BlockBee's min_tx). It reaches the fake through the documented fallback:
    # blockbee unregistered -> "gateway". This used to buy with `monero` — which is NOT on the
    # form and is now refused at POST; see tests/test_buy_offered_methods.py.
    registry.register(FakeProvider(name="blockbee", external_id="ext-buy"))
    before = session.query(Account).count()
    resp = client.post("/buy", data={"plan_code": "1m", "method": "litecoin", "account_number": ""})
    assert resp.status_code == 200
    assert session.query(Account).count() == before + 1
    assert session.query(Invoice).count() == 1


def test_buy_below_minimum_keeps_the_users_choice(client, session, price_plan):
    """Regression: the error banner used to describe a different coin than the form showed.

    The radios were hardcoded to `checked if loop.first`, so a failed POST re-rendered the form
    reset to the first plan/coin while the banner still talked about what the user had picked.
    The operator saw "This coin starts at $8" (BTC) above a form showing USDT — the banner looked
    like it was permanently stuck. Fix: echo the selection back."""
    # the $5 plan is below the BTC gate ($8) → error page, selection must survive
    resp = client.post("/buy", data={"plan_code": "1m", "method": "btc_onchain", "account_number": ""})
    assert resp.status_code == 400
    assert 'value="btc_onchain" checked' in resp.text.replace(" >", ">")
    assert "$8" in resp.text  # the banner names the gate the user actually hit


def test_buy_plan_card_shows_price_and_effective_monthly(client, session, price_plan):
    """The card must show the price prominently and, for multi-month plans, the per-month figure —
    otherwise the ladder's whole discount is invisible."""
    resp = client.get("/buy")
    assert resp.status_code == 200
    assert 'class="plan-price"' in resp.text
    assert "5.00 USD" in resp.text  # the fixture plan is 1 month / $5 → no "/mo" duplicate needed
    assert "/mo" not in resp.text


def test_buy_cheap_plan_declares_which_coins_can_pay_it(client, session, price_plan):
    """The $5 plan physically cannot be paid with BTC/USDT (BlockBee min_tx). Say it on the card
    instead of letting the user pick an impossible combination and only then erroring."""
    resp = client.get("/buy")
    assert "payable only with" in resp.text
    assert "LTC" in resp.text


def test_buy_rate_limited(client, session, price_plan):
    # B3: global ceiling on invoice creation → 429 once exhausted
    from app.webui import buy_limiter

    buy_limiter._buckets.clear()
    try:
        for _ in range(60):
            buy_limiter.allow("buy")  # exhaust the bucket
        resp = client.post("/buy", data={"plan_code": "1m", "method": "monero", "account_number": ""})
        assert resp.status_code == 429
    finally:
        buy_limiter._buckets.clear()


def test_purchases_closed_blocks_buy(client, session, price_plan):
    get_settings().purchases_open = False
    try:
        g = client.get("/buy")
        assert g.status_code == 200
        assert (
            "Purchases temporarily unavailable" in g.text
            or "Покупка временно недоступна" in g.text
        )
        before = session.query(Invoice).count()
        p = client.post("/buy", data={"plan_code": "1m", "method": "monero", "account_number": ""})
        assert p.status_code == 403
        assert session.query(Invoice).count() == before  # not a single invoice was created
    finally:
        get_settings().purchases_open = True  # restore for the remaining tests


def test_invoice_status_requires_ownership(client, session):
    # regression (IDOR): GET /invoice/{id} without an ownership guard would expose payment metadata
    # of any invoice to whoever knows the UUID. Now: anonymous → /login; someone else's invoice
    # → /dashboard.
    from app.models.enums import PaymentMethod

    a, number = _make_account(session)
    inv_a = Invoice(
        account_id=a.id, method=PaymentMethod.BTC_ONCHAIN, days_granted=30, amount_fiat_cents=500
    )
    b, _ = _make_account(session)
    inv_b = Invoice(
        account_id=b.id, method=PaymentMethod.BTC_ONCHAIN, days_granted=30, amount_fiat_cents=500
    )
    session.add_all([inv_a, inv_b])
    session.commit()

    # anonymous (no cabinet cookie) → redirect to login, the invoice is not rendered
    r = client.get(f"/invoice/{inv_a.id}")
    assert r.url.path == "/login"

    client.post("/login", data={"account_number": number})  # log in as A
    r = client.get(f"/invoice/{inv_a.id}")  # own invoice — rendered
    assert r.status_code == 200 and r.url.path == f"/invoice/{inv_a.id}"
    r = client.get(f"/invoice/{inv_b.id}")  # someone else's invoice — do NOT expose
    assert r.url.path == "/dashboard"
    r = client.get("/invoice/not-a-uuid")  # garbage id → redirect, not a 500
    assert r.status_code == 200 and r.url.path == "/dashboard"


def test_pages_render(client):
    assert client.get("/").status_code == 200
    assert client.get("/buy").status_code == 200
    assert client.get("/guides").status_code == 200
    r = client.get("/transparency")
    assert r.status_code == 200
    assert "no-log" in r.text.lower() or "no-logs" in r.text.lower()  # trust content is in place
    assert client.get("/aup").status_code == 200
    assert client.get("/privacy").status_code == 200
    assert client.get("/terms").status_code == 200


def test_legal_pages_render_english_body(client):
    """#7: with lang=en the body of /privacy and /aup is in English, with no Russian fallback."""
    r = client.get("/privacy?lang=en")
    assert r.status_code == 200
    assert "No connection logs" in r.text          # the translated body string
    assert "Никаких логов подключений" not in r.text  # the Russian source is absent
    a = client.get("/aup?lang=en")
    assert "honest no-logs VPN" in a.text
    assert "честный no-logs VPN" not in a.text
    t = client.get("/terms?lang=en")
    assert "you accept these Terms in full" in t.text
    assert "принимаете эти Условия целиком" not in t.text


def test_provider_for_routes_to_blockbee_and_has_no_fallback():
    """BlockBee is the only provider since the NOWPayments/OxaPay removal (2026-07-24).

    The absence of a fallback is the point of the test, not an omission: an unregistered provider
    must return None so /buy says "payments unavailable", rather than minting invoices against a
    second gateway whose credentials nobody has exercised in months.
    """
    from app.api.cabinet import _provider_for
    from app.models.enums import PaymentMethod

    registry.clear()
    try:
        # nothing registered → no provider, and emphatically no silent second gateway
        assert _provider_for(PaymentMethod.BTC_ONCHAIN) is None
        assert _provider_for(PaymentMethod.USDT_TRC20) is None
        assert _provider_for(PaymentMethod.LITECOIN) is None
        # BlockBee registered → every sold coin routes to it
        registry.register(FakeProvider(name="blockbee"))
        assert _provider_for(PaymentMethod.BTC_ONCHAIN).name == "blockbee"
        assert _provider_for(PaymentMethod.USDT_TRC20).name == "blockbee"
        assert _provider_for(PaymentMethod.LITECOIN).name == "blockbee"
        # MONERO is not sold and has no provider mapping at all
        assert _provider_for(PaymentMethod.MONERO) is None
    finally:
        registry.clear()
