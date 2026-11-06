from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Uuid,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import InvoiceStatus, PaymentMethod


class Invoice(Base, TimestampMixin):
    """Invoice. Full state machine — Phase 3. Here — the data structure and scrub fields."""

    __tablename__ = "invoice"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # nullable: the link to the account is scrubbed after activation (no-logs)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account.id", ondelete="SET NULL"), nullable=True, index=True
    )
    price_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_plan.id", ondelete="SET NULL"), nullable=True
    )
    method: Mapped[PaymentMethod] = mapped_column(
        SAEnum(PaymentMethod, native_enum=False, length=16)
    )
    status: Mapped[InvoiceStatus] = mapped_column(
        SAEnum(InvoiceStatus, native_enum=False, length=24),
        default=InvoiceStatus.CREATED,
        index=True,
    )
    # snapshot of the duration at the moment of purchase (the plan may change/be deleted)
    days_granted: Mapped[int] = mapped_column(Integer)
    amount_fiat_cents: Mapped[int] = mapped_column(BigInteger)
    # Percent-off actually applied at purchase time; 0 = no promo code. amount_fiat_cents is ALREADY
    # the discounted figure — this is the snapshot of WHY it differs from the plan's list price
    # (the plan is operator-editable, so the difference is not re-derivable later).
    # Survives the scrub — it is a small integer that links to no account (unlike address/amount/
    # txid, which do).
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    # Which code reserved a use for this invoice, so that use can be GIVEN BACK when the invoice
    # dies unpaid (services/promo.release). Without it a stranger could zero out any code's
    # max_uses for free, just by POSTing /buy and never paying — reproduced, it took 20 requests.
    # Lives exactly as long as account_id: both are the payment-window linkage, and
    # scrub_invoice_linkage nulls them together. So this stores nothing that outlives what the
    # invoice↔account link already stored — which is the whole reason a promo_redemption LEDGER
    # was rejected (see models/promo_code) but this column is fine.
    promo_code_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("promo_code.id", ondelete="SET NULL"), nullable=True
    )
    fiat_currency: Mapped[str] = mapped_column(String(3), default="USD")
    crypto_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    crypto_amount: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    crypto_amount_paid: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    # {txid: "amount"} of the on-chain transactions received for THIS invoice. Needed because the
    # gateway reports value PER TRANSACTION: without accumulating, a payment split across two txs
    # kept overwriting crypto_amount_paid with the last part, so the invoice stayed UNDERPAID and a
    # customer who had sent the full sum got nothing. The txid is the dedup key (a callback may
    # repeat for the same tx). Linkage data → scrubbed together with address/amount.
    crypto_txs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    rate_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rate_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # external id (BTCPay invoice / monero-subaddress index); scrubbed after activation
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    payment_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Hosted payment page (gateway checkout, e.g. OxaPay); None for address providers.
    checkout_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    confirmations_required: Mapped[int] = mapped_column(Integer, default=0)
    confirmations_seen: Mapped[int] = mapped_column(Integer, default=0)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scrubbed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
