"""Building the client vless:// link (Reality+XHTTP) and the base64 subscription.

The link is understood by clients on the **Xray** engine (v2rayNG, Streisand, v2rayTun,
desktop xray-core) — sing-box/NekoBox do not support XHTTP. ⚠️ Hiddify by default drives an
imported XHTTP profile through its own sing-box fork, where mode=auto is not resolved and the
upload hangs (hiddify-app#2082) — sites load, but uploads/calls hang; the mode parameter from
the URL is lost on import, so it cannot be "fixed via the link". That is why in the guide we
recommend pure xray-core (v2rayNG). mode=auto on xray-core resolves itself to stream-one — on
prod (Xray v26.3.27) it works correctly. To an inexperienced user we hand out the subscription
(base64 of the lines) as a QR: one scan + auto-update on key rotation.
"""

from __future__ import annotations

import base64
from urllib.parse import quote, urlencode

from app.services.reality_profile import profile_from_node


def to_vless_link(node, peer, *, label: str | None = None) -> str:
    """vless://UUID@host:port?...#label for VLESS+Reality+XHTTP. Without flow (Vision
    is incompatible with XHTTP)."""
    profile = profile_from_node(node)
    if profile is None or not getattr(peer, "vless_uuid", None):
        raise ValueError("node without a reality profile, or peer without a vless_uuid")
    short_ids = profile.get("short_ids") or [""]
    params = {
        "encryption": "none",
        "security": "reality",
        "sni": profile["server_names"][0],
        "fp": profile["fingerprint"],
        "pbk": profile["public_key"],
        "sid": short_ids[0],
        "type": "xhttp",
        "path": profile["path"],
        "mode": "auto",
    }
    query = urlencode(params)
    frag = quote(label or node.name)
    return f"vless://{peer.vless_uuid}@{node.endpoint_host}:{node.endpoint_port}?{query}#{frag}"


def to_subscription(links: list[str]) -> str:
    """base64 (standard) of the vless:// lines joined by \\n — the subscription format of Xray clients."""
    return base64.b64encode("\n".join(links).encode()).decode()
