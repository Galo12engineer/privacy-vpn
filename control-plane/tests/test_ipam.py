import pytest

from app.services.ipam import PoolExhaustedError, allocate, release


def test_allocate_sequential_skips_server_ip(session, node):
    l1 = allocate(session, node)
    l2 = allocate(session, node)
    assert l1.address == "10.7.0.2"  # .1 = the server, skipped
    assert l2.address == "10.7.0.3"


def test_reuse_lowest_after_release(session, node):
    allocate(session, node)  # .2
    l2 = allocate(session, node)  # .3
    allocate(session, node)  # .4
    release(session, l2)  # release .3
    l4 = allocate(session, node)
    assert l4.address == "10.7.0.3"  # the lowest free address is reused


def test_pool_exhaustion_raises(session, node):
    for _ in range(5):  # a /29 yields exactly 5 assignable addresses
        allocate(session, node)
    with pytest.raises(PoolExhaustedError):
        allocate(session, node)


def test_addresses_are_unique(session, node):
    addrs = {allocate(session, node).address for _ in range(5)}
    assert len(addrs) == 5


def test_v6_mapped_when_node_has_v6(session, node_v6):
    lease = allocate(session, node_v6)
    assert lease.address == "10.8.0.2"
    assert lease.address_v6 == "fd00:8::2"  # the same host offset inside the /64


def test_no_v6_when_node_lacks_v6(session, node):
    lease = allocate(session, node)
    assert lease.address_v6 is None


def test_release_leaves_no_history(session, node):
    from sqlalchemy import select

    from app.models import IpLease

    lease = allocate(session, node)
    release(session, lease)
    remaining = session.execute(
        select(IpLease).where(IpLease.node_id == node.id)
    ).scalars().all()
    assert remaining == []  # the row is deleted outright, no history kept
