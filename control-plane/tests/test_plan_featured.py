"""PricePlan.featured flag (the "recommended" plan — the anchor on the landing page)."""
from __future__ import annotations

from app.services.admin_service import create_plan, update_plan


def _mk(session, code, days, cents):
    return create_plan(session, code=code, name=code, duration_days=days, price_fiat_cents=cents)


def test_featured_defaults_false(session):
    p = _mk(session, "1m", 30, 500)
    session.flush()
    assert p.featured is False


def test_single_featured_invariant(session):
    a = _mk(session, "3m", 90, 1350)
    b = _mk(session, "6m", 180, 2500)
    session.flush()

    update_plan(session, a, featured=True)
    assert a.featured is True
    assert b.featured is False

    # turn the recommendation on for another one → it is cleared on the first (no more than one featured)
    update_plan(session, b, featured=True)
    assert b.featured is True
    assert a.featured is False


def test_update_plan_can_unset_featured(session):
    a = _mk(session, "6m", 180, 2500)
    session.flush()
    update_plan(session, a, featured=True)
    assert a.featured is True
    update_plan(session, a, featured=False)
    assert a.featured is False
