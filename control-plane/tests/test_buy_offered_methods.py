"""POST /buy must refuse coins we do not sell.

Regression: PaymentMethod still holds MONERO and LIGHTNING (enum/DB history) and _PROVIDER_NAME
still routes them to the NOWPayments "gateway". The form offers only USDT/LTC/BTC, but nothing
enforced that on POST: the min-USD gate has no entry for those coins, so a hand-crafted
`method=monero` reached _provider_for, got the gateway, and minted a REAL invoice for a coin with
no payout address configured — the customer's money would go somewhere we cannot recover it from.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import cabinet
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models.enums import PaymentMethod
from app.payments import registry


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False
    get_settings().purchases_open = True

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    app.dependency_overrides.clear()
    registry.clear()


def test_unsold_coins_are_not_in_the_offered_set():
    assert PaymentMethod.MONERO not in cabinet._OFFERED_METHODS
    assert PaymentMethod.LIGHTNING not in cabinet._OFFERED_METHODS
    assert cabinet._OFFERED_METHODS == {
        PaymentMethod.USDT_TRC20,
        PaymentMethod.LITECOIN,
        PaymentMethod.BTC_ONCHAIN,
    }


def test_offered_set_matches_the_form_exactly():
    """The form is the contract — the gate must be derived from it, never a second hand-kept list."""
    assert cabinet._OFFERED_METHODS == {m for _v, _l, m in cabinet._METHOD_DEFS}


def test_every_offered_coin_has_a_min_usd_gate():
    """A coin on the form with no min gate is the exact hole monero fell through."""
    from app.services import min_tx

    for m in cabinet._OFFERED_METHODS:
        assert min_tx.min_usd_for(m) > 0, f"{m} is offered but has no BlockBee min_tx gate"


@pytest.mark.parametrize("coin", ["monero", "lightning"])
def test_post_buy_rejects_unsold_coin(client, price_plan, coin):
    """With the NOWPayments 'gateway' registered — exactly prod's fallback state — an unsold coin
    must still be refused before it can reach a provider and mint an invoice."""
    from tests._fakes import FakeProvider

    registry.register(FakeProvider(name="blockbee"))

    r = client.post(
        "/buy",
        data={"plan_code": price_plan.code, "method": coin},
        follow_redirects=False,
    )

    assert r.status_code == 303, f"{coin} must be refused, not routed to a provider"
    assert r.headers["location"] == "/buy"
