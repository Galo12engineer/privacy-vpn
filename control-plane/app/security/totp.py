"""TOTP (RFC 6238) for admin-panel 2FA. No external dependencies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code_at(secret: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


def now_code(secret: str, *, t: float | None = None, step: int = 30, digits: int = 6) -> str:
    t = time.time() if t is None else t
    return _code_at(secret, int(t // step), digits)


def matched_counter(
    secret: str, code: str, *, t: float | None = None, step: int = 30, window: int = 1
) -> int | None:
    """Return the counter (time step) at which the code matched, otherwise None. For anti-replay (#23):
    the caller requires a strictly increasing counter → one code cannot be replayed within the ~90s window."""
    t = time.time() if t is None else t
    counter = int(t // step)
    code = (code or "").strip()
    for drift in range(-window, window + 1):
        if hmac.compare_digest(_code_at(secret, counter + drift), code):
            return counter + drift
    return None


def verify(secret: str, code: str, *, t: float | None = None, step: int = 30, window: int = 1) -> bool:
    return matched_counter(secret, code, t=t, step=step, window=window) is not None


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    return f"otpauth://totp/{issuer}:{account}?secret={secret}&issuer={issuer}"
