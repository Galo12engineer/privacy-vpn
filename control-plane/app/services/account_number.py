"""Account-number: the only credential. >=60 bits of entropy, Mullvad-style format.

20 decimal digits = 20*log2(10) ~= 66.4 bits (> the 60-bit requirement).
Only HMAC(pepper, digits) is stored on the server — not the plaintext.
"""

import hashlib
import hmac
import re
import secrets

ACCOUNT_NUMBER_DIGITS = 20
_GROUP = 4


def generate_account_number() -> str:
    """Cryptographically strong number: 20 digits in groups of 4."""
    digits = "".join(str(secrets.randbelow(10)) for _ in range(ACCOUNT_NUMBER_DIGITS))
    return format_account_number(digits)


def normalize_account_number(raw: str) -> str:
    """Canonicalize input -> 20 digits without separators. ValueError on wrong length."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) != ACCOUNT_NUMBER_DIGITS:
        raise ValueError("invalid account number length")
    return digits


def format_account_number(raw: str) -> str:
    digits = normalize_account_number(raw)
    return " ".join(digits[i : i + _GROUP] for i in range(0, len(digits), _GROUP))


def is_valid_format(raw: str) -> bool:
    try:
        normalize_account_number(raw)
        return True
    except ValueError:
        return False


def hash_account_number(raw: str, pepper: str) -> str:
    """HMAC-SHA256(pepper, canonical_digits) -> hex(64). Deterministic (for lookup)."""
    digits = normalize_account_number(raw)
    return hmac.new(pepper.encode(), digits.encode(), hashlib.sha256).hexdigest()
