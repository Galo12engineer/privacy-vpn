"""Per-coin minimum payable amount in USD — the gate that keeps a customer's coins from getting
stuck below the gateway's forwarding threshold.

BlockBee will not forward a payment under its `min_tx`; below that the money sits at the deposit
address and is not recoverable by us or by the customer. The threshold is fixed IN THE COIN, so its
USD value floats with the rate — a table of constants is a snapshot that silently rots.

So: a static floor (measured, with buffer) combined with the gateway's LIVE figure, and we take the
HIGHER of the two. That direction is the whole safety argument — a live value can only ever tighten
the gate. Every failure path (no BlockBee registered, API down, junk response) falls back to the
static floor, i.e. to exactly the behaviour that shipped before this module existed.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from decimal import Decimal

from app.models.enums import PaymentMethod
from app.payments import registry

log = logging.getLogger("min_tx")

# Measured floor per coin (USD), 2026-07-16, from BlockBee's live min_tx:
#   BTC   0.00008 BTC @ $64,104 ~= $5.13  -> floor $8
#   USDT  10 USDT     @ $0.999  ~= $9.99  -> floor $12
#   LTC   0.002 LTC   @ $44.42  ~= $0.09  -> floor $1
# These carry deliberate buffer (the $8 BTC floor holds up to ~$100k/BTC). Do NOT lower them: they
# are what protects the gate when the live lookup is unavailable.
_FLOOR_USD: dict[PaymentMethod, int] = {
    PaymentMethod.LITECOIN: 1,
    PaymentMethod.USDT_TRC20: 12,
    PaymentMethod.BTC_ONCHAIN: 8,
}

# Coin strings as the providers know them — the authority is purchase._COIN. Only the three methods
# we actually sell appear here; anything else falls through to "no gate", matching the old
# `_METHOD_MIN_USD.get(method)` → None behaviour.
_COIN: dict[PaymentMethod, str] = {
    PaymentMethod.LITECOIN: "LTC",
    PaymentMethod.USDT_TRC20: "USDT-TRC20",
    PaymentMethod.BTC_ONCHAIN: "BTC",
}

_TTL_OK = 900      # 15 min — a fresh figure; rates do not move enough to matter faster than this
_TTL_FAIL = 300    # 5 min — negative caching, so an outage cannot put an API call on every /buy
_cache: dict[str, tuple[float, int | None]] = {}
_lock = threading.Lock()
_in_flight: set[str] = set()


def reset_cache() -> None:
    """Drop the memo (tests; and an operator escape hatch after re-measuring the floors)."""
    with _lock:
        _cache.clear()
        _in_flight.clear()


def _fetch(coin: str) -> None:
    """Refresh one coin's cached figure. Runs OFF the request path — see _live_usd."""
    value: int | None = None
    try:
        provider = registry.get("blockbee")
        ask = getattr(provider, "min_transaction_usd", None)
        if ask is not None:
            usd = ask(coin)
            if usd is not None and usd > 0:
                # ceil: rounding a threshold DOWN would re-open the very hole this closes
                value = int(math.ceil(Decimal(usd)))
    except KeyError:
        pass  # BlockBee not registered in this process (dev fake only) — floor it is
    except Exception as e:  # noqa: BLE001 — a gateway hiccup must not take down /buy
        log.warning("min_tx lookup failed for %s: %s", coin, type(e).__name__)
    finally:
        with _lock:
            _cache[coin] = (time.time() + (_TTL_OK if value is not None else _TTL_FAIL), value)
            _in_flight.discard(coin)


def _live_usd(coin: str, *, now: float) -> int | None:
    """Last known BlockBee min_tx for this coin, in whole USD. NEVER blocks the caller.

    This is read while rendering /buy — and /buy holds a pooled DB connection open across the whole
    render (_render_buy queries the plans first). A synchronous 5s gateway call in that window
    parks a connection idle-in-transaction, three times per page, for every request that arrives on
    a cold or just-expired entry. So: serve what we have and refresh behind the request.

    Cold cache returns None, i.e. the static floor — the same answer as any other failure, and
    still the safe direction (the live figure can only ever RAISE the gate). Within a second or two
    the refresh lands and subsequent requests get the sharper number. `_in_flight` collapses the
    stampede at expiry: N concurrent readers on a stale entry spawn ONE fetch, not N.
    """
    with _lock:
        hit = _cache.get(coin)
        fresh = hit is not None and hit[0] > now
        if not fresh and coin not in _in_flight:
            _in_flight.add(coin)
            spawn = True
        else:
            spawn = False
    if spawn:
        threading.Thread(target=_fetch, args=(coin,), daemon=True, name=f"min_tx:{coin}").start()
    return hit[1] if hit is not None else None


def min_usd_for(method: PaymentMethod) -> int:
    """Smallest plan amount (USD, whole dollars) this coin can actually be paid with.

    0 = no gate for this method. Callers compare against the amount the customer will REALLY pay,
    i.e. after any promo discount — comparing against the list price is what let a discounted
    $13.50 plan drop to $11.48 and slip under the $12 USDT threshold.
    """
    floor = _FLOOR_USD.get(method)
    if floor is None:
        return 0
    coin = _COIN.get(method)
    live = _live_usd(coin, now=time.time()) if coin else None
    if live is not None and live > floor:
        # Worth a line in the log: it means the measured floors above have drifted out of date and
        # the gate is now being held up by the live lookup alone.
        log.warning("min_tx for %s is %s USD live, above the static floor %s", coin, live, floor)
        return live
    return floor


def gate_ok(method: PaymentMethod, amount_cents: int) -> bool:
    """Can `amount_cents` be paid with this coin at all?"""
    return amount_cents >= min_usd_for(method) * 100
