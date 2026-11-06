from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Integer, LargeBinary, String, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import AccountStatus

if TYPE_CHECKING:
    from app.models.peer import Peer
    from app.models.subscription import Subscription


class Account(Base, TimestampMixin):
    """Account. No PII. Identification — HMAC of the account-number (≥60 bits)."""

    __tablename__ = "account"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # HMAC(pepper, account_number) — deterministic, the plaintext is not stored
    account_number_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[AccountStatus] = mapped_column(
        SAEnum(AccountStatus, native_enum=False, length=16), default=AccountStatus.EXPIRED
    )
    # Source of truth for time. None = a new account with no paid time.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    device_limit: Mapped[int] = mapped_column(Integer, default=5)
    # opt-in email for reminders only; encrypted with Fernet; we minimize PII
    email_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    reminders_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    # when the scheduler wiped the peers after grace (Phase 4)
    peers_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # when the opt-in reminders were sent (so as not to send them again)
    reminder_3d_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminder_1d_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # attribution: click_id from the landing page (UTM), postback to the tracker on activation
    attribution_click_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attribution_fired: Mapped[bool] = mapped_column(Boolean, default=False)
    # #17: session epoch — inside the cabinet-token binding. Increment = "log out on all devices"
    # (a stolen cookie cannot be revoked any other way: login is by number, without a password).
    # Revocation without a store.
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    peers: Mapped[list[Peer]] = relationship(
        "Peer", back_populates="account", cascade="all, delete-orphan"
    )
    subscriptions: Mapped[list[Subscription]] = relationship(
        "Subscription", back_populates="account", cascade="all, delete-orphan"
    )
