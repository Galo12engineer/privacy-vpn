"""HMAC signing (mirror of control-plane/app/security/signing.py). On top of mTLS.

The window (60s by default) cuts off replays OUTSIDE the window; ReplayGuard (a RAM-TTL set
of seen signatures) closes byte-for-byte repeats INSIDE the window (#22). The signature is unique
per request (ts+body+secret) → it works as a dedup key without a separate nonce in the
wire format (the signature format is a contract, see the golden-vector test)."""

import hashlib
import hmac
import time

DEFAULT_WINDOW_SECONDS = 60


def sign(secret: str, body: bytes, *, timestamp: int | None = None) -> tuple[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return ts, mac


class ReplayGuard:
    """RAM-only TTL set of seen signatures. One instance per verifier process."""

    def __init__(self, window: int = DEFAULT_WINDOW_SECONDS):
        self.window = window
        self._seen: dict[str, float] = {}

    def _purge(self, now: float) -> None:
        expired = [sig for sig, exp in self._seen.items() if exp <= now]
        for sig in expired:
            del self._seen[sig]

    def check(self, signature: str) -> bool:
        """True — seen for the first time (accept, remember). False — already seen (replay)."""
        now = time.time()
        self._purge(now)
        if signature in self._seen:
            return False
        self._seen[signature] = now + self.window
        return True


def verify(
    secret: str,
    body: bytes,
    timestamp: str,
    signature: str,
    *,
    window: int = DEFAULT_WINDOW_SECONDS,
    replay_guard: "ReplayGuard | None" = None,
) -> bool:
    if not secret or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > window:
        return False
    expected = hmac.new(
        secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False
    if replay_guard is not None and not replay_guard.check(signature):
        return False
    return True
