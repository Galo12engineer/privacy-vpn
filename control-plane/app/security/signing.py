"""HMAC signing of CP↔agent requests (on top of mTLS). Protection against spoofing/replay.

Signature = HMAC-SHA256(secret, "<ts>." + body). The time window cuts off replays OUTSIDE
the window, but inside the window (60s by default) a byte-for-byte repeat of an intercepted
request would previously pass as valid (#22: a replayed add_peer restores a removed peer; a
replayed health flips DOWN→ACTIVE). ReplayGuard closes this too: a RAM-TTL set of already
seen signatures — a signature is unique per request (it depends on ts+body+secret), therefore
it is sufficient as a deduplication key, without a separate nonce in the wire format (the
signature format is a contract between CP and the agent, see the golden-vector test).
"""

import hashlib
import hmac
import time

DEFAULT_WINDOW_SECONDS = 60


def sign(secret: str, body: bytes, *, timestamp: int | None = None) -> tuple[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return ts, mac


class ReplayGuard:
    """RAM-only TTL set of seen signatures. Keep one instance per process (per
    verifying side), NOT per request. Not persisted (RAM-only ethos) — a process
    restart simply reopens the `window`, no longer than what would have been
    available to an attacker without the guard anyway."""

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
