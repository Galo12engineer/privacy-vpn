"""HEAD must be served wherever GET is.

FastAPI's APIRoute, unlike Starlette's Route, does not add HEAD to a GET route, so every
public page used to answer 405. bingbot and several AI fetchers issue HEAD to check
freshness before spending a GET, and a 405 reads as an error rather than "unchanged" —
the opposite of what a site that wants to be crawled needs.

The fix lives in app.main.HeadAsGet.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app

PUBLIC_PAGES = [
    "/",
    "/faq",
    "/support",
    "/guides",
    "/guides/linux",
    "/terms",
    "/aup",
    "/privacy",
    "/transparency",
    "/login",
]


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_head_is_not_405(client, path):
    assert client.head(path).status_code == 200


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_head_body_is_empty_and_headers_match_get(client, path):
    """HEAD is GET without a body: same status, same content-type, nothing in the body.

    The body is dropped by HeadAsGet itself rather than left to uvicorn, so the guarantee
    holds on every transport — including this TestClient, which never goes over the wire.
    """
    head, get = client.head(path), client.get(path)
    assert head.status_code == get.status_code
    assert head.headers.get("content-type") == get.headers.get("content-type")
    assert head.content == b""
    assert get.content, "sanity: the GET this is compared against must be non-empty"


def test_head_on_healthz(client):
    """Monitoring and uptime checkers hit /healthz with HEAD too."""
    assert client.head("/healthz").status_code == 200


def test_head_on_static_asset(client):
    """/static is a Mount, not an APIRoute — check the middleware covers it too."""
    r = client.head("/static/style.css")
    assert r.status_code == 200
    assert r.content == b""


def test_head_preserves_security_headers(client):
    """A HEAD response is still a response: the CSP/no-store guarantees must not be skipped."""
    r = client.head("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_post_only_route_still_rejects_head(client):
    """The rewrite must not turn HEAD into a skeleton key: a POST-only route stays 405."""
    assert client.head("/v1/nodes/register").status_code == 405


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
