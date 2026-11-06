"""Tor-only access to the admin panel (CP_ADMIN_ONION_ONLY) + Secure-cookie over .onion.

Rationale: the admin panel must be reachable only over .onion. .onion goes into the
control-plane directly (torrc HiddenServicePort) with Host=<onion>; clearnet goes through
Caddy with the domain Host. The guard reads the flag at runtime → env toggle + restart
(an escape hatch against lockout).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.services import admin_auth

ONION = "privacyvpnexampleonionaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion"


@pytest.fixture()
def client(session):
    s = get_settings()
    prev_secure, prev_onion = s.cookie_secure, s.admin_onion_only
    s.cookie_secure = True  # prod mode (behind TLS)
    from app.webui import admin_flood_limiter, login_limiter

    login_limiter._buckets.clear()
    admin_flood_limiter._buckets.clear()

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    app.dependency_overrides.clear()
    # restore the global settings — otherwise the Secure-cookie/onion-guard leak into other tests
    s.cookie_secure, s.admin_onion_only = prev_secure, prev_onion


# genuine Tor = .onion Host WITHOUT the Caddy marker; clearnet (incl. spoof) = marker present
_TOR = {"host": ONION}
_CLEARNET_SPOOF = {"host": ONION, "x-tt-edge": "clearnet"}  # Host is forged, but the Caddy marker gives it away


def test_is_tor_helper():
    from unittest.mock import MagicMock

    from app.api.admin_ui import _is_tor

    def req(host, edge=None):
        m = MagicMock()
        m.url.hostname = host
        m.headers.get.return_value = edge
        return m

    assert _is_tor(req(ONION))  # genuine Tor
    assert not _is_tor(req(ONION, "clearnet"))  # Host spoof + Caddy marker → NOT Tor
    assert not _is_tor(req("privacyvpn.example.com"))  # ordinary clearnet
    assert not _is_tor(req(None))  # no Host


def test_clearnet_blocked_when_onion_only(client):
    get_settings().admin_onion_only = True
    # ordinary clearnet Host (default "testserver") → 404
    assert client.get("/admin/login").status_code == 404
    # genuine Tor (.onion, no marker) → the login form is visible
    assert client.get("/admin/login", headers=_TOR).status_code == 200


def test_host_spoof_with_edge_marker_blocked(client):
    # KEY POINT: Host is forged to the .onion, but the clearnet Caddy marker gives the spoof away → 404
    get_settings().admin_onion_only = True
    assert client.get("/admin/login", headers=_CLEARNET_SPOOF).status_code == 404


def test_clearnet_allowed_when_flag_off(client):
    # default (flag off) → the admin panel is reachable from clearnet too (backward compatibility)
    get_settings().admin_onion_only = False
    assert client.get("/admin/login").status_code == 200


def test_login_over_onion_sets_non_secure_cookie(client, session):
    admin_auth.create_admin(session, username="root", password="pw")
    session.commit()
    get_settings().admin_onion_only = True
    r = client.post(
        "/admin/login",
        data={"username": "root", "password": "pw", "totp": ""},
        headers=_TOR,
        follow_redirects=False,
    )
    assert r.status_code == 303
    cookie = r.headers.get("set-cookie", "")
    assert "admin_session=" in cookie
    # over .onion (http) — WITHOUT Secure, otherwise the browser drops the cookie and the session won't hold
    assert "Secure" not in cookie
    assert "HttpOnly" in cookie


def test_login_over_clearnet_keeps_secure_cookie(client, session):
    admin_auth.create_admin(session, username="root", password="pw")
    session.commit()
    # flag off → clearnet login is allowed, cookie with Secure (behind TLS). We send the marker like Caddy does.
    get_settings().admin_onion_only = False
    r = client.post(
        "/admin/login",
        data={"username": "root", "password": "pw", "totp": ""},
        headers={"x-tt-edge": "clearnet"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Secure" in r.headers.get("set-cookie", "")
