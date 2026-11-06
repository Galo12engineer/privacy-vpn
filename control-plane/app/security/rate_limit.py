"""Ephemeral rate-limit in RAM (token bucket). Keys are an IP hash, NOT persisted (no-logs).

Protects the login endpoint against brute force. The IP is visible only at the moment of
the request, is not logged and is not stored — only a transient counter in the process
memory.
"""

from __future__ import annotations

import hashlib
import threading
import time


class RateLimiter:
    def __init__(self, max_events: int, per_seconds: int):
        self.max_events = max_events
        self.per_seconds = per_seconds
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        cutoff = now - self.per_seconds
        with self._lock:
            events = [t for t in self._buckets.get(key, []) if t > cutoff]
            if len(events) >= self.max_events:
                self._buckets[key] = events
                return False
            events.append(now)
            self._buckets[key] = events
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)


def client_key(identity: str | None, scope: str) -> str:
    """Transient key derived from an identifier (we hash it, we do not store the identifier itself).

    `identity` is usually the IP, BUT Caddy strips the client-IP (no-logs) → an IP key would
    be global. That is why admin login is keyed by username (M7): per-target protection
    without disclosing/storing the IP. The name lives only as a hash in the RAM limiter, it
    is not logged.
    """
    raw = f"{scope}:{identity or 'unknown'}"
    return hashlib.sha256(raw.encode()).hexdigest()
