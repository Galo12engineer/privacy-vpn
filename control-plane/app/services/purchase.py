"""Time purchase (time-credit). The first payment mints an account-number; a top-up extends it.

Activation is idempotent: within the retention window the marker is invoice.paid_at; after the
scrub (retention=0 or deferred) it is safeguarded by account_id is None + the absence of
external_id (a replay will not find the invoice).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Account, Invoice, PricePlan
from app.models.enums import AccountStatus, InvoiceStatus, PaymentMethod
from app.payments.base import InvoiceRequest, PaymentProvider
from app.services import attribution
from app.services.account_number import generate_account_number, hash_account_number
from app.services.billing import apply_credit, scrub_invoice_linkage
from app.services.promo import discounted_cents

log = logging.getLogger("purchase")


class UnknownAccount(Exception):
    pass


@dataclass
class PurchaseResult:
    invoice: Invoice
    account: Account
    account_number: str | None  # plaintext — returned ONCE, at mint time
    is_new: bool


def _utcnow() -> datetime:
    return datetime.now(UTC)


_COIN = {
    PaymentMethod.MONERO: "XMR",
    PaymentMethod.BTC_ONCHAIN: "BTC",
    PaymentMethod.LIGHTNING: "BTC-LN",
    PaymentMethod.LITECOIN: "LTC",
    PaymentMethod.USDT_TRC20: "USDT-TRC20",
    PaymentMethod.CASH: "CASH",
}


def _confirmations_for(method: PaymentMethod, settings) -> int:
    return {
        PaymentMethod.MONERO: settings.xmr_confirmations_required,
        PaymentMethod.BTC_ONCHAIN: settings.btc_confirmations_required,
        PaymentMethod.LIGHTNING: 0,  # instant path
        PaymentMethod.LITECOIN: settings.ltc_confirmations_required,
        PaymentMethod.USDT_TRC20: settings.usdt_confirmations_required,
        PaymentMethod.CASH: 0,
    }[method]


def start_purchase(
    session: Session,
    *,
    price_plan: PricePlan,
    method: PaymentMethod,
    account_number: str | None = None,
    now: datetime | None = None,
    ttl_minutes: int | None = None,
    provider: PaymentProvider | None = None,
    discount_percent: int = 0,
    promo_code_id=None,
) -> PurchaseResult:
    """Create an invoice. account_number given → extension; otherwise → mint of a new account.

    discount_percent: a validated promo percent (the caller resolves and RESERVES the code — see
    services/promo). The discounted amount is computed ONCE here and used for both the Invoice row
    and the provider quote. That is the point of computing it in this function rather than passing
    two amounts in: the row and the crypto sum are the same number by construction, so the invoice
    can never say $11.48 while the customer is asked for $13.50 worth of coins.

    promo_code_id: which code's allowance this invoice is holding, so the scheduler can give it
    back if the invoice is never paid (promo.release). Payment-window linkage only — scrubbed with
    account_id.
    """
    now = now or _utcnow()
    settings = get_settings()
    ttl_minutes = ttl_minutes if ttl_minutes is not None else settings.invoice_ttl_minutes

    plaintext: str | None = None
    if account_number:
        h = hash_account_number(account_number, settings.account_number_pepper)
        account = session.execute(
            select(Account).where(Account.account_number_hash == h)
        ).scalar_one_or_none()
        if account is None:
            raise UnknownAccount("account number not found")
        is_new = False
    else:
        plaintext = generate_account_number()
        account = Account(
            account_number_hash=hash_account_number(plaintext, settings.account_number_pepper),
            status=AccountStatus.EXPIRED,
            device_limit=settings.default_device_limit,
        )
        session.add(account)
        session.flush()
        is_new = True

    discount_percent = max(0, discount_percent)
    amount_fiat_cents = discounted_cents(price_plan.price_fiat_cents, discount_percent)

    invoice = Invoice(
        account_id=account.id,
        price_plan_id=price_plan.id,
        method=method,
        status=InvoiceStatus.CREATED,
        days_granted=price_plan.duration_days,
        amount_fiat_cents=amount_fiat_cents,
        discount_percent=discount_percent,
        promo_code_id=promo_code_id,
        fiat_currency=price_plan.fiat_currency,
        confirmations_required=_confirmations_for(method, settings),
        expires_at=now + timedelta(minutes=ttl_minutes),
    )
    session.add(invoice)
    session.flush()

    if provider is not None:
        pinv = provider.create_invoice(
            InvoiceRequest(
                order_id=str(invoice.id),
                # the DISCOUNTED amount — the same integer that went into the row above
                amount_fiat_cents=amount_fiat_cents,
                fiat_currency=price_plan.fiat_currency,
                coin=_COIN[method],
                days_granted=price_plan.duration_days,
            )
        )
        invoice.external_id = pinv.external_id
        invoice.payment_address = pinv.payment_address
        invoice.checkout_url = pinv.checkout_url
        invoice.crypto_amount = pinv.crypto_amount
        invoice.crypto_currency = pinv.crypto_currency
        invoice.exchange_rate = pinv.exchange_rate
        invoice.rate_locked_at = now
        invoice.rate_expires_at = pinv.rate_expires_at or now + timedelta(
            minutes=settings.rate_lock_window_minutes
        )
        if pinv.confirmations_required:
            invoice.confirmations_required = pinv.confirmations_required

    return PurchaseResult(invoice=invoice, account=account, account_number=plaintext, is_new=is_new)


def on_payment_settled(
    session: Session,
    invoice: Invoice,
    *,
    now: datetime | None = None,
    grace_days: int | None = None,
    retention_minutes: int | None = None,
) -> bool:
    """Idempotent activation: credit the days, set paid_at, scrub the linkage.

    Idempotency guards: (1) paid_at (the marker within the retention window); (2) account_id is
    None — after the scrub paid_at is zeroed out, but the linkage is already severed → a repeated
    activation is impossible. True if applied right now.
    """
    if invoice.paid_at is not None:
        return False  # already activated within the retention window — a repeat webhook is safe

    now = now or _utcnow()
    settings = get_settings()
    retention_minutes = (
        retention_minutes if retention_minutes is not None else settings.invoice_link_retention_minutes
    )

    if invoice.account_id is None:
        return False  # linkage already scrubbed / no account — nothing to activate
    account = session.get(Account, invoice.account_id)
    if account is None:
        return False

    plan = session.get(PricePlan, invoice.price_plan_id) if invoice.price_plan_id else None
    apply_credit(
        session,
        account,
        days=invoice.days_granted,
        now=now,
        price_plan_code=plan.code if plan else None,
        invoice_id=invoice.id,
        device_limit=plan.device_limit if plan else None,
    )
    invoice.paid_at = now
    if invoice.status not in (InvoiceStatus.PAID, InvoiceStatus.OVERPAID):
        invoice.status = InvoiceStatus.PAID

    # Attribution + privacy: fire the conversion postback ONCE (only if a tracker is configured),
    # then ERASE click_id — we do not keep the ad-click↔account linkage forever (no-log). A
    # tracker failure (network/timeout) MUST NOT break activation → best-effort.
    if account.attribution_click_id:
        try:
            attribution.maybe_fire(session, account)
        except Exception as e:  # noqa: BLE001 — activation matters more than the postback
            log.warning("attribution postback failed: %s", type(e).__name__)
        account.attribution_click_id = None

    if retention_minutes <= 0:
        scrub_invoice_linkage(session, invoice, now=now)
    return True
