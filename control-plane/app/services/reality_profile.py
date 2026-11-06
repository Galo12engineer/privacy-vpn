"""Reality server profile (VLESS+XHTTP) + client identifier generation.

Reality masks the TLS handshake as a real site (dest/SNI) and protects against active
probing; XHTTP normalizes post-handshake behaviour (volume/timings). x25519 keys are
generated right in Python — the format matches `xray x25519` (base64url without padding,
private key clamped per RFC 7748).

no-logs nuance: VLESS identifies the client by a UUID that the SERVER ISSUES (unlike the
WG pubkey, which the client generates itself). It is an access identifier, not an activity
log; Xray logs and StatsService are disabled — see docs/no-logs-inventory.md.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

# since Feb-2026 the chrome/safari/ios fingerprints are a giveaway for TSPU (XTLS#6293) → default firefox
DEFAULT_FINGERPRINT = "firefox"
DEFAULT_PATH = "/"
_REQUIRED = ("private_key", "public_key", "short_ids", "server_names", "dest", "path", "fingerprint")


def _b64url(raw: bytes) -> str:
    """base64url without padding — the Reality key format (RawURLEncoding in `xray x25519`)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_x25519() -> tuple[str, str]:
    """(private_b64url, public_b64url) for Reality. The private key is clamped, like `xray x25519`."""
    raw = bytearray(os.urandom(32))
    raw[0] &= 248
    raw[31] &= 127
    raw[31] |= 64
    priv = X25519PrivateKey.from_private_bytes(bytes(raw))
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return _b64url(bytes(raw)), _b64url(pub)


def generate_short_id() -> str:
    """8 bytes → 16 hex characters (a valid Reality shortId: hex, even length, <=16)."""
    return secrets.token_hex(8)


def generate_server_profile(
    *,
    dest: str,
    server_names: list[str],
    path: str = DEFAULT_PATH,
    fingerprint: str = DEFAULT_FINGERPRINT,
) -> dict:
    """Server-side Reality profile. dest ('host:port') and server_names (SNI) are set by the operator
    at provisioning time (steal-oneself with your own domain, or a borrowed TLS1.3 site)."""
    priv, pub = generate_x25519()
    return {
        "private_key": priv,
        "public_key": pub,
        "short_ids": [generate_short_id()],
        "server_names": list(server_names),
        "dest": dest,
        "path": path,
        "fingerprint": fingerprint,
    }


def dump_profile(profile: dict) -> str:
    return json.dumps(profile)


def profile_from_node(node) -> dict | None:
    """The node's reality_config (JSON) → dict, or None if unset/corrupted."""
    raw = getattr(node, "reality_config", None)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def generate_client(peer_id) -> dict:
    """VLESS client identifiers: uuid + email tag for `xray api adu/rmu`.
    The email is derived from peer_id (unique, no PII)."""
    return {"uuid": str(uuid.uuid4()), "email": f"p-{peer_id}"}


def validate_server_profile(p: dict) -> None:
    """Validate the server profile; ValueError on violation."""
    for key in _REQUIRED:
        if key not in p:
            raise ValueError(f"reality-профиль без поля {key}")
    if not p["server_names"]:
        raise ValueError("нужен хотя бы один server_name (SNI)")
    for sid in p["short_ids"]:
        if sid and (len(sid) > 16 or len(sid) % 2 or not all(c in "0123456789abcdef" for c in sid.lower())):
            raise ValueError(f"невалидный shortId: {sid!r}")
    if ":" not in p["dest"]:
        raise ValueError("dest должен быть в формате host:port")
