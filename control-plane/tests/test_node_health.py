from datetime import UTC, datetime, timedelta

from app.models import Node
from app.models.enums import NodeStatus
from app.scheduler.node_health import sweep_node_availability

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _node(session, name, last_health_at, *, status=NodeStatus.ACTIVE, available=True) -> Node:
    n = Node(
        name=name,
        endpoint_host=f"{name}.example.net",
        location_code="pa",
        location_name="Panama",
        subnet_v4="10.50.0.0/24",
        dns_ip="10.50.0.1",
        status=status,
        available_for_sale=available,
        last_health_at=last_health_at,
    )
    session.add(n)
    session.commit()
    return n


def test_stale_node_taken_off_sale(session):
    n = _node(session, "stale", NOW - timedelta(seconds=600))
    taken = sweep_node_availability(session, now=NOW, stale_seconds=150)
    session.commit()
    assert n.id in taken
    assert n.status == NodeStatus.DOWN
    assert n.available_for_sale is False


def test_fresh_node_stays(session):
    n = _node(session, "fresh", NOW - timedelta(seconds=30))
    taken = sweep_node_availability(session, now=NOW, stale_seconds=150)
    assert n.id not in taken
    assert n.status == NodeStatus.ACTIVE
    assert n.available_for_sale is True


def test_never_reported_taken_off(session):
    n = _node(session, "never", None)
    taken = sweep_node_availability(session, now=NOW, stale_seconds=150)
    assert n.id in taken
    assert n.status == NodeStatus.DOWN


# --- our own downtime is not evidence about a node ------------------------------------------------
#
# Nodes push health TO the control plane, so every second it is down produces silence by
# construction. The sweep used to read that silence as "the fleet is dead": coming back from a
# deploy longer than stale_seconds — which a rebuild routinely is — the first tick took EVERY node
# off sale. It healed on the next cycle (agents push every 30s), but in between /buy had no
# locations to sell and nothing said why. `listening_since` is when we could first observe.

def test_downtime_longer_than_the_threshold_does_not_condemn_the_fleet(session):
    """The exact prod scenario: CP rebuilt, nobody could report for 10 minutes."""
    restart = NOW  # the scheduler comes back up
    a = _node(session, "nl-1", NOW - timedelta(seconds=600))
    b = _node(session, "ro-1", NOW - timedelta(seconds=600))

    taken = sweep_node_availability(
        session, now=restart, stale_seconds=150, listening_since=restart
    )
    session.commit()

    assert taken == []
    assert [n.status for n in (a, b)] == [NodeStatus.ACTIVE, NodeStatus.ACTIVE]
    assert all(n.available_for_sale for n in (a, b)), "the fleet must stay sellable"


def test_a_node_still_silent_after_the_grace_is_condemned(session):
    """The grace delays the verdict by one window — it must not cancel it."""
    restart = NOW
    n = _node(session, "dead", NOW - timedelta(seconds=600))

    early = sweep_node_availability(
        session, now=restart + timedelta(seconds=149), stale_seconds=150, listening_since=restart
    )
    assert early == [], "inside the window the node still has time to report"

    late = sweep_node_availability(
        session, now=restart + timedelta(seconds=151), stale_seconds=150, listening_since=restart
    )
    session.commit()
    assert n.id in late, "a genuinely dead node must still be caught, just one cycle later"
    assert n.status == NodeStatus.DOWN


def test_a_node_that_reported_after_the_restart_is_judged_on_its_own_report(session):
    """listening_since must not become a blanket amnesty: once a node reports, its own clock rules."""
    restart = NOW
    reported = _node(session, "reporting", restart + timedelta(seconds=10))
    silent = _node(session, "silent", restart - timedelta(seconds=600))

    later = restart + timedelta(seconds=400)
    taken = sweep_node_availability(
        session, now=later, stale_seconds=150, listening_since=restart
    )
    session.commit()

    assert silent.id in taken
    assert reported.id in taken, "its own report is 390s old — stale on its own merits"


def test_without_a_start_marker_the_old_wall_clock_behaviour_is_kept(session):
    """Cron-style one-shot runs pass nothing: a fresh process there would never condemn anything."""
    n = _node(session, "cron", NOW - timedelta(seconds=600))

    taken = sweep_node_availability(session, now=NOW, stale_seconds=150)

    assert n.id in taken


def test_run_once_hands_the_start_marker_to_the_sweep(session):
    """The grace is worthless if the runner does not pass it — that is the wiring, not the rule."""
    from app.scheduler import run as sched

    seen = {}

    def _spy(session, **kw):
        seen.update(kw)
        return []

    import app.scheduler.node_health as nh
    original = nh.sweep_node_availability
    sched.node_health.sweep_node_availability = _spy
    try:
        sched.run_once(session, now=NOW, listening_since=NOW)
    finally:
        sched.node_health.sweep_node_availability = original

    assert seen.get("listening_since") == NOW


# --- alerting the operator and pulling a location off sale are different questions ----------------
#
# One threshold used to serve both. The node→CP path drops packets in transit: on 29.07 ro-1 went
# silent for over 150s fourteen times in five days while its tunnels never faltered (WireGuard is
# client↔node and never touches that path). Every one of those silences was eligible to erase
# Romania from /buy. The alert threshold stays tight; the sales one does not.

def test_silence_between_the_two_thresholds_keeps_the_location_sellable(session):
    from app.config import get_settings

    s = get_settings()
    assert s.node_offsale_seconds > s.node_stale_seconds, "the sales threshold must be the looser one"

    n = _node(session, "flaky", NOW - timedelta(seconds=400))  # past alerting (300s), short of off-sale (600s)
    taken = sweep_node_availability(session, now=NOW, stale_seconds=s.node_offsale_seconds)

    assert taken == []
    assert n.available_for_sale is True, "a few lost packets must not cost the location"


def test_run_once_sweeps_on_the_offsale_threshold_not_the_alert_one(session):
    """The wiring is the whole fix: a sweep handed node_stale_seconds is the old behaviour."""
    from app.config import get_settings
    from app.scheduler import run as sched

    seen = {}

    def _spy(session, **kw):
        seen.update(kw)
        return []

    import app.scheduler.node_health as nh
    original = nh.sweep_node_availability
    sched.node_health.sweep_node_availability = _spy
    try:
        sched.run_once(session, now=NOW, listening_since=NOW)
    finally:
        sched.node_health.sweep_node_availability = original

    s = get_settings()
    assert seen.get("stale_seconds") == s.node_offsale_seconds
    assert seen.get("stale_seconds") != s.node_stale_seconds
