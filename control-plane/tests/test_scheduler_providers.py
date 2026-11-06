"""The scheduler process must register payment providers itself.

Regression for a production no-op that lasted weeks: build_providers() lived in app.main and ran at
that module's import time. The scheduler (`python -m app.scheduler.run`) never imports app.main, so
its registry stayed empty, and invoice_reconcile — the only recovery path for a payment whose
callback never arrived — hit KeyError and returned 0 in silence. Live check on prod showed
`registry.names() == []` inside the scheduler container while control-plane had blockbee registered.
"""

from __future__ import annotations

import logging

import pytest

from app.payments import registry
from app.scheduler import invoice_reconcile, run


def test_scheduler_main_builds_providers_before_looping(monkeypatch):
    built = []
    monkeypatch.setattr(run, "build_providers", lambda: built.append("built"))
    monkeypatch.setattr(run, "emailer_from_settings", lambda s: None)

    def _stop(*_a, **_kw):
        raise KeyboardInterrupt  # BaseException -> survives main()'s `except Exception` loop guard

    monkeypatch.setattr(run.SessionLocal, "__call__", _stop, raising=False)
    monkeypatch.setattr(run.time, "sleep", _stop)

    with pytest.raises(KeyboardInterrupt):
        run.main()

    assert built == ["built"], "scheduler must register providers, or reconcile silently does nothing"


def test_reconcile_without_provider_warns_instead_of_silent_zero(caplog):
    """The silence is what hid the outage: 'no providers' looked identical to 'nothing to do'."""
    registry.clear()
    with caplog.at_level(logging.WARNING, logger="invoice_reconcile"):
        moved = invoice_reconcile.reconcile_pending_invoices(session=None)

    assert moved == 0
    assert any("DISABLED" in r.message or "DISABLED" in r.getMessage() for r in caplog.records), (
        "a missing blockbee provider must be logged loudly — it means real payments cannot be rescued"
    )


def test_build_providers_registers_blockbee_from_settings(monkeypatch):
    """Guards the wiring the scheduler now depends on (and the compose env list behind it)."""
    from app.payments import bootstrap

    registry.clear()
    s = bootstrap.get_settings()
    monkeypatch.setattr(s, "blockbee_enabled", True, raising=False)
    monkeypatch.setattr(s, "blockbee_api_key", "test-key", raising=False)
    monkeypatch.setattr(s, "blockbee_callback_secret", "test-secret", raising=False)
    monkeypatch.setattr(s, "public_base_url", "https://example.test", raising=False)
    monkeypatch.setattr(s, "dev_fake_payments", False, raising=False)
    monkeypatch.setattr(bootstrap, "get_settings", lambda: s)

    bootstrap.build_providers()

    assert "blockbee" in registry.names()
    assert hasattr(registry.get("blockbee"), "reconcile"), "reconcile() is what the job calls"


def test_missing_providers_are_logged_at_warning_not_info(monkeypatch, caplog):
    """The scheduler container logs at WARNING. An info-level "providers: NONE" would be invisible
    — which is precisely how the empty registry hid for weeks. Absence must be an alarm."""
    registry.clear()
    monkeypatch.setattr(run, "build_providers", lambda: None)  # simulate the broken wiring
    monkeypatch.setattr(run, "emailer_from_settings", lambda s: None)

    def _stop(*_a, **_kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(run.time, "sleep", _stop)
    monkeypatch.setattr(run, "SessionLocal", _stop)

    with caplog.at_level(logging.WARNING, logger="scheduler"), pytest.raises(KeyboardInterrupt):
        run.main()

    assert any("DISABLED" in r.getMessage() for r in caplog.records), (
        "an empty registry must be visible at WARNING, or nobody ever learns recovery is off"
    )
