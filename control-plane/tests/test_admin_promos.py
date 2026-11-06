"""Promo-code CRUD in the operator console.

The validation tested here is not cosmetic. `min`/`max` on an <input> is a hint to a browser, not
a constraint on a POST — so every rule the form displays has to hold server-side too, or the first
hand-crafted request walks straight past it. The percent ceiling is the one with money behind it:
a 100% code mints a zero-amount invoice, which quotes as 0 coins and reads as fully paid on any
callback (see services/promo.MAX_PERCENT).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import PromoCode
from app.models.base import as_utc
from app.services import admin_auth


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


def _create(client, **over):
    data = {"code": "LAUNCH20", "percent_off": "20", "expires_at": "", "max_uses": "", "note": ""}
    data.update(over)
    return client.post("/admin/promos", data=data, follow_redirects=False)


def test_operator_creates_a_code(client, session):
    assert _create(client).status_code == 303
    row = session.query(PromoCode).one()
    assert (row.code, row.percent_off, row.is_active) == ("LAUNCH20", 20, True)
    assert row.max_uses is None and row.expires_at is None and row.used_count == 0


def test_the_code_is_stored_normalized(client, session):
    """So "launch-20" typed here and "LAUNCH 20" typed by the customer are the same code."""
    assert _create(client, code=" launch-20 ").status_code == 303
    assert session.query(PromoCode).one().code == "LAUNCH20"


def test_a_duplicate_is_refused_rather_than_shadowing_the_original(client, session):
    _create(client)
    resp = _create(client, code="launch 20")
    assert resp.status_code == 400
    assert "уже существует" in resp.text
    assert session.query(PromoCode).count() == 1


@pytest.mark.parametrize("percent", ["0", "100", "-5", "91"])
def test_a_percent_outside_the_allowed_range_is_refused(client, session, percent):
    resp = _create(client, percent_off=percent)
    assert resp.status_code == 400
    assert session.query(PromoCode).count() == 0


def test_a_code_shorter_than_the_minimum_is_refused(client, session):
    resp = _create(client, code="AB")
    assert resp.status_code == 400
    assert session.query(PromoCode).count() == 0


def test_expiry_covers_the_whole_chosen_day(client, session):
    """An operator writing "until the 31st" means through the 31st. Midnight would cut it a day
    short — a silent way to break codes already in customers' hands."""
    _create(client, expires_at="2026-08-31")
    exp = as_utc(session.query(PromoCode).one().expires_at)
    assert (exp.year, exp.month, exp.day, exp.hour) == (2026, 8, 31, 23)
    assert exp > datetime(2026, 8, 31, 12, tzinfo=UTC)


def test_a_garbled_limit_is_refused_not_silently_read_as_unlimited(client, session):
    resp = _create(client, max_uses="сто")
    assert resp.status_code == 400
    assert session.query(PromoCode).count() == 0


def test_an_overlong_note_is_truncated_not_500(client, session):
    """`maxlength` in the form is advisory; a hand-made POST walks past it. Over-long text is a
    Postgres-only error (VARCHAR(200)) that SQLite accepts silently — so it must be cut in code."""
    assert _create(client, note="д" * 500).status_code == 303
    assert len(session.query(PromoCode).one().note) == 200


def test_operator_switches_a_code_off(client, session):
    _create(client, max_uses="50")
    row = session.query(PromoCode).one()
    row.used_count = 7
    session.commit()

    resp = client.post(
        f"/admin/promos/{row.id}",
        data={"percent_off": "20", "expires_at": "", "max_uses": "50", "is_active": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    session.refresh(row)
    assert row.is_active is False
    # switching off keeps the record of what was already given away
    assert row.used_count == 7


def test_editing_cannot_raise_the_percent_past_the_ceiling(client, session):
    _create(client)
    row = session.query(PromoCode).one()
    resp = client.post(f"/admin/promos/{row.id}", data={"percent_off": "100"})
    assert resp.status_code == 400
    session.refresh(row)
    assert row.percent_off == 20


def test_the_promos_page_requires_a_logged_in_admin(session):
    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    anon = TestClient(app)
    assert anon.get("/admin/promos", follow_redirects=False).status_code == 303
    assert _create(anon).headers["location"].endswith("/admin/login")
    assert session.query(PromoCode).count() == 0
    app.dependency_overrides.clear()
