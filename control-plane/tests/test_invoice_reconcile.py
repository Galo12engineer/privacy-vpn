"""#6 reconcile: polling BlockBee /logs picks up an invoice whose callback got lost."""

from datetime import UTC, datetime
from decimal import Decimal

from app.models.enums import AccountStatus, InvoiceStatus, PaymentMethod
from app.payments import registry
from app.payments.base import ProviderStatus
from app.scheduler.invoice_reconcile import reconcile_pending_invoices
from app.services.billing_fsm import PaymentEvent
from app.services.purchase import start_purchase

NOW = datetime(2026, 6, 1, tzinfo=UTC)


class _FakeReconciler:
    """A provider with .reconcile — like BlockBeeProvider, but without network."""

    name = "blockbee"

    def __init__(self, status: ProviderStatus | None):
        self._status = status
        self.calls: list[tuple[str, str, Decimal]] = []

    def create_invoice(self, req):  # not used in these tests
        raise NotImplementedError

    def get_status(self, external_id):
        raise NotImplementedError

    def reconcile(self, *, order_id, coin, expected):
        self.calls.append((order_id, coin, expected))
        return self._status


def _pending_invoice(session, price_plan, *, address="addr-1", amount=Decimal("0.012")):
    res = start_purchase(session, price_plan=price_plan, method=PaymentMethod.BTC_ONCHAIN, now=NOW)
    inv = res.invoice
    inv.external_id = address
    inv.crypto_amount = amount
    inv.crypto_currency = "BTC"
    session.commit()
    return res


def teardown_function():
    registry.clear()


def test_no_blockbee_registered_is_noop(session, price_plan):
    registry.clear()
    _pending_invoice(session, price_plan)
    assert reconcile_pending_invoices(session, now=NOW) == 0


def test_settled_poll_activates_account(session, price_plan):
    registry.clear()
    fake = _FakeReconciler(ProviderStatus(
        external_id="addr-1", event=PaymentEvent.SETTLED, amount_paid=Decimal("0.012"), confirmations=6,
    ))
    registry.register(fake)
    res = _pending_invoice(session, price_plan)

    n = reconcile_pending_invoices(session, now=NOW)
    session.commit()

    assert n == 1
    assert fake.calls == [(str(res.invoice.id), "BTC", Decimal("0.012"))]
    session.refresh(res.account)
    assert res.account.status == AccountStatus.ACTIVE
    assert res.invoice.status == InvoiceStatus.PAID  # paid_at is zeroed out by the scrub (retention=0 in the test config)


def test_seen_poll_does_not_change_anything(session, price_plan):
    registry.clear()
    fake = _FakeReconciler(ProviderStatus(external_id="addr-1", event=PaymentEvent.SEEN))
    registry.register(fake)
    res = _pending_invoice(session, price_plan)

    assert reconcile_pending_invoices(session, now=NOW) == 0
    assert res.invoice.status == InvoiceStatus.CREATED


def test_none_poll_result_is_ignored(session, price_plan):
    registry.clear()
    fake = _FakeReconciler(None)
    registry.register(fake)
    _pending_invoice(session, price_plan)
    assert reconcile_pending_invoices(session, now=NOW) == 0


def test_already_paid_invoice_not_polled(session, price_plan):
    registry.clear()
    fake = _FakeReconciler(ProviderStatus(external_id="addr-1", event=PaymentEvent.SETTLED))
    registry.register(fake)
    res = _pending_invoice(session, price_plan)
    res.invoice.status = InvoiceStatus.PAID
    session.commit()

    reconcile_pending_invoices(session, now=NOW)
    assert fake.calls == []  # PAID is outside _PENDING — not in the selection


def test_invoice_without_address_not_polled(session, price_plan):
    registry.clear()
    fake = _FakeReconciler(ProviderStatus(external_id="x", event=PaymentEvent.SETTLED))
    registry.register(fake)
    start_purchase(session, price_plan=price_plan, method=PaymentMethod.BTC_ONCHAIN, now=NOW)
    session.commit()

    reconcile_pending_invoices(session, now=NOW)
    assert fake.calls == []  # without external_id/crypto_amount — there is nothing to reconcile


def test_underpaid_poll_updates_status_without_activating(session, price_plan):
    registry.clear()
    fake = _FakeReconciler(ProviderStatus(
        external_id="addr-1", event=PaymentEvent.UNDERPAID, amount_paid=Decimal("0.006"),
    ))
    registry.register(fake)
    res = _pending_invoice(session, price_plan)

    n = reconcile_pending_invoices(session, now=NOW)
    session.commit()

    assert n == 1
    assert res.invoice.status == InvoiceStatus.UNDERPAID
    assert res.account.status == AccountStatus.EXPIRED  # not activated


def test_provider_without_reconcile_method_is_noop(session, price_plan):
    registry.clear()

    class _NoReconcile:
        name = "blockbee"

        def create_invoice(self, req):
            raise NotImplementedError

        def get_status(self, external_id):
            raise NotImplementedError

    registry.register(_NoReconcile())
    _pending_invoice(session, price_plan)
    assert reconcile_pending_invoices(session, now=NOW) == 0


def test_poll_exception_isolated_per_invoice(session, price_plan):
    registry.clear()

    class _Boom:
        name = "blockbee"
        calls = 0

        def create_invoice(self, req):
            raise NotImplementedError

        def get_status(self, external_id):
            raise NotImplementedError

        def reconcile(self, *, order_id, coin, expected):
            self.calls += 1
            raise RuntimeError("blockbee down")

    boom = _Boom()
    registry.register(boom)
    _pending_invoice(session, price_plan, address="addr-1")
    _pending_invoice(session, price_plan, address="addr-2")

    assert reconcile_pending_invoices(session, now=NOW) == 0
    assert boom.calls == 2  # a failure of one does not stop polling the rest
