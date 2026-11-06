from datetime import UTC, datetime, timedelta

from app.models import Account, Peer
from app.models.enums import AccountStatus
from app.scheduler.lifecycle import refresh_statuses, run_revocations
from app.services.provisioning import create_peer

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _account(session, suffix: str, expires_at) -> Account:
    a = Account(
        account_number_hash=f"lc{suffix}".ljust(64, "0"),
        status=AccountStatus.ACTIVE,
        expires_at=expires_at,
        device_limit=5,
    )
    session.add(a)
    session.commit()
    return a


def test_expired_account_peers_revoked_and_dispatched(session, node):
    a = _account(session, "1", NOW + timedelta(days=5))
    create_peer(session, a, node, public_key="PUB1", now=NOW)
    a.expires_at = NOW - timedelta(days=10)  # expired, the grace period is over
    session.commit()

    calls = []
    revoked = run_revocations(
        session, now=NOW, grace_days=3, dispatcher=lambda nid, pk: calls.append((nid, pk))
    )
    session.commit()

    assert len(revoked) == 1
    assert session.query(Peer).count() == 0  # the peer is revoked
    assert a.peers_revoked_at is not None
    assert a.status == AccountStatus.EXPIRED
    assert calls == [(node.id, "PUB1")]  # the removal reached the node


def test_grace_account_not_revoked(session, node):
    a = _account(session, "2", NOW + timedelta(days=1))
    create_peer(session, a, node, public_key="PUB1", now=NOW)
    a.expires_at = NOW - timedelta(days=1)  # grace (grace_days=3)
    session.commit()

    revoked = run_revocations(session, now=NOW, grace_days=3)
    assert revoked == []
    assert session.query(Peer).count() == 1
    refresh_statuses(session, now=NOW, grace_days=3)
    assert a.status == AccountStatus.GRACE


def test_revocation_is_idempotent(session, node):
    a = _account(session, "3", NOW + timedelta(days=1))
    create_peer(session, a, node, public_key="PUB1", now=NOW)
    a.expires_at = NOW - timedelta(days=10)
    session.commit()

    first = run_revocations(session, now=NOW, grace_days=3)
    session.commit()
    second = run_revocations(session, now=NOW, grace_days=3)
    assert len(first) == 1
    assert second == []  # a repeat run is a no-op


def test_refresh_keeps_active(session):
    a = _account(session, "4", NOW + timedelta(days=10))
    refresh_statuses(session, now=NOW, grace_days=3)
    assert a.status == AccountStatus.ACTIVE
