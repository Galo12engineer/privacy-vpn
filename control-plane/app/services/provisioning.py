"""Peer provisioning: device limit, subscription activity, IPAM, revocation.

Pure domain logic (no networking). Dispatching commands to a node — node_client.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, Node, Peer
from app.models.enums import AccountStatus, NodeProtocol
from app.services import ipam, reality_profile
from app.services.billing import evaluate_status
from app.services.config_builder import PeerConfig, build_peer_config


class ProvisioningError(Exception):
    pass


class AccountNotActive(ProvisioningError):
    pass


class DeviceLimitReached(ProvisioningError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def active_peer_count(session: Session, account: Account) -> int:
    return session.execute(
        select(func.count())
        .select_from(Peer)
        .where(Peer.account_id == account.id)
    ).scalar_one()


def create_peer(
    session: Session,
    account: Account,
    node: Node,
    *,
    public_key: str,
    name: str | None = None,
    preshared_key: str | None = None,
    now: datetime | None = None,
    grace_days: int = 3,
) -> tuple[Peer, PeerConfig]:
    """Create a device: checks → IPAM → Peer → config (without the private key)."""
    if evaluate_status(account, now=now, grace_days=grace_days) == AccountStatus.EXPIRED:
        raise AccountNotActive("account has no active time")
    # TOCTOU: two concurrent /devices/add calls (double click) would otherwise both pass the
    # check and exceed device_limit. We lock the account row → check+insert are serialized
    # until commit.
    # (On SQLite FOR UPDATE is a no-op, but the tests are single-threaded; the lock works on
    # prod/Postgres.)
    session.execute(
        select(Account.id).where(Account.id == account.id).with_for_update()
    ).one()
    if active_peer_count(session, account) >= account.device_limit:
        raise DeviceLimitReached(f"device limit {account.device_limit} reached")

    lease = ipam.allocate(session, node)
    peer = Peer(
        account_id=account.id,
        node_id=node.id,
        ip_lease_id=lease.id,
        public_key=public_key,
        name=name,
        preshared_key=preshared_key,
    )
    session.add(peer)
    session.flush()
    cfg = build_peer_config(node, lease, preshared_key=preshared_key)
    return peer, cfg


def create_vless_peer(
    session: Session,
    account: Account,
    node: Node,
    *,
    name: str | None = None,
    now: datetime | None = None,
    grace_days: int = 3,
) -> tuple[Peer, dict]:
    """Create a VLESS device: the same checks (activity/limit), but WITHOUT IPAM and a WG key.

    The client is identified by vless_uuid (issued by the server) + an email tag for the xray api.
    Returns (peer, client={"uuid","email"})."""
    if evaluate_status(account, now=now, grace_days=grace_days) == AccountStatus.EXPIRED:
        raise AccountNotActive("account has no active time")
    # the same TOCTOU lock as in create_peer: the device limit is counted across all protocols
    session.execute(
        select(Account.id).where(Account.id == account.id).with_for_update()
    ).one()
    if active_peer_count(session, account) >= account.device_limit:
        raise DeviceLimitReached(f"device limit {account.device_limit} reached")

    peer = Peer(
        account_id=account.id,
        node_id=node.id,
        protocol_type=NodeProtocol.VLESS_REALITY,
        name=name,
    )
    session.add(peer)
    session.flush()  # peer.id → email tag
    client = reality_profile.generate_client(peer.id)
    peer.vless_uuid = client["uuid"]
    peer.vless_email = client["email"]
    session.flush()
    return peer, client


def remove_device(session: Session, peer: Peer) -> str:
    """Remove a device on user request: drop the peer + free the IP. Returns the pubkey for the agent."""
    pubkey = peer.public_key
    lease = peer.ip_lease
    session.delete(peer)
    session.flush()
    if lease is not None:
        ipam.release(session, lease)
    return pubkey


def revoke_account_peers(
    session: Session, account: Account, *, now: datetime | None = None
) -> list[tuple[uuid.UUID, str]]:
    """Drop all peers of the account (expired subscription, Phase 4).

    Returns [(node_id, public_key), ...] for dispatching the removal to the agents.
    """
    targets: list[tuple[uuid.UUID, str]] = []
    for peer in list(account.peers):
        targets.append((peer.node_id, peer.public_key))
        remove_device(session, peer)
    account.peers_revoked_at = now or _utcnow()
    return targets
