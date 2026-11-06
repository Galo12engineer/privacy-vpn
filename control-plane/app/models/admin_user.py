from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Integer, LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AdminUser(Base, TimestampMixin):
    """Admin. Password (argon2) + optional TOTP secret (encrypted). 2FA logic — Phase 5."""

    __tablename__ = "admin_user"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # #23: last accepted TOTP counter → the code is single-use (cannot be replayed within the ~90s window)
    last_totp_counter: Mapped[int | None] = mapped_column(Integer, nullable=True)
