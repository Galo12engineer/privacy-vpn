"""CP → node-agent client (control channel).

Transport: TLS with PINNING of the node's self-signed cert (H1) — we trust exactly the
cert the node presented at registration (cadata + check_hostname=False). Plus an HMAC
signature of the body (proves possession of agent_secret). All together: mutual
authentication without PKI.

No silent fallback to plaintext: if there is no pinned cert for the node (or
scheme=https but there is no cert) — we raise NodeChannelError so that the calling code
SEES it (see M4) instead of silently skipping the abuse block.
"""

from __future__ import annotations

import json
import ssl

import httpx

from app.config import get_settings
from app.models.enums import NodeProtocol
from app.security import signing
from app.services.config_builder import server_allowed_ips


class NodeChannelError(RuntimeError):
    """The CP→node channel is unusable (no pinned cert, etc.)."""


def _agent_url(node, path: str) -> str:
    s = get_settings()
    return f"{s.agent_control_scheme}://{node.endpoint_host}:{s.agent_control_port}{path}"


def _verify_for(node):
    """SSL context with pinning of the node's cert (or True/path-string for http/CA bundle)."""
    s = get_settings()
    if s.agent_control_scheme != "https":
        # explicit opt-in: http is acceptable only when the channel is already encrypted
        # (e.g. CP↔node over WireGuard/onion). By default scheme=https.
        return True
    if not getattr(node, "agent_tls_cert", None):
        raise NodeChannelError(
            f"no pinned TLS cert for node {node.id}: the node never registered with TLS — "
            "refusing to talk over an unprotected channel"
        )
    ctx = ssl.create_default_context(cadata=node.agent_tls_cert)
    ctx.check_hostname = False  # we trust by cert pinning, not by hostname
    if s.agent_ca_bundle:
        ctx.load_verify_locations(cafile=s.agent_ca_bundle)
    return ctx


def _signed_post(node, path: str, payload: dict, *, client: httpx.Client | None = None):
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts, sig = signing.sign(node.agent_secret or "", body)
    headers = {
        "Content-Type": "application/json",
        "X-Timestamp": ts,
        "X-Signature": sig,
    }
    own = client is None
    c = client or httpx.Client(timeout=10, verify=_verify_for(node))
    try:
        resp = c.post(_agent_url(node, path), content=body, headers=headers)
        # A NON-2xx from the agent (401 bad-sig / 4xx / 5xx) = NOT applied on the node. Previously we
        # returned the Response without checking → the calling code (add/remove/block_peer) treated it as success.
        resp.raise_for_status()
        return resp
    finally:
        if own:
            c.close()


def dispatch_add_peer(node, peer, lease, *, client: httpx.Client | None = None):
    if peer.protocol_type == NodeProtocol.VLESS_REALITY:
        # VLESS user: the agent will call `xray api adu` (lease is not needed)
        payload = {"protocol": "vless_reality", "uuid": peer.vless_uuid, "email": peer.vless_email}
    else:
        payload = {
            "protocol": peer.protocol_type.value,
            "public_key": peer.public_key,
            "allowed_ips": server_allowed_ips(node, lease),
            "preshared_key": peer.preshared_key,
        }
    return _signed_post(node, "/v1/peers", payload, client=client)


def dispatch_remove_peer(
    node,
    public_key: str | None = None,
    *,
    protocol: str = "wireguard",
    email: str | None = None,
    client: httpx.Client | None = None,
):
    """Remove a peer on the node. WG — by public_key; VLESS — `xray api rmu` by email."""
    if protocol == "vless_reality":
        payload = {"protocol": "vless_reality", "email": email}
    else:
        payload = {"public_key": public_key}
    return _signed_post(node, "/v1/peers/remove", payload, client=client)
