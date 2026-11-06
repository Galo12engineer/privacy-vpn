"""Language switcher (/lang, cookie) + server-side guides (/guides/{os}).

EN is the primary language (the default when there is no cookie), RU is used on explicit
choice. The guides are Jinja templates with _() (previously they were static content
outside the translation dictionary and stayed Russian on the EN site).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app

_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")
_GUIDES = ("windows", "macos", "ios", "android", "linux")


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_guides_hub_links_all_pages(client):
    resp = client.get("/guides")
    assert resp.status_code == 200
    for os_name in _GUIDES:
        assert f"/guides/{os_name}" in resp.text


def test_guide_pages_render_english_by_default(client):
    # EN is primary: without a cookie there must not be a single Cyrillic letter on the page
    for os_name in _GUIDES:
        resp = client.get(f"/guides/{os_name}")
        assert resp.status_code == 200
        assert "— setup" in resp.text
        assert not _CYRILLIC.search(resp.text), f"{os_name}: кириллица в EN-выдаче"


def test_guide_unknown_os_404(client):
    assert client.get("/guides/beos").status_code == 404


def test_buy_page_english_has_not_a_single_cyrillic_letter(client, price_plan):
    """Regression (prod bug, 2026-07-16): the EN /buy page showed Russian coin hints (the per-coin
    minimum and the "any amount" note). Reason: _METHODS was a module-level constant with
    hardcoded RU strings, so they never passed through the request translator — and
    check_i18n only scanned templates, so nothing caught it."""
    get_settings().purchases_open = True
    resp = client.get("/buy")
    assert resp.status_code == 200
    assert not _CYRILLIC.search(resp.text), "кириллица на английской /buy"


def test_buy_coin_hint_is_derived_from_the_enforced_minimum(client, price_plan):
    """The hint must come from min_tx, not from a second hardcoded copy: the label
    used to say "from $20" while the gate was an independent constant → they drifted apart."""
    from app.models.enums import PaymentMethod
    from app.services import min_tx

    get_settings().purchases_open = True
    resp = client.get("/buy")
    for method in (PaymentMethod.BTC_ONCHAIN, PaymentMethod.USDT_TRC20):
        assert f"from ${min_tx.min_usd_for(method)}" in resp.text


def test_lang_switch_sets_cookie_and_redirects(client):
    resp = client.get("/lang?to=ru&back=/guides", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/guides"
    assert "lang=ru" in resp.headers.get("set-cookie", "")


def test_lang_ru_renders_russian(client):
    client.get("/lang?to=ru")
    resp = client.get("/guides/linux")
    assert "Linux — подключение" in resp.text
    landing = client.get("/")
    assert "Тихо. Без логов." in landing.text


def test_lang_bad_value_falls_back_to_en(client):
    resp = client.get("/lang?to=de", follow_redirects=False)
    assert "lang=en" in resp.headers.get("set-cookie", "")
    # a garbage cookie set by hand → also EN, not a 500
    client.cookies.set("lang", "hax")
    assert "No noise. No logs." in client.get("/").text


@pytest.mark.parametrize(
    "back",
    ["https://evil.example", "//evil.example", "/\\evil", "javascript:alert(1)"],
)
def test_lang_back_open_redirect_guarded(client, back):
    resp = client.get("/lang", params={"to": "en", "back": back}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


# --- behind the login ----------------------------------------------------------------------------
#
# Every EN-Cyrillic assertion above stops at pages a visitor can reach without an account, so the
# page a PAYING customer actually lands on was never checked. That gap let an i18n cleanup delete
# the three subscription status words as unused — they were requested through a dict built inside
# the _() call, which the scanner cannot see — and nothing failed.

@pytest.mark.parametrize(
    ("status", "expires_in_days"),
    [("active", 30), ("grace", -1), ("expired", -30)],
    ids=["active", "grace", "expired"],
)
def test_dashboard_has_no_russian_in_english(client, session, status, expires_in_days):
    from datetime import UTC, datetime, timedelta

    from app.models import Account
    from app.models.enums import AccountStatus
    from app.services.account_number import generate_account_number, hash_account_number

    number = generate_account_number()
    session.add(
        Account(
            account_number_hash=hash_account_number(number, get_settings().account_number_pepper),
            status=AccountStatus.ACTIVE,
            expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
            device_limit=5,
        )
    )
    session.commit()

    client.post("/login", data={"account_number": number})
    resp = client.get("/dashboard")  # no lang cookie → EN, the default

    assert resp.status_code == 200
    leaked = sorted(set(_CYRILLIC.findall(resp.text)))
    assert not leaked, f"Russian on the EN dashboard ({status}): {''.join(leaked)}"
