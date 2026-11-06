"""Registry of payment providers by name. Populated from settings (app.main) or in tests."""

from __future__ import annotations

from app.payments.base import PaymentProvider

_PROVIDERS: dict[str, PaymentProvider] = {}


def register(provider: PaymentProvider) -> None:
    _PROVIDERS[provider.name] = provider


def get(name: str) -> PaymentProvider:
    if name not in _PROVIDERS:
        raise KeyError(f"unknown payment provider: {name}")
    return _PROVIDERS[name]


def names() -> list[str]:
    return sorted(_PROVIDERS)


def clear() -> None:
    _PROVIDERS.clear()
