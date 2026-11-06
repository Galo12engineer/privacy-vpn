"""Percent-off promo codes: normalization, validation, price arithmetic, capacity reservation.

This module owns the ONE rounding rule for a discounted price. Everything downstream — the plan
card the customer reads, the min-USD coin gate, the Invoice row, the amount handed to the payment
provider — asks `discounted_cents` and gets the same integer, so the price shown and the price
charged cannot drift apart by a cent.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PromoCode
from app.models.base import as_utc

# Ceiling on the discount an operator can set. Not a policy preference — a guard: a 100% code would
# mint a zero-amount invoice, which the gateway quotes as 0 coins and every callback then reads as
# "fully paid" (free subscriptions, granted unattended). Anything at or below this leaves a real
# amount to pay. A deeper discount is a manual operator grant (admin → accounts → extend), not a code.
MAX_PERCENT = 90
MIN_PERCENT = 1
# Shortest code we will store.
#
# Be honest about what this is for. A promo code is NOT a secret and cannot be defended like one:
# whatever we do, an attacker learns hit-from-miss by whether the price changed, and no throttle
# fixes that (a throttle that also blocked VALID codes would just hand them a way to switch the
# feature off for everyone). Reproduced: with the failure budget fully spent, a wrong code says
# "too many attempts" while a right one still applies — the throttle is itself the oracle.
#
# So the defence is keyspace, plus the fact that a guessed code only buys the discount the operator
# already chose to give away, bounded by max_uses. Six chars = 36^6 ≈ 2.2e9; the global limiter's
# ~43k attempts/day walks that in ~140 years. Four chars would fall in under three weeks.
# A dictionary word (SUMMER, LAUNCH) is guessable no matter how long it is — the admin page says so.
MIN_CODE_LEN = 6

_NON_CODE = re.compile(r"[^A-Z0-9]")


class PromoError(Exception):
    """Why a code did not apply.

    `reason` is a stable key, not a message: the caller maps it to a translated string. Keeping the
    text out of here is what stops a Russian literal from leaking onto the English /buy page.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def normalize(raw: str) -> str:
    """Canonical stored form: upper-case, A-Z0-9 only, max 32 chars.

    Customers retype codes out of chat and paste them with stray spaces, dashes and lower case.
    Normalizing on BOTH write and read means "launch-20", "LAUNCH 20" and "launch20" are one code —
    and, because the column is UNIQUE on this form, they cannot become three rows in the admin.
    """
    return _NON_CODE.sub("", (raw or "").upper())[:32]


def discounted_cents(price_cents: int, percent: int) -> int:
    """Price after a percent-off discount. THE rounding rule — do not reimplement anywhere else.

    Integer arithmetic on cents, half-up on the FINAL amount (not on the discount): $13.50 −15%
    → (1350*85 + 50)//100 = 1148 = $11.48. No float ever touches a price.

    Floors at 1 cent: `percent` is clamped to MAX_PERCENT, but a defensive floor costs nothing and a
    zero-cent invoice is a free subscription (see MAX_PERCENT). The min-USD coin gate rejects
    anything this small long before it reaches a provider anyway.
    """
    if percent <= 0:
        return price_cents
    percent = min(percent, MAX_PERCENT)
    return max(1, (price_cents * (100 - percent) + 50) // 100)


def _check(promo: PromoCode, *, now: datetime) -> None:
    """Raise PromoError if this code cannot be used right now."""
    # An operator-disabled code reports as `unknown`, not as its own reason: "this code exists but
    # is off" is a free oracle for someone guessing codes, and it tells the customer nothing they
    # can act on. Expiry and exhaustion DO get their own reason — a customer holding a genuine code
    # deserves to know which of the two happened.
    if not promo.is_active:
        raise PromoError("unknown")
    exp = as_utc(promo.expires_at)
    if exp is not None and exp <= now:
        raise PromoError("expired")
    if promo.max_uses is not None and promo.used_count >= promo.max_uses:
        raise PromoError("exhausted")


def lookup(session: Session, raw: str, *, now: datetime | None = None) -> PromoCode:
    """Resolve a customer-entered code to a usable PromoCode, or raise PromoError.

    Read-only: it neither reserves nor mutates anything, so it is safe on the preview path
    (`/buy` with Apply), which must be able to run repeatedly without burning uses.
    """
    now = now or datetime.now(UTC)
    code = normalize(raw)
    if not code:
        raise PromoError("unknown")
    promo = session.execute(
        select(PromoCode).where(PromoCode.code == code)
    ).scalar_one_or_none()
    if promo is None:
        raise PromoError("unknown")
    _check(promo, now=now)
    return promo


def reserve(session: Session, promo: PromoCode, *, now: datetime | None = None) -> None:
    """Consume one use, under a row lock. Raises PromoError if the code is no longer usable.

    Re-reads the row FOR UPDATE and re-validates instead of trusting the object the caller already
    has: between the customer's preview and their submit, another buyer can take the last use and
    the operator can switch the code off. `populate_existing=True` is LOAD-BEARING — without it
    SQLAlchemy hands back the identity-mapped object with its already-loaded `used_count`, so the
    check would re-read the pre-lock value and two concurrent buyers would both sail past a
    max_uses=1 gate. The lock would be taken and its result thrown away. (SQLite ignores FOR UPDATE,
    so this serializes on Postgres only; tests cover the staleness half, which is where the bug was.)

    Counted at INVOICE CREATION, not at settlement: counting on payment would let an unbounded
    number of discounted invoices exist at once and blow straight through max_uses when they all
    settle. The cost of counting early is that an unpaid invoice holds a use — which is why
    `release` exists and why the invoice records which code it took the use from.
    The caller must roll the transaction back if the purchase then fails; POST /buy does.
    """
    now = now or datetime.now(UTC)
    row = session.execute(
        select(PromoCode)
        .where(PromoCode.id == promo.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    _check(row, now=now)
    row.used_count += 1


def was_funded(invoice) -> bool:
    """Did any money ever arrive for this invoice?

    THE discriminator between a real customer and an abandoned cart — and deliberately not the
    invoice's status. `EXPIRED` requires that no deposit address was ever issued, but BlockBee is
    address-per-order, so every real invoice carries one and expiry parks it in `LATE_PAYMENT`
    instead. Keying a refund on the status therefore looked correct and never fired once in
    production. The admin console already tells these two apart exactly this way
    (admin_service.rescuable_invoices).
    """
    return (invoice.crypto_amount_paid or 0) > 0 or bool(invoice.crypto_txs)


def release_if_unfunded(session: Session, invoice) -> bool:
    """`release`, but only for an invoice nobody ever paid. The call every scheduler job should use.

    Both terminal-unpaid paths go through here so neither can drift from the other: expiry (fast,
    within the hour, covers the abandoned cart and the griefer) and the unpaid-retention scrub
    (slow backstop, covers anything expiry never saw). That backstop is not theoretical —
    `CANCELLED` is reached from a provider webhook, not from expiry, so a code redeemed against an
    invoice the gateway later failed would otherwise hold its use until the row was scrubbed,
    and then lose it silently when the scrub nulled the link.
    """
    if was_funded(invoice):
        return False
    return release(session, invoice)


def release(session: Session, invoice) -> bool:
    """Give back the use an invoice reserved, because that invoice will never be paid. Idempotent.

    Without this, `max_uses` is not a control at all: reserving at creation means anyone can spend
    a code's entire allowance by POSTing /buy and walking away. Reproduced before the fix — twenty
    unpaid requests took a max_uses=20 code to zero and every real customer then saw "limit
    reached". Nothing was gained by the attacker, which is exactly what makes it attractive as
    griefing: it costs one HTTP request to burn one of the operator's discounts.

    Idempotency is the FK itself: it is cleared in the same transaction as the decrement, so a
    second call (expiry re-running, a scrub racing it) finds nothing to release and does nothing.
    Also re-locks the counter, because the decrement races other buyers reserving.

    Lock order is the reverse of POST /buy's (that one locks the promo, then INSERTs an invoice;
    this one updates an invoice, then locks the promo) — which is fine only because /buy inserts a
    NEW invoice row and so can never be holding a lock this job wants. If a future caller ever locks
    an EXISTING invoice before calling release, that becomes a genuine deadlock cycle.

    Unconditional: it does NOT ask whether the invoice was paid. Callers outside a test should use
    `release_if_unfunded`, which owns that judgement so the two scheduler jobs cannot disagree
    about it.
    """
    code_id = getattr(invoice, "promo_code_id", None)
    if code_id is None:
        return False
    row = session.execute(
        select(PromoCode)
        .where(PromoCode.id == code_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    invoice.promo_code_id = None
    if row is None:
        return False  # the operator deleted the code outright; nothing left to credit back
    # max(0, ...) rather than a bare decrement: a counter that can go negative would let the next
    # buyer past a limit that is already spent, and no arithmetic here is worth that risk.
    row.used_count = max(0, row.used_count - 1)
    return True
