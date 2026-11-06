"""Node-agent: FastAPI control server (CP → agent). Applies wg commands. RAM-only.

The node's secret/identifier is kept in the process memory (RAM-only ethos): on reboot —
re-registration using the token from cloud-init.
"""

from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException, Request, Response

from agent import signing, store
from agent.config import get_agent_settings
from agent.wg import WireGuard, tool_for_protocol
from agent.xray import Xray


class AgentState:
    def __init__(self) -> None:
        self.node_id: str | None = None
        self.agent_secret: str | None = None
        self.wg: WireGuard | None = None
        self.xray: Xray | None = None  # for a vless_reality node (managing VLESS users)
        # TLS control channel (H1): paths to the cert/key in tmpfs for uvicorn
        self.tls_cert_path: str | None = None
        self.tls_key_path: str | None = None


state = AgentState()
app = FastAPI(title="privacyvpn node agent")
# #22: RAM-TTL replay-guard on incoming CP→agent commands (add/remove peer)
_replay_guard = signing.ReplayGuard()


def get_wg() -> WireGuard:
    if state.wg is None:
        # usually state.wg is already set by the bootstrap with the right tool; fallback —
        # we determine awg/wg by the protocol from the node config cache
        s = get_agent_settings()
        cfg = store.load_config(s.state_dir) or {}
        state.wg = WireGuard(s.wg_interface, tool=tool_for_protocol(cfg.get("protocol")))
    return state.wg


def get_xray() -> Xray:
    if state.xray is None:
        # user management (adu/rmu) requires only the api address + inbound-tag, not the profile
        s = get_agent_settings()
        state.xray = Xray(config_path=s.xray_config_path, api_addr=s.xray_api_addr)
    return state.xray


async def _verify(request: Request) -> bytes:
    body = await request.body()
    if not signing.verify(
        state.agent_secret or "",
        body,
        request.headers.get("X-Timestamp", ""),
        request.headers.get("X-Signature", ""),
        replay_guard=_replay_guard,
    ):
        raise HTTPException(status_code=401, detail="bad signature")
    return body


@app.post("/v1/peers")
async def add_peer(request: Request) -> dict:
    body = await _verify(request)
    data = json.loads(body)
    if data.get("protocol") == "vless_reality":
        get_xray().add_user(data["uuid"], data["email"])  # on the fly, without restarting Xray
    else:
        get_wg().add_peer(data["public_key"], data["allowed_ips"], data.get("preshared_key"))
    return {"status": "ok"}


@app.post("/v1/peers/remove", status_code=204)
async def remove_peer(request: Request) -> Response:
    body = await _verify(request)
    data = json.loads(body)
    if data.get("protocol") == "vless_reality":
        get_xray().remove_user(data["email"])
    else:
        get_wg().remove_peer(data["public_key"])
    return Response(status_code=204)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
