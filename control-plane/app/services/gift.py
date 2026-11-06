"""Gift tokens: prepaid access sold on someone else's counter, redeemed once on ours.

Creates no invoice, so the zero-amount-invoice problem that caps promo codes at 90% does not exist
here. The account number is minted at redemption, in the buyer's own request, and the operator never
learns which account came out of which code. Rationale and rejected alternatives: docs/decisions.md.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Account, GiftToken
from app.models.base import as_utc

# "create an account with time on it" must have exactly one implementation, so the customer path
# calls the admin service. admin_service does not import this module — the dependency is one-way.
from app.services.admin_service import create_account
from app.services.billing import apply_credit

# Crockford base32 (no I, L, O, U). 16 chars = 80 bits: a guessed gift is a free subscription, so
# promo's 6-char floor is nowhere near enough here.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
BODY_LEN = 16
# Without the prefix a token and an account number are two opaque strings, and the buyer pastes the
# token into /login. It is not part of the hash.
PREFIX = "TT"
_GROUP = 4

_NOISE = re.compile(r"[^A-Z0-9]")
_CONFUSABLE = str.maketrans({"I": "1", "L": "1", "O": "0"})

# The batch is shown once, on one page: whatever is not copied off it is money burned.
MAX_BATCH = 50
MAX_DAYS = 3650  # mirrors api/admin_ui.MAX_GRANT_DAYS, which cannot be imported here

GIFT_GRANT_CODE = "gift"  # ledger code when the token was cut from no plan

# Shared with the account number on purpose: a new CP_* setting is the thing that silently does not
# arrive (compose passes an explicit environment list), and its default would be a published pepper.
# The prefix keeps the two hash spaces disjoint.
_HMAC_DOMAIN = "gift-token:v1:"


class GiftError(Exception):
    """Why a token did not redeem. `reason` is a stable key; the page owns the wording."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Redemption:
    """`number` is non-None ONLY for a freshly minted account: plaintext, stored nowhere, and the
    caller must put it in a response body — never a log, a redirect or a URL."""

    account: Account
    number: str | None
    days: int
    device_limit: int
    expires_at: datetime | None


def generate() -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(BODY_LEN))
    return format_token(body)


def format_token(body: str) -> str:
    return PREFIX + "-" + "-".join(body[i : i + _GROUP] for i in range(0, len(body), _GROUP))


def normalize(raw: str) -> str:
    """Canonical body, or "" if the input cannot be a token (an unparseable string and an unknown
    one are the same answer to the buyer, so this returns rather than raises)."""
    s = _NOISE.sub("", (raw or "").upper())
    # only when what remains is exactly a body, so a body starting with "TT" is never truncated
    if len(s) == len(PREFIX) + BODY_LEN and s.startswith(PREFIX):
        s = s[len(PREFIX) :]
    s = s.translate(_CONFUSABLE)
    if len(s) != BODY_LEN or any(c not in _ALPHABET for c in s):
        return ""
    return s


def hash_token(body: str) -> str:
    """HMAC-SHA256(pepper, domain + body) -> hex(64). Deterministic because the row is looked up by
    what the buyer typed; 80 bits of uniform entropy needs no slow KDF."""
    pepper = get_settings().account_number_pepper.encode()
    return hmac.new(pepper, (_HMAC_DOMAIN + body).encode(), hashlib.sha256).hexdigest()


def _check(row: GiftToken, *, now: datetime) -> None:
    """Raise GiftError if this token cannot be redeemed now.

    The four reasons are told apart, unlike promo codes: nobody reaches these branches by guessing
    an 80-bit string, and the buyer paid for the token.
    """
    if row.redeemed_at is not None:
        raise GiftError("redeemed")
    if not row.is_active:
        raise GiftError("disabled")
    exp = as_utc(row.expires_at)
    if exp is not None and exp <= now:
        raise GiftError("expired")


def lookup(session: Session, raw: str, *, now: datetime | None = None) -> GiftToken:
    """Read-only preview path: mutates nothing, takes no lock, must be repeatable. Whatever it
    reports is advisory — `redeem` re-checks under a lock."""
    now = now or datetime.now(UTC)
    body = normalize(raw)
    if not body:
        raise GiftError("unknown")
    row = session.execute(
        select(GiftToken).where(GiftToken.token_hash == hash_token(body))
    ).scalar_one_or_none()
    if row is None:
        raise GiftError("unknown")
    _check(row, now=now)
    return row


def redeem(
    session: Session,
    raw: str,
    *,
    account: Account | None = None,
    now: datetime | None = None,
) -> Redemption:
    """Spend a token: mint an account with the time on it, or extend the caller's own.

    The row lock is what makes redemption exactly-once — it does the job apply_credit's per-invoice
    dedupe does on the money path, which cannot help here because there is no invoice.
    `populate_existing=True` is load-bearing (as in promo.reserve): without it the locked SELECT is
    answered from the identity map, so a row another request just committed comes back with
    redeemed_at=None and the guard waves it through.

    Does NOT commit. The caller must commit BEFORE showing the buyer anything: a number rendered
    from an open transaction is a credential for an account that may never exist.

    `account` decides new-vs-extend and comes from the SESSION, never a form field — a field would
    make this page an oracle for which account numbers exist.
    """
    now = now or datetime.now(UTC)
    body = normalize(raw)
    if not body:
        raise GiftError("unknown")
    row = session.execute(
        select(GiftToken)
        .where(GiftToken.token_hash == hash_token(body))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise GiftError("unknown")
    _check(row, now=now)

    # Coarsened to the day, `updated_at` included: TimestampMixin would otherwise stamp this very
    # UPDATE with the exact instant of redemption (the trap scrub_invoice_linkage documents).
    # Nothing reads either field for logic.
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    row.redeemed_at = day
    row.updated_at = day

    number: str | None = None
    if account is None:
        account, number = create_account(
            session,
            days=row.days,
            device_limit=row.device_limit,
            price_plan_code=row.price_plan_code or GIFT_GRANT_CODE,
            now=now,
        )
    else:
        # stacking from max(now, expires_at) is apply_credit's rule, not reimplemented here
        apply_credit(
            session,
            account,
            days=row.days,
            now=now,
            price_plan_code=row.price_plan_code or GIFT_GRANT_CODE,
            device_limit=row.device_limit,
        )
    return Redemption(
        account=account,
        number=number,
        days=row.days,
        device_limit=account.device_limit,
        expires_at=as_utc(account.expires_at),
    )


def issue_batch(
    session: Session,
    *,
    count: int,
    days: int,
    device_limit: int | None = None,
    price_plan_code: str | None = None,
    expires_at: datetime | None = None,
    note: str | None = None,
) -> list[str]:
    """Mint `count` tokens and return their plaintexts ONCE — the rows carry only HMACs.

    Money-equivalent, like admin_service.create_account: the caller must show them to a human
    immediately and keep them out of logs and URLs. Validation is here and not only in the form
    because `min`/`max` on an <input> is a hint to a browser, not a constraint on a POST.
    """
    if not 1 <= count <= MAX_BATCH:
        raise ValueError(f"количество кодов — от 1 до {MAX_BATCH} за раз")
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"срок — от 1 до {MAX_DAYS} дней")
    if device_limit is not None and device_limit < 1:
        raise ValueError("лимит устройств должен быть ≥ 1 (пусто = как в тарифе по умолчанию)")

    plaintexts: list[str] = []
    for _ in range(count):
        token = generate()
        session.add(
            GiftToken(
                token_hash=hash_token(normalize(token)),
                days=days,
                device_limit=device_limit,
                price_plan_code=price_plan_code,
                expires_at=expires_at,
                note=note or None,
            )
        )
        plaintexts.append(token)
    # Flush here so a UNIQUE violation surfaces while the caller can still refuse the whole batch,
    # not at a later commit with half a page of tokens already rendered.
    session.flush()
    return plaintexts


def list_tokens(session: Session) -> list[GiftToken]:
    return list(
        session.execute(select(GiftToken).order_by(GiftToken.created_at.desc())).scalars().all()
    )


def set_active(session: Session, row: GiftToken, active: bool) -> GiftToken:
    """The only edit an issued token allows: days, device limit and expiry are the promise printed
    on a listing already in a buyer's hands. To retire a batch, switch it off."""
    row.is_active = active
    session.flush()
    return row
