"""Payment provider interface. The FSM and billing do not depend on the coin."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.services.billing_fsm import PaymentEvent


@dataclass
class InvoiceRequest:
    order_id: str
    amount_fiat_cents: int
    fiat_currency: str
    coin: str  # "XMR" | "BTC" | "BTC-LN" | "LTC" | "USDT-TRC20"
    days_granted: int


@dataclass
class ProviderInvoice:
    external_id: str
    payment_address: str | None
    crypto_amount: Decimal | None
    crypto_currency: str
    exchange_rate: Decimal | None
    rate_expires_at: datetime | None
    confirmations_required: int
    # Hosted checkout (gateway payment page, e.g. OxaPay); None for address-based providers.
    checkout_url: str | None = None


@dataclass
class ProviderStatus:
    external_id: str
    event: PaymentEvent
    amount_paid: Decimal | None = None
    confirmations: int | None = None
    # On-chain transaction id this callback is about, when the provider reports per-transaction
    # (BlockBee does). The app accumulates by txid so a payment split across several transactions
    # counts as its SUM — assigning the last part is what used to leave a fully-paid invoice
    # UNDERPAID. None for providers that already report a cumulative amount.
    txid: str | None = None


class PaymentProvider(ABC):
    name: str

    @abstractmethod
    def create_invoice(self, req: InvoiceRequest) -> ProviderInvoice:
        ...

    @abstractmethod
    def get_status(self, external_id: str) -> ProviderStatus:
        ...

    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        """By default webhooks are not supported (poll-only providers)."""
        return False

    def parse_webhook(self, headers: dict, body: bytes) -> ProviderStatus:
        raise NotImplementedError(f"{self.name} has no webhook")
