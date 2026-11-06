"""Scheduler job isolation (#6 static-audit): a failure in one job must not crash or block the others."""

from app.scheduler import run as sched


def test_run_once_isolates_failing_job(session, monkeypatch):
    calls = {"scrub": 0, "health": 0}

    def boom(*a, **k):
        raise RuntimeError("smtp down")

    def counting_scrub(*a, **k):
        calls["scrub"] += 1
        return 0

    def counting_health(*a, **k):
        calls["health"] += 1
        return []

    monkeypatch.setattr("app.scheduler.reminders.send_due_reminders", boom)
    monkeypatch.setattr(sched.invoice_scrub, "scrub_due_invoices", counting_scrub)
    monkeypatch.setattr(sched.node_health, "sweep_node_availability", counting_health)

    class _Emailer:  # truthy → the reminders job runs (and fails)
        pass

    res = sched.run_once(session, emailer=_Emailer())
    assert isinstance(res, dict)   # the exception was not propagated outwards
    assert res["reminders"] == 0   # the failed reminders job → 0
    assert calls["scrub"] == 1     # scrub ran DESPITE the reminders failure (isolation)
    assert calls["health"] == 1    # node_health too
