from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utcnow
from app.models.enums import NodeProtocol

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.ip_lease import IpLease


class Peer(Base):
    """Device = wg peer. Each one = its own keypair + its own IP. The private key never reaches the server."""

    __tablename__ = "peer"
    __table_args__ = (
        # WG: pubkey unique per node (NULL for VLESS peers — NULLs do not conflict).
        UniqueConstraint("node_id", "public_key", name="uq_peer_node_pubkey"),
        # VLESS: UUID unique per node (NULL for WG peers).
        UniqueConstraint("node_id", "vless_uuid", name="uq_peer_node_vless_uuid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("node.id", ondelete="CASCADE"), index=True
    )
    # WG/AmneziaWG: an IP allocated from the node's subnet. Nullable — a VLESS peer has no
    # tunnel IP of its own (Xray NATs everyone through the node's IP).
    ip_lease_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ip_lease.id", ondelete="CASCADE"), unique=True, nullable=True
    )
    # WG/AmneziaWG: the client's pubkey ONLY (nullable — VLESS peers have no key)
    public_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # optional PSK (pluggable, off by default in the MVP)
    preshared_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # the device's transport (by default inherits the node's protocol — wireguard)
    protocol_type: Mapped[NodeProtocol] = mapped_column(
        SAEnum(NodeProtocol, native_enum=False, length=16), default=NodeProtocol.WIREGUARD
    )
    # VLESS identifiers (only when protocol_type=vless_reality). VLESS has no private
    # key: the client is identified by a UUID that the server ISSUES (unlike the WG pubkey,
    # which the client generates itself). vless_email — the tag for the xray api adu/rmu.
    vless_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    vless_email: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship("Account", back_populates="peers")
    ip_lease: Mapped[IpLease | None] = relationship("IpLease", back_populates="peer")
