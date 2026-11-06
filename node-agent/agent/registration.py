"""Node auto-registration in the CP via a one-time token + push of aggregate health metrics."""

from __future__ import annotations

import json

import httpx

from agent import signing
from agent.config import AgentSettings


def register(
    settings: AgentSettings,
    *,
    public_key: str,
    tls_cert_pem: str | None = None,
    client: httpx.Client | None = None,
) -> dict:
    own = client is None
    c = client or httpx.Client(timeout=10)
    try:
        resp = c.post(
            f"{settings.cp_base_url}/v1/nodes/register",
            json={
                "token": settings.registration_token,
                "public_key": public_key,
                "endpoint_port": settings.wg_listen_port,
                # H1: control-channel cert → the CP pins it for subsequent dispatches
                "agent_tls_cert": tls_cert_pem,
            },
        )
        resp.raise_for_status()
        return resp.json()
    finally:
        if own:
            c.close()


def reattach(
    settings: AgentSettings,
    *,
    node_id: str,
    agent_secret: str,
    public_key: str,
    tls_cert_pem: str | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """Node comeback after a reboot: we authenticate with the agent_secret (HMAC, without the
    one-time token), bring a fresh TLS cert (the CP re-pins it) + a stable pubkey. The CP returns
    the network config AND the list of active peers for reconciliation (the wg state after a
    reboot is empty)."""
    body = json.dumps(
        {
            "public_key": public_key,
            "endpoint_port": settings.wg_listen_port,
            "agent_tls_cert": tls_cert_pem,
        },
        separators=(",", ":"),
    ).encode()
    ts, sig = signing.sign(agent_secret, body)
    own = client is None
    c = client or httpx.Client(timeout=10)
    try:
        resp = c.post(
            f"{settings.cp_base_url}/v1/nodes/{node_id}/reattach",
            content=body,
            headers={"Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": sig},
        )
        resp.raise_for_status()
        return resp.json()
    finally:
        if own:
            c.close()


def push_health(
    settings: AgentSettings,
    *,
    node_id: str,
    agent_secret: str,
    metrics: dict,
    client: httpx.Client | None = None,
) -> int:
    body = json.dumps(metrics, separators=(",", ":")).encode()
    ts, sig = signing.sign(agent_secret, body)
    own = client is None
    c = client or httpx.Client(timeout=10)
    try:
        resp = c.post(
            f"{settings.cp_base_url}/v1/nodes/{node_id}/health",
            content=body,
            headers={"Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": sig},
        )
        # #27: the status used to be ignored → 401 (agent_secret desync) / 404 / 500 were treated
        # as success, the node stayed silent, and the CP flipped it DOWN by staleness — masking
        # exactly that secret desync. Now an error response raises an exception → the health loop
        # logs it as a warning.
        resp.raise_for_status()
        return resp.status_code
    finally:
        if own:
            c.close()
