from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Integer, String, Text, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import NodeProtocol, NodeStatus

if TYPE_CHECKING:
    from app.models.ip_lease import IpLease


class Node(Base, TimestampMixin):
    """VPN node. RAM-only. Stores aggregate metrics, NOT per-user."""

    __tablename__ = "node"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # wg server pubkey (base64, 44 characters); None while the node is provisioning
    public_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    endpoint_host: Mapped[str] = mapped_column(String(255))
    endpoint_port: Mapped[int] = mapped_column(Integer, default=51820)
    endpoint_port_alt: Mapped[int] = mapped_column(Integer, default=443)
    location_code: Mapped[str] = mapped_column(String(16))
    location_name: Mapped[str] = mapped_column(String(120))
    subnet_v4: Mapped[str] = mapped_column(String(43))  # CIDR, a dedicated subnet per node
    subnet_v6: Mapped[str | None] = mapped_column(String(43), nullable=True)
    dns_ip: Mapped[str] = mapped_column(String(45))  # per-node no-log resolver
    mtu: Mapped[int] = mapped_column(Integer, default=1420)
    # data transport: wireguard (default, backward compatibility) or amneziawg (obfuscation)
    protocol: Mapped[NodeProtocol] = mapped_column(
        SAEnum(NodeProtocol, native_enum=False, length=16), default=NodeProtocol.WIREGUARD
    )
    # AmneziaWG obfuscation profile — filled in ONLY when protocol=amneziawg.
    # S1,S2,H1-H4 must match between server↔client (see services/awg_profile.py).
    awg_jc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    awg_jmin: Mapped[int | None] = mapped_column(Integer, nullable=True)
    awg_jmax: Mapped[int | None] = mapped_column(Integer, nullable=True)
    awg_s1: Mapped[int | None] = mapped_column(Integer, nullable=True)
    awg_s2: Mapped[int | None] = mapped_column(Integer, nullable=True)
    awg_h1: Mapped[int | None] = mapped_column(Integer, nullable=True)
    awg_h2: Mapped[int | None] = mapped_column(Integer, nullable=True)
    awg_h3: Mapped[int | None] = mapped_column(Integer, nullable=True)
    awg_h4: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Reality server profile (JSON) — only when protocol=vless_reality. Fields:
    # {private_key, public_key, short_ids:[...], server_names:[...], dest, path, fingerprint}.
    # private_key is the server-side x25519 (Xray); public_key goes to the client as pbk. Port = endpoint_port.
    reality_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[NodeStatus] = mapped_column(
        SAEnum(NodeStatus, native_enum=False, length=16), default=NodeStatus.PROVISIONING
    )
    available_for_sale: Mapped[bool] = mapped_column(Boolean, default=False)
    # pinned self-signed cert of the agent's control channel (H1): PEM + its SHA-256.
    # Filled in at registration; node_client pins exactly this cert on dispatch.
    mtls_cert_fingerprint: Mapped[str | None] = mapped_column(String(95), nullable=True)
    agent_tls_cert: Mapped[str | None] = mapped_column(Text, nullable=True)
    # one-time auto-registration token (the hash is stored) and per-node HMAC secret CP↔agent
    registration_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # last aggregate snapshot (not a time series, not per-user)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    health_cpu_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    health_mem_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    health_peers: Mapped[int | None] = mapped_column(Integer, nullable=True)

    leases: Mapped[list[IpLease]] = relationship(
        "IpLease", back_populates="node", cascade="all, delete-orphan"
    )
