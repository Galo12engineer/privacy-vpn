"""Wiring of payment providers into the registry, from settings.

This lives OUTSIDE app.main on purpose. It used to sit in app.main and run at that module's import
time — but the scheduler process (`python -m app.scheduler.run`) never imports app.main, so its
registry stayed EMPTY forever. `invoice_reconcile` — the entire safety net for a payment whose
callback never arrived — does `registry.get("blockbee")`, caught the KeyError and returned 0. It was
a permanent no-op on production: a lost callback meant the customer's money sat in the operator's
wallet with no subscription and nothing to rescue it.

Both entrypoints must call build_providers(): app.main (web) and app.scheduler.run (jobs).
NB: the process also needs the CP_BLOCKBEE_* / CP_PUBLIC_BASE_URL env — see docker-compose.yml,
where control-plane and scheduler each carry their own explicit `environment:` list.

BlockBee is the ONLY provider. The NOWPayments/OxaPay/BTCPay/Monero adapters were removed by
operator decision (2026-07-24): none of them served a live payment method — all three coins we
sell already routed to BlockBee — and dormant code that moves money is a liability, not a safety
net. There is deliberately NO fallback now: if BlockBee is unregistered, checkout fails loudly
instead of quietly minting invoices against a provider nobody has exercised in months.
"""

from __future__ import annotations

from app.config import get_settings
from app.payments import registry


def build_providers() -> None:
    """Register the configured providers. Secrets come from env/secret-store."""
    s = get_settings()
    if (
        s.blockbee_enabled
        and s.blockbee_api_key
        and s.blockbee_callback_secret
        and s.public_base_url
    ):
        from app.payments.blockbee import BlockBeeProvider

        # keys = coin strings from purchase._COIN
        bb_payouts = {}
        if s.blockbee_payout_btc:
            bb_payouts["BTC"] = s.blockbee_payout_btc
        if s.blockbee_payout_ltc:
            bb_payouts["LTC"] = s.blockbee_payout_ltc
        if s.blockbee_payout_usdt_trc20:
            bb_payouts["USDT-TRC20"] = s.blockbee_payout_usdt_trc20
        registry.register(
            BlockBeeProvider(
                api_key=s.blockbee_api_key,
                callback_secret=s.blockbee_callback_secret,
                payout_addresses=bb_payouts,
                callback_base_url=s.public_base_url,
                confirmations={
                    "BTC": s.btc_confirmations_required,
                    "LTC": s.ltc_confirmations_required,
                    "USDT-TRC20": s.usdt_confirmations_required,
                },
                underpaid_tolerance=s.payment_underpaid_tolerance,
                base_url=s.blockbee_base_url,
            )
        )
    if s.dev_fake_payments:
        # DEV-ONLY: registered under "blockbee" so cabinet._provider_for routes the sold coins to
        # it. It used to register as "oxapay" and win through a special case in _provider_for; with
        # the OxaPay adapter gone, taking BlockBee's own name is what keeps the dev flow working.
        from app.payments.dev import DevProvider

        registry.register(DevProvider(name="blockbee"))
