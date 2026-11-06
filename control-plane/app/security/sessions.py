"""Signed cookie session (subject + scope + exp [+ binding]). No server-side store.

scope separates cabinet sessions ("cabinet") from admin sessions ("admin").

binding (M5) — an optional binding of the token to a changing secret of the subject (for
the admin panel — a stamp derived from password_hash). Changing the password changes the
stamp → all old tokens become invalid (revocation without a separate sessions table). Plus
a short TTL for the admin panel narrows the window of a stolen token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

from app.config import get_settings


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(
    subject_id: uuid.UUID,
    *,
    scope: str = "cabinet",
    now: int | None = None,
    ttl: int | None = None,
    binding: str | None = None,
) -> str:
    s = get_settings()
    now = now or int(time.time())
    ttl = ttl if ttl is not None else s.session_ttl_seconds
    payload_obj = {"sub": str(subject_id), "scope": scope, "exp": now + ttl}
    if binding is not None:
        payload_obj["bnd"] = binding
    payload = json.dumps(payload_obj, separators=(",", ":")).encode()
    mac = hmac.new(s.session_secret.encode(), payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(mac)}"


def read(token: str, *, now: int | None = None) -> dict | None:
    """Verify the signature + expiry and return the payload (scope/binding are checked by the caller)."""
    s = get_settings()
    now = now or int(time.time())
    try:
        body_b64, mac_b64 = token.split(".", 1)
        payload = _b64d(body_b64)
        expected = hmac.new(s.session_secret.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(mac_b64)):
            return None
        data = json.loads(payload)
        if int(data["exp"]) < now:
            return None
        return data
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def verify(
    token: str,
    *,
    scope: str = "cabinet",
    now: int | None = None,
    binding: str | None = None,
) -> uuid.UUID | None:
    data = read(token, now=now)
    if data is None or data.get("scope") != scope:
        return None
    if binding is not None and data.get("bnd") != binding:
        return None
    try:
        return uuid.UUID(data["sub"])
    except (ValueError, KeyError):
        return None
