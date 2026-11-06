"""The default plan/method pair on /buy must be one the customer can actually pay.

Regression: both radios default to `loop.first`, and the two firsts contradicted each other. Plans
are ordered by sort_order (cheapest first = $5), while _METHOD_DEFS starts with USDT, gated at $12
by BlockBee's min-transfer. So a visitor who opened /buy and pressed the button without touching
anything got a 400 ("from $12") on the very first click, on the entry-level plan — the single most
common first interaction the funnel has. The card already lists which coins can pay a plan; the
form now preselects one of them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import cabinet
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import PricePlan


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False
    get_settings().purchases_open = True

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_ladder(session):
    """The real prod shape: cheapest first by sort_order."""
    session.add_all([
        PricePlan(code="1m", name="1 месяц", duration_days=30, price_fiat_cents=500,
                  fiat_currency="USD", device_limit=5, is_active=True, sort_order=1),
        PricePlan(code="3m", name="3 месяца", duration_days=90, price_fiat_cents=1350,
                  fiat_currency="USD", device_limit=5, is_active=True, sort_order=2),
    ])
    session.commit()


def test_default_method_clears_the_gate_for_every_plan(session):
    """The invariant: whatever we preselect, that plan can actually pay with it."""
    from app.services import min_tx

    _seed_ladder(session)
    plans = session.query(PricePlan).order_by(PricePlan.sort_order).all()
    for plan in plans:
        value = cabinet._default_method_for(plan)
        assert value is not None, f"no payable coin offered for {plan.code}"
        method = next(m for v, _l, m in cabinet._METHOD_DEFS if v == value)
        gate = min_tx.min_usd_for(method) * 100
        assert plan.price_fiat_cents >= gate, (
            f"{plan.code} (${plan.price_fiat_cents/100}) preselects {value}, gated at ${gate/100}"
        )


def test_cheapest_plan_does_not_preselect_an_unpayable_coin(session):
    """$5 cannot pay with USDT ($12 gate) — the pair that produced the guaranteed 400."""
    _seed_ladder(session)
    cheapest = session.query(PricePlan).order_by(PricePlan.sort_order).first()
    assert cheapest.price_fiat_cents == 500
    assert cabinet._default_method_for(cheapest) == "litecoin"


def test_buy_page_checks_a_payable_method(session, client):
    _seed_ladder(session)
    html = client.get("/buy").text
    # the radio that carries `checked` must be the payable one, not the first-listed USDT
    checked = [
        line for line in html.splitlines() if 'name="method"' in line and "checked" in line
    ]
    assert checked, "no method radio is preselected"
    assert any('value="litecoin"' in line for line in checked)
    assert not any('value="usdt_trc20"' in line for line in checked), (
        "USDT is gated at $12 and cannot pay the default $5 plan"
    )
