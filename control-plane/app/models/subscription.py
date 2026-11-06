from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utcnow

if TYPE_CHECKING:
    from app.models.account import Account


class Subscription(Base):
    """Ledger of time grants (time-credit, stacks up).

    No PII. The link to the payment (invoice_id) is broken on scrub — payment↔account
    is not stored longer than activation.
    """

    __tablename__ = "subscription"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    # unique: DB backstop against double-crediting on the same invoice (webhook race).
    # NULL is allowed multiple times (the scrub breaks the invoice_id→None bridge for activated ones).
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invoice.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    price_plan_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    days: Mapped[int] = mapped_column(Integer)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship("Account", back_populates="subscriptions")
