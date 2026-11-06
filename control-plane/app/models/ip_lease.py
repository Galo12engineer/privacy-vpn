from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utcnow

if TYPE_CHECKING:
    from app.models.node import Node
    from app.models.peer import Peer


class IpLease(Base):
    """An IP issued from the node's subnet. On revoke it is deleted entirely — no account↔IP history."""

    __tablename__ = "ip_lease"
    __table_args__ = (UniqueConstraint("node_id", "address", name="uq_lease_node_address"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("node.id", ondelete="CASCADE"), index=True
    )
    address: Mapped[str] = mapped_column(String(45))  # IPv4 host
    address_v6: Mapped[str | None] = mapped_column(String(45), nullable=True)
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    node: Mapped[Node] = relationship("Node", back_populates="leases")
    peer: Mapped[Peer | None] = relationship(
        "Peer", back_populates="ip_lease", uselist=False
    )
