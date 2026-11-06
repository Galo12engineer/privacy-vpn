"""HARD .onion invariant: pages MUST work over Tor — ZERO external resources
(CDN/fonts/analytics), system fonts, and functionality without JS. Previously not covered
by tests at all: a single <script src=cdn>/<link href=googlefonts> would break the .onion
site and leak the request into the clearnet (deanonymizing the Tor user). The test catches
such a regression in the rendered HTML.

A resource load (script src / link href / img src / css url()) must be relative or point
to .onion. A plain <a href> into the clearnet (e.g. www.wireguard.com/install) is a
navigation, not a resource load: it does not break the .onion render, so we do not check
hyperlinks.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Account, Node
from app.models.enums import AccountStatus, NodeStatus
from app.payments import registry
from app.services.account_number import generate_account_number, hash_account_number

# A resource load by the browser (as opposed to <a href> — a navigation).
_RESOURCE = re.compile(
    r"""<(?:script|link|img|source|iframe|audio|video|embed)\b[^>]*?\b(?:src|href)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_CSS_URL = re.compile(r"""url\(\s*['"]?([^)'"]+)""", re.IGNORECASE)
# `<link rel="canonical">` is metadata, not a fetch — the browser never requests it, exactly like
# an `<a href>`. It has to be absolute (a canonical URL with no host means nothing), so without
# this exemption the invariant below fires on a tag that loads nothing. What it points AT is still
# checked, just not here: test_seo_surface.test_onion_stays_same_origin proves that a request
# arriving on the onion gets the onion's own address back in canonical and og:image.
_NON_FETCHING_LINK = re.compile(
    r"""<link\b[^>]*\brel\s*=\s*["'](?:canonical|alternate)["'][^>]*>""", re.IGNORECASE
)
_ABSOLUTE = re.compile(r"^\s*https?://", re.IGNORECASE)
# Explicit markers of external networks/analytics/fonts — must not appear anywhere.
_FORBIDDEN = re.compile(
    r"googleapis|gstatic|fonts\.google|cdnjs|jsdelivr|unpkg|"
    r"google-analytics|googletagmanager|gtag\(|@font-face|cloudflare|bootstrapcdn",
    re.IGNORECASE,
)


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False
    get_settings().purchases_open = True
    from app.webui import login_limiter

    login_limiter._buckets.clear()

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    app.dependency_overrides.clear()
    registry.clear()


def _active_account(session) -> str:
    number = generate_account_number()
    session.add(
        Account(
            account_number_hash=hash_account_number(number, get_settings().account_number_pepper),
            status=AccountStatus.ACTIVE,
            expires_at=datetime.now(UTC) + timedelta(days=30),
            device_limit=5,
        )
    )
    session.commit()
    return number


def _sellable_node(session) -> Node:
    n = Node(
        name="on-1",
        endpoint_host="on1.example.net",
        location_code="on",
        location_name="Onionland",
        subnet_v4="10.9.0.0/29",
        dns_ip="10.9.0.1",
        status=NodeStatus.ACTIVE,
        available_for_sale=True,
        public_key="SRVPUB=",
    )
    session.add(n)
    session.commit()
    return n


def _assert_onion_safe(path: str, html: str) -> None:
    html = _NON_FETCHING_LINK.sub("", html)
    for url in _RESOURCE.findall(html) + _CSS_URL.findall(html):
        if url.startswith("data:") or url.startswith("#"):
            continue
        if _ABSOLUTE.match(url):
            assert ".onion" in url, f"{path}: external resource load to clearnet host: {url!r}"
    m = _FORBIDDEN.search(html)
    assert m is None, f"{path}: forbidden external/analytics/font marker: {m.group(0)!r}"


def test_public_pages_are_onion_safe(client):
    for path in ("/", "/login", "/buy", "/redeem", "/guides", "/transparency", "/privacy", "/aup"):
        r = client.get(path)
        assert r.status_code == 200, path
        _assert_onion_safe(path, r.text)


def test_authed_pages_are_onion_safe(client, session):
    number = _active_account(session)
    _sellable_node(session)
    assert client.post("/login", data={"account_number": number}).status_code == 200
    for path in ("/dashboard", "/devices/add"):
        r = client.get(path)
        assert r.status_code == 200, path
        _assert_onion_safe(path, r.text)


def test_core_flows_work_without_js(client, session):
    """Since JS may be disabled (Tor Browser safest), the forms must be in the HTML itself."""
    number = _active_account(session)
    _sellable_node(session)

    login_html = client.get("/login").text
    assert re.search(r'<form[^>]+action="/login"', login_html) or 'name="account_number"' in login_html

    assert client.post("/login", data={"account_number": number}).status_code == 200
    add_html = client.get("/devices/add").text
    # the server-side add-device form is present without any JS
    assert 'name="public_key"' in add_html
    assert 'name="node_id"' in add_html
