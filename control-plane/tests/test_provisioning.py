from datetime import UTC, datetime, timedelta

import pytest

from app.models import Account, IpLease, Peer
from app.models.enums import AccountStatus
from app.services.ipam import allocate
from app.services.provisioning import (
    AccountNotActive,
    DeviceLimitReached,
    create_peer,
    remove_device,
    revoke_account_peers,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def test_create_peer_allocates_ip_and_persists(session, active_account, node):
    peer, cfg = create_peer(session, active_account, node, public_key="CLIENTPUB1", now=NOW)
    session.commit()
    assert peer.id is not None
    assert peer.ip_lease is not None
    assert cfg.address_v4.endswith("/32")
    assert session.query(Peer).count() == 1


def test_device_limit_enforced(session, active_account, node):
    active_account.device_limit = 1
    session.commit()
    create_peer(session, active_account, node, public_key="PUB1", now=NOW)
    with pytest.raises(DeviceLimitReached):
        create_peer(session, active_account, node, public_key="PUB2", now=NOW)


def test_expired_account_cannot_create_peer(session, node):
    a = Account(
        account_number_hash="exp".ljust(64, "0"),
        status=AccountStatus.EXPIRED,
        expires_at=NOW - timedelta(days=10),
    )
    session.add(a)
    session.commit()
    with pytest.raises(AccountNotActive):
        create_peer(session, a, node, public_key="PUB", now=NOW)


def test_remove_device_releases_ip(session, active_account, node):
    peer, _ = create_peer(session, active_account, node, public_key="PUB1", now=NOW)
    session.commit()
    assert session.query(IpLease).count() == 1
    pubkey = remove_device(session, peer)
    session.commit()
    assert pubkey == "PUB1"
    assert session.query(Peer).count() == 0
    assert session.query(IpLease).count() == 0  # the IP is released, no history kept


def test_revoke_account_peers_returns_targets(session, active_account, node):
    create_peer(session, active_account, node, public_key="PUB1", now=NOW)
    create_peer(session, active_account, node, public_key="PUB2", now=NOW)
    session.commit()
    targets = revoke_account_peers(session, active_account, now=NOW)
    session.commit()
    assert {pk for _, pk in targets} == {"PUB1", "PUB2"}
    assert session.query(Peer).count() == 0
    assert active_account.peers_revoked_at == NOW


def test_freed_ip_is_reused(session, active_account, node):
    peer, _ = create_peer(session, active_account, node, public_key="PUB1", now=NOW)
    first_ip = peer.ip_lease.address
    remove_device(session, peer)
    session.commit()
    lease = allocate(session, node)
    assert lease.address == first_ip  # the lowest free address was reused
