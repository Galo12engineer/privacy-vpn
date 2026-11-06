from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Boolean, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PricePlan(Base, TimestampMixin):
    """Price plan. Data-driven from the admin console — no code changes needed (price, days, device limit)."""

    __tablename__ = "price_plan"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    duration_days: Mapped[int] = mapped_column(Integer)
    # fiat in minor units (cents) — no floating point
    price_fiat_cents: Mapped[int] = mapped_column(BigInteger)
    fiat_currency: Mapped[str] = mapped_column(String(3), default="USD")
    device_limit: Mapped[int] = mapped_column(Integer, default=5)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # "recommended" plan — highlighted on the landing page (anchor). Invariant: no more than
    # one featured at a time (admin_service.update_plan clears the flag on the others).
    featured: Mapped[bool] = mapped_column(Boolean, default=False)
