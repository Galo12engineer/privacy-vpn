"""/healthz — readiness with a DB check (cluster E, ops monitoring)."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz_ok_when_db_reachable():
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # version comes from importlib.metadata (on prod = the current one from pyproject; locally depends on install).
    # We check that a string field is present, without tying ourselves to a specific value.
    assert isinstance(body["version"], str) and body["version"]


def test_healthz_503_when_db_unreachable(monkeypatch):
    """DB unreachable → 503 (otherwise monitoring/compose-healthcheck consider it "alive" while not serving)."""

    def _boom(*a, **k):
        raise RuntimeError("db down")

    # healthz does `from app.db import SessionLocal` inside the function → we patch the module attribute
    monkeypatch.setattr("app.db.SessionLocal", _boom)
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["status"] == "db_error"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
