"""Fiat→crypto rate source (for self-hosted Monero, where we compute the quota ourselves).

BTCPay/the gateway quote the amount themselves. A real provider hits an exchange API; here we
have the interface and a static implementation for tests/dev.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol


class RateProvider(Protocol):
    def quote(self, *, amount_fiat_cents: int, fiat_currency: str, coin: str) -> tuple[Decimal, Decimal]:
        """Return (crypto_amount, exchange_rate), where rate = price of 1 coin in fiat."""
        ...


class StaticRateProvider:
    """Rates are set explicitly (for tests/dev). prices: {coin: price_of_1_coin_in_fiat}."""

    def __init__(self, prices: dict[str, Decimal]):
        self.prices = prices

    def quote(self, *, amount_fiat_cents: int, fiat_currency: str, coin: str) -> tuple[Decimal, Decimal]:
        price = self.prices.get(coin)
        if not price:
            raise ValueError(f"no rate for {coin}")
        fiat = Decimal(amount_fiat_cents) / Decimal(100)
        crypto_amount = (fiat / price).quantize(Decimal("0.000000000001"))  # 12 decimal places
        return crypto_amount, price
