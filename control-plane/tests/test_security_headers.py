from fastapi.testclient import TestClient

from app.main import app
from app.security.headers import CSP

client = TestClient(app)


def test_dynamic_page_has_security_headers_and_no_store():
    r = client.get("/healthz")
    assert r.headers["content-security-policy"] == CSP
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    # the account number / invoice must not be cached
    assert r.headers["cache-control"] == "no-store"


def test_static_is_cacheable_but_still_hardened():
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert "max-age" in r.headers.get("cache-control", "")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_no_external_origins_allowed_in_csp():
    # connect/img/script — self only (+ data: for img); frames are forbidden
    assert "frame-ancestors 'none'" in CSP
    assert "object-src 'none'" in CSP
    assert "base-uri 'none'" in CSP
