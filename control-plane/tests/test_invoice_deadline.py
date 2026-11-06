"""The customer must be told when his coins have to arrive.

Regression: /buy advertised "the rate is locked for 15 minutes" — a window no code implements
(`rate_expires_at` is written in purchase.py and read nowhere). The real clock is
invoice.expires_at = created + invoice_ttl_minutes (60 on prod). The invoice page never mentioned
a deadline at all: it said "Awaiting payment. Rate is locked." Past expires_at the invoice goes
EXPIRED (terminal) and coins arriving later are credited to nobody, while the webhook still answers
*ok*. Funding a wallet from an exchange routinely takes longer than 60 minutes — so a silent
deadline quietly eats a real customer's money.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import cabinet
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Invoice
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


def test_pay_deadline_is_the_real_expiry_not_the_phantom_rate_lock():
    inv = Invoice(expires_at=datetime(2026, 7, 17, 12, 30, tzinfo=UTC))
    assert cabinet._pay_deadline(inv) == "17.07.2026 12:30 UTC"


def test_pay_deadline_survives_a_missing_expiry():
    assert cabinet._pay_deadline(Invoice(expires_at=None)) is None


def test_buy_page_states_the_real_window_and_never_the_15_minute_claim(client, price_plan):
    r = client.get("/buy?lang=en")
    body = r.text

    assert r.status_code == 200
    ttl = get_settings().invoice_ttl_minutes
    assert f"within {ttl} minutes" in body, "the enforced window must be stated on /buy"
    assert "15 minutes" not in body, "the phantom 15-minute rate lock must be gone"
    assert "rate is locked" not in body.lower()


def test_created_invoice_shows_the_deadline(client, session, price_plan):
    from app.services.billing_fsm import PaymentEvent
    from tests._fakes import FakeProvider

    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    r = client.post("/buy", data={"plan_code": "1m", "method": "litecoin", "account_number": ""})

    assert r.status_code == 200
    inv = session.query(Invoice).one()
    assert inv.expires_at is not None, "purchase must set a deadline at all"
    # the absolute stamp the customer is told, e.g. "17.07.2026 12:30 UTC"
    assert re.search(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2} UTC", r.text), (
        "a created invoice must show WHEN the coins have to arrive"
    )


def test_deadline_matches_the_configured_ttl(client, session, price_plan):
    from app.services.billing_fsm import PaymentEvent
    from tests._fakes import FakeProvider

    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    before = datetime.now(UTC)
    client.post("/buy", data={"plan_code": "1m", "method": "litecoin", "account_number": ""})

    inv = session.query(Invoice).one()
    expected = before + timedelta(minutes=get_settings().invoice_ttl_minutes)
    delta = abs((cabinet.as_utc(inv.expires_at) - expected).total_seconds())
    assert delta < 60, "the advertised window and the enforced clock must be the same number"


def test_new_account_page_warns_that_the_number_vanishes_on_refresh(client, session, price_plan):
    """The page offers "↻ Refresh status", which GETs /invoice/{id} and re-renders with
    account_number=None — wiping the only credential. The server cannot re-show it (it keeps only
    a hash), so the warning must name that exact trap, not just "before you close the tab"."""
    from app.services.billing_fsm import PaymentEvent
    from tests._fakes import FakeProvider as FP

    registry.register(FP(name="blockbee", event=PaymentEvent.SEEN))
    r = client.post("/buy?lang=en", data={"plan_code": "1m", "method": "litecoin", "account_number": ""})

    assert r.status_code == 200
    assert "shown once" in r.text
    assert "Refresh status" in r.text, "the trap is this very control"


def test_refresh_status_really_does_drop_the_number(client, session, price_plan):
    """Documents the behaviour the warning is about — so nobody 'fixes' the warning away."""
    from app.services.billing_fsm import PaymentEvent
    from tests._fakes import FakeProvider

    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    post = client.post("/buy", data={"plan_code": "1m", "method": "litecoin", "account_number": ""})
    inv = session.query(Invoice).one()

    again = client.get(f"/invoice/{inv.id}")

    assert again.status_code == 200
    assert "acct" in post.text, "the number is shown on the POST result"
    assert "acct" not in again.text, "and is gone on refresh — the server has only the hash"
