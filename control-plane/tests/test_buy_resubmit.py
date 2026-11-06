"""Reloading the purchase result must not mint a second account.

POST /buy answers with a rendered page rather than a redirect, because the account number is shown
exactly once and the server cannot re-derive it (it stores only a hash) — so classic
POST-Redirect-GET is not available. That left F5 / Back+resend re-running the purchase: a second
Account, a second Invoice, and the session cookie silently swapped to the new account. The customer
then saves account #2 while his coins are paying invoice #1 — money in, access to the wrong account.

The reload carries the cookie the first response set, so a POST with a session + a live pending
invoice + no renew number is a resubmit, and is sent back to the invoice that already exists.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Account, Invoice
from app.payments import registry
from app.services.billing_fsm import PaymentEvent
from tests._fakes import FakeProvider


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


def _buy(client, **extra):
    return client.post(
        "/buy", data={"plan_code": "1m", "method": "litecoin", "account_number": "", **extra}
    )


def test_reload_of_the_result_page_does_not_mint_a_second_account(client, session, price_plan):
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))

    first = _buy(client)
    assert first.status_code == 200
    assert session.query(Account).count() == 1
    assert session.query(Invoice).count() == 1
    inv_id = session.query(Invoice).one().id

    # F5 on the POST result: the browser re-sends the same body, now carrying the session cookie
    again = client.post(
        "/buy",
        data={"plan_code": "1m", "method": "litecoin", "account_number": ""},
        follow_redirects=False,
    )

    assert again.status_code == 303, "a resubmit must bounce to the existing invoice, not buy again"
    assert again.headers["location"] == f"/invoice/{inv_id}"
    assert session.query(Account).count() == 1, "a reload must not mint a second account"
    assert session.query(Invoice).count() == 1, "nor a second invoice"


def test_resubmit_keeps_the_original_session(client, session, price_plan):
    """The silent cookie swap was the dangerous half: the customer kept browsing as account #2."""
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))

    _buy(client)
    cookie_after_first = client.cookies.get("session")
    account_id = session.query(Account).one().id

    client.post("/buy", data={"plan_code": "1m", "method": "litecoin", "account_number": ""},
                follow_redirects=False)

    assert client.cookies.get("session") == cookie_after_first, "the session must not be swapped"
    assert session.query(Account).one().id == account_id


def test_a_fresh_visitor_can_still_buy(client, session, price_plan):
    """The guard must not block the normal first purchase — there is no session yet."""
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))

    r = _buy(client)

    assert r.status_code == 200
    assert session.query(Account).count() == 1
    assert session.query(Invoice).count() == 1


def test_guard_ignores_an_expired_pending_invoice(client, session, price_plan):
    """A dead invoice must not trap the customer: he has to be able to start over."""
    from datetime import UTC, datetime, timedelta

    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _buy(client)
    inv = session.query(Invoice).one()
    inv.expires_at = datetime.now(UTC) - timedelta(minutes=1)  # the deadline has passed
    session.commit()

    r = _buy(client)

    assert r.status_code == 200, "with the old invoice dead, a new purchase must go through"
    assert session.query(Invoice).count() == 2
