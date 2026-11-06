from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PromoCode(Base, TimestampMixin):
    """Percent-off promo code, created and switched off from the admin console.

    Deliberately holds NO row per redemption and no link to the invoices or accounts that used it —
    only an aggregate counter. A redemption ledger would recreate exactly the linkage
    `scrub_invoice_linkage` exists to destroy (code ↔ account ↔ time), and for a code handed to one
    person that linkage names them. What the money path actually needs from this table is a capacity
    check, and a counter answers that. The purchase itself keeps only `invoice.discount_percent` —
    a number, tied to nothing.

    `code` is stored NORMALIZED (upper-case, A-Z0-9 only — see services/promo.normalize), so the
    UNIQUE index really does prevent "launch20" and "LAUNCH-20" from being two different codes.
    """

    __tablename__ = "promo_code"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    percent_off: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # None = no deadline. Compared against "now" in UTC (see services/promo.check).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # None = unlimited. Counted at INVOICE CREATION, not at settlement — see services/promo.reserve
    # for why, and what that costs.
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    # operator memo (where the code was published) — never shown to the customer
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
