"""Fake payment provider for FSM/purchase/webhook tests."""

from __future__ import annotations

import json
from decimal import Decimal

from app.payments.base import InvoiceRequest, PaymentProvider, ProviderInvoice, ProviderStatus
from app.services.billing_fsm import PaymentEvent


class FakeProvider(PaymentProvider):
    def __init__(
        self,
        *,
        name: str = "fake",
        event: PaymentEvent = PaymentEvent.SETTLED,
        external_id: str = "ext-1",
    ):
        self.name = name
        self.event = event
        self.external_id = external_id
        self.created: list[InvoiceRequest] = []

    def create_invoice(self, req: InvoiceRequest) -> ProviderInvoice:
        self.created.append(req)
        return ProviderInvoice(
            external_id=self.external_id,
            payment_address="FAKEADDR",
            crypto_amount=Decimal("0.5"),
            crypto_currency="XMR",
            exchange_rate=Decimal("100"),
            rate_expires_at=None,
            confirmations_required=0,
        )

    def get_status(self, external_id: str) -> ProviderStatus:
        return ProviderStatus(external_id, self.event)

    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        return True

    def parse_webhook(self, headers: dict, body: bytes) -> ProviderStatus:
        data = json.loads(body) if body else {}
        event = PaymentEvent(data.get("event", self.event))
        return ProviderStatus(
            external_id=data.get("external_id", self.external_id),
            event=event,
            confirmations=data.get("confirmations"),
        )
