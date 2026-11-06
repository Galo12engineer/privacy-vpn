"""Issuing gift tokens from the operator console.

The console is the only place a plaintext token ever exists, and it exists there for one render.
So the tests here are mostly about what must NOT happen to it: no redirect (a 303 parks a batch of
live credentials in the URL, i.e. in history and in every proxy log), no log line, no second
appearance in the listing. The validation tests exist because `min`/`max` on an <input> is a hint
to a browser, not a constraint on a POST.
"""

from __future__ import annotations

import logging
import re

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import GiftToken, PricePlan
from app.services import admin_auth, gift


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False
    get_settings().email_encryption_key = Fernet.generate_key().decode()
    from app.webui import admin_flood_limiter, login_limiter

    login_limiter._buckets.clear()
    admin_flood_limiter._buckets.clear()

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    c = TestClient(app)
    admin_auth.create_admin(session, username="root", password="pw")
    session.commit()
    c.post("/admin/login", data={"username": "root", "password": "pw", "totp": ""})
    yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def plan(session):
    p = PricePlan(
        code="1m", name="1 месяц", duration_days=30, price_fiat_cents=500,
        fiat_currency="USD", device_limit=5, is_active=True,
    )
    session.add(p)
    session.commit()
    return p


def _issue(client, **over):
    data = {"plan_id": "", "custom_days": "30", "count": "3", "expires_at": "", "note": ""}
    data.update(over)
    return client.post("/admin/gifts", data=data, follow_redirects=False)


# A token as the page prints it. Matched against the whole response rather than line by line: what
# matters is that the string is somewhere in the body, not how the markup wraps it.
_TOKEN = re.compile(r"TT-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}")


def _tokens(resp) -> list[str]:
    return _TOKEN.findall(resp.text)


def test_the_batch_is_printed_once_in_the_body_and_never_redirected(client, session, caplog):
    with caplog.at_level(logging.DEBUG):
        resp = _issue(client, count="3")

    assert resp.status_code == 200, "a 303 would put the tokens in a URL"
    assert "location" not in {k.lower() for k in resp.headers}
    assert session.query(GiftToken).count() == 3

    shown = _tokens(resp)
    assert len(shown) == 3 and len(set(shown)) == 3

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "issued 3 token(s)" in logged, "the giveaway itself must be logged"
    for token in shown:
        assert token not in logged and gift.normalize(token) not in logged


def test_a_printed_token_actually_works(client, session):
    """Proves the strings on the operator's screen are the tokens the rows were built from —
    the one thing that cannot be checked afterwards, because only the HMAC survives."""
    resp = _issue(client, count="1", custom_days="30")
    (token,) = _tokens(resp)

    row = gift.lookup(session, token)

    assert row.days == 30


def test_the_listing_never_shows_a_token(client, session):
    tokens = _tokens(_issue(client, count="2"))

    page = client.get("/admin/gifts")

    assert len(tokens) == 2
    assert all(t not in page.text for t in tokens)
    assert _tokens(page) == [], "the listing must contain nothing token-shaped at all"


def test_a_plan_fills_in_days_devices_and_the_ledger_code(client, session, plan):
    assert _issue(client, plan_id=str(plan.id), custom_days="", count="1").status_code == 200

    row = session.query(GiftToken).one()
    assert (row.days, row.device_limit, row.price_plan_code) == (30, 5, "1m")


def test_custom_days_beat_the_plan(client, session, plan):
    """Same rule as the manual grant: a filled-in number means "I mean this number"."""
    assert _issue(client, plan_id=str(plan.id), custom_days="7", count="1").status_code == 200

    assert session.query(GiftToken).one().days == 7


def test_nothing_is_issued_without_a_term(client, session):
    resp = _issue(client, plan_id="", custom_days="", count="1")

    assert resp.status_code == 400
    assert session.query(GiftToken).count() == 0


@pytest.mark.parametrize("count", ["0", str(gift.MAX_BATCH + 1), "-1", "abc", "١٢"])
def test_the_batch_size_is_enforced_server_side(client, session, count):
    """The form says 1..MAX_BATCH; the POST must too. "١٢" is Arabic-Indic for 12 — `isdigit()`
    accepts it and `int()` does not, which is a 500 on a form that hands out paid time."""
    resp = _issue(client, count=count)

    assert resp.status_code == 400
    assert session.query(GiftToken).count() == 0


def test_a_malformed_expiry_is_refused_rather_than_ignored(client, session):
    """Silently dropping it would issue tokens with no deadline at all — the opposite of the
    operator's intent, on the one field that limits the damage of a leaked batch."""
    resp = _issue(client, expires_at="31.12.2026")

    assert resp.status_code == 400
    assert session.query(GiftToken).count() == 0


def test_switching_a_token_off_stops_it_being_redeemed(client, session):
    (token,) = _tokens(_issue(client, count="1"))
    row = session.query(GiftToken).one()

    toggle = client.post(
        f"/admin/gifts/{row.id}", data={"is_active": "0"}, follow_redirects=False
    )

    assert toggle.status_code == 303  # no secret in this one, so a redirect is fine
    session.refresh(row)
    assert row.is_active is False
    with pytest.raises(gift.GiftError) as e:
        gift.lookup(session, token)
    assert e.value.reason == "disabled"


def test_the_page_is_closed_to_anyone_without_an_admin_session(session):
    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    try:
        anon = TestClient(app)
        assert anon.get("/admin/gifts", follow_redirects=False).status_code == 303
        assert anon.post(
            "/admin/gifts", data={"custom_days": "30", "count": "1"}, follow_redirects=False
        ).status_code == 303
        assert session.query(GiftToken).count() == 0
    finally:
        app.dependency_overrides.clear()
