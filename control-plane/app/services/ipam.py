"""IPAM: a dedicated subnet per node, issuing the lowest free IP, reuse, exhaustion.

On revoke the lease is deleted entirely → there is no account↔IP history (no-logs).
Races are suppressed by UniqueConstraint(node_id, address) + retry inside a SAVEPOINT.
"""

import ipaddress

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import IpLease, Node


class PoolExhaustedError(Exception):
    """No free addresses are left in the node's subnet."""


def _allocated_addresses(session: Session, node: Node) -> set[ipaddress._BaseAddress]:
    rows = session.execute(
        select(IpLease.address).where(IpLease.node_id == node.id)
    ).scalars().all()
    return {ipaddress.ip_address(a) for a in rows}


def _pick_free_v4(node: Node, allocated: set) -> ipaddress.IPv4Address:
    net = ipaddress.ip_network(node.subnet_v4, strict=False)
    reserved = set(allocated)
    reserved.add(next(net.hosts()))  # .1 is reserved for the server/gateway
    for host in net.hosts():
        if host not in reserved:
            return host  # the lowest free one → deterministic reuse
    raise PoolExhaustedError(f"node {node.name} v4 pool exhausted")


def _map_v6(node: Node, v4_host: ipaddress.IPv4Address) -> str | None:
    """Deterministic v6: the same host-offset inside the node's /64. No separate pool."""
    if not node.subnet_v6:
        return None
    net4 = ipaddress.ip_network(node.subnet_v4, strict=False)
    net6 = ipaddress.ip_network(node.subnet_v6, strict=False)
    offset = int(v4_host) - int(net4.network_address)
    return str(net6.network_address + offset)


def allocate(session: Session, node: Node, *, max_retries: int = 5) -> IpLease:
    """Issue the lowest free IP. Idempotent with respect to races via retry."""
    last_exc: Exception | None = None
    for _ in range(max_retries):
        allocated = _allocated_addresses(session, node)
        host = _pick_free_v4(node, allocated)
        lease = IpLease(node_id=node.id, address=str(host), address_v6=_map_v6(node, host))
        try:
            with session.begin_nested():  # SAVEPOINT: roll back only this insert
                session.add(lease)
            return lease
        except IntegrityError as exc:  # the address was taken in a race — try the next one
            last_exc = exc
            continue
    raise PoolExhaustedError("could not allocate after retries") from last_exc


def release(session: Session, lease: IpLease) -> None:
    """Free the IP. We delete the row entirely — no history at all (no-logs)."""
    session.delete(lease)
    session.flush()
