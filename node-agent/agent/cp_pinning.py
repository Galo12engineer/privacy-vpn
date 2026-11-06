"""#13-extra: TOFU pinning of the control-plane TLS cert on the node side.

CP already uses a public-CA cert (https) — basic MITM protection is in place (#13 fail-closed
forbids cleartext http). Pinning is an ADDITIONAL layer: on the first successful contact
we remember the SHA-256 fingerprint of the CP cert; on subsequent ones we compare. A change of
fingerprint is LOGGED loudly (we do not stay silent), but does NOT block the channel: legitimate
ACME cert rotation (every ~60-90d) would otherwise periodically tear down the node's link to CP
without operator intervention — the same trade-off as #18 (CP warns about a change of the node's
endpoint/cert, but does not refuse). Only https:// — .onion is already authenticated by the
address of the hidden service itself, localhost is dev/tests (see AgentSettings.is_cp_secure).
"""

from __future__ import annotations

import hashlib
import logging
import socket
import ssl
from urllib.parse import urlsplit

from agent import store

log = logging.getLogger("agent.cp_pinning")


def fetch_fingerprint(base_url: str, *, timeout: float = 10.0) -> str | None:
    """SHA-256 fingerprint of the TLS cert the CP currently serves. None — not https, a broken URL
    or a network failure (a failure must not break enroll — this is an extra layer, not the only protection)."""
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname:
        return None
    host = parts.hostname
    port = parts.port or 443
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as e:
        log.warning("could not obtain the CP cert fingerprint: %s", type(e).__name__)
        return None
    if not der:
        return None
    return hashlib.sha256(der).hexdigest()


def check_and_pin(state_dir: str, base_url: str, *, fetch=fetch_fingerprint) -> None:
    """TOFU: first contact — remember it; afterwards — compare and warn loudly on a change."""
    fp = fetch(base_url)
    if fp is None:
        return  # not https / network unavailable — the extra layer is unavailable, the basic CA check remains
    pinned = store.load_pinned_cp_cert(state_dir)
    if pinned is None:
        store.save_pinned_cp_cert(state_dir, fp)
        log.info("cp cert TOFU-pinned: %s", fp[:16])
    elif pinned != fp:
        log.warning(
            "CP cert fingerprint changed %s -> %s — legitimate rotation or MITM, verify by hand",
            pinned[:16], fp[:16],
        )
        store.save_pinned_cp_cert(state_dir, fp)  # ratchet forward — we do not block the channel
