"""DEV-ONLY fake payment provider for locally testing a purchase (without real crypto).

Enabled by the CP_DEV_FAKE_PAYMENTS=true flag. NEVER in prod. `checkout_url` points at the dev route
`/dev/pay/{invoice_id}`; clicking it simulates a confirmed payment. Registered under the
name "blockbee" (BlockBee is the only provider) so cabinet._provider_for routes the sold coins to it.
"""
from __future__ import annotations

import json
from decimal import Decimal

from app.payments.base import InvoiceRequest, PaymentProvider, ProviderInvoice, ProviderStatus
from app.services.billing_fsm import PaymentEvent


class DevProvider(PaymentProvider):
    def __init__(self, name: str = "blockbee"):
        self.name = name

    def create_invoice(self, req: InvoiceRequest) -> ProviderInvoice:
        return ProviderInvoice(
            external_id=f"dev-{req.order_id}",
            payment_address=None,
            crypto_amount=Decimal("0.001"),
            crypto_currency=req.coin,
            exchange_rate=None,
            rate_expires_at=None,
            confirmations_required=0,
            # clicking this link (dev route) simulates a payment
            checkout_url=f"/dev/pay/{req.order_id}",
        )

    def get_status(self, external_id: str) -> ProviderStatus:
        return ProviderStatus(external_id=external_id, event=PaymentEvent.SEEN)

    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        return True

    def parse_webhook(self, headers: dict, body: bytes) -> ProviderStatus:
        d = json.loads(body or b"{}")
        return ProviderStatus(external_id=str(d.get("external_id", "")), event=PaymentEvent.SETTLED)
