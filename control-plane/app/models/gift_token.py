from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class GiftToken(Base, TimestampMixin):
    """A prepaid access code, stored as an HMAC and never in plaintext.

    Opposite policy to PromoCode on purpose: a promo code is not a secret, a gift token is the
    goods, and in plaintext a database dump would be a stack of free subscriptions.

    Holds NO link to the account that redeemed it — `redeemed_at` is all one-shot redemption needs,
    and an account_id column would rebuild exactly the linkage this design exists to avoid.
    """

    __tablename__ = "gift_token"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    days: Mapped[int] = mapped_column(Integer)
    # Resolved at ISSUE time: editing the plan later must not change what an already-sold token
    # delivers. None = the server default at redemption.
    device_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # goes into the Subscription ledger, so a redeemed gift reads there like an ordinary purchase
    price_plan_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # deadline for REDEEMING the token, not the end of the subscription it grants
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set once, under a row lock, in the same transaction as the credit (services/gift.redeem).
    # Coarsened to the day: an exact time would point at the account created in that second.
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)  # operator memo
