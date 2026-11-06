"""Xray-core (VLESS+Reality+XHTTP) management on the node. The runner is injected → testable.

The config uses the steal_oneself pattern (chika0801): an outer VLESS tcp+reality on the port →
fallback to the abstract Unix socket @xhttp → inner VLESS xhttp (that's where the clients live).
Users are edited on the fly via the gRPC HandlerService (`xray api adu/rmu`) — no restart.

no-logs: log loglevel=none + StatsService is NOT enabled (HandlerService only) → there is no
per-user accounting. The config is written to tmpfs (see AgentSettings.xray_config_path).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence

from agent.wg import Runner, default_runner

API_TAG = "api"
INBOUND_TAG = "vless-in"  # VLESS+Reality+XHTTP inbound where the clients live (target of adu/rmu)


def render_config(
    reality: dict,
    *,
    listen_port: int,
    users: list[dict],
    api_addr: str = "127.0.0.1:10085",
) -> dict:
    """Build the Xray config. reality — the server profile (dest/server_names/private_key/
    short_ids/path). users — [{"uuid","email"}] for the initial config (afterwards add/remove
    go through api). api_addr — the localhost address of HandlerService."""
    api_host, api_port = api_addr.split(":")
    clients = [{"id": u["uuid"], "email": u["email"], "level": 0} for u in users]
    return {
        "log": {"loglevel": "none", "access": "none", "error": "none"},
        # WITHOUT api.listen: the port is listened on by the dokodemo-door inbound below (tag=api).
        # Specifying BOTH api.listen AND an inbound on the same port = double bind → "address already in use".
        "api": {"tag": API_TAG, "services": ["HandlerService"]},
        "routing": {
            "rules": [
                {"type": "field", "inboundTag": [API_TAG], "outboundTag": "api"},
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"},
            ]
        },
        "inbounds": [
            {
                "listen": api_host,
                "port": int(api_port),
                "protocol": "dokodemo-door",
                "settings": {"address": api_host},
                "tag": API_TAG,
            },
            {
                # A single VLESS inbound: XHTTP + Reality, clients connect directly (target of `xray api adu/rmu`).
                # Reality itself forwards unauthenticated probes to the target (anti-active-probing),
                # so a separate tcp-fallback to a local site is not needed.
                "listen": "0.0.0.0",
                "port": listen_port,
                "protocol": "vless",
                "tag": INBOUND_TAG,
                "settings": {"decryption": "none", "clients": clients},
                "streamSettings": {
                    "network": "xhttp",
                    "security": "reality",
                    "realitySettings": {
                        "target": reality["dest"],
                        "serverNames": reality["server_names"],
                        "privateKey": reality["private_key"],
                        "shortIds": reality["short_ids"],
                    },
                    "xhttpSettings": {"path": reality["path"], "mode": "auto"},
                },
            },
        ],
        "outbounds": [
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "block"},
        ],
    }


class Xray:
    """Management of a running Xray: writing the config to tmpfs, restart, add/remove user."""

    def __init__(
        self,
        *,
        config_path: str,
        api_addr: str = "127.0.0.1:10085",
        inbound_tag: str = INBOUND_TAG,
        restart_cmd: Sequence[str] = ("systemctl", "restart", "xray"),
        runner: Runner = default_runner,
    ):
        self.config_path = config_path
        self.api_addr = api_addr
        self.inbound_tag = inbound_tag
        self.restart_cmd = list(restart_cmd)
        self.runner = runner

    def write_config(self, config: dict) -> None:
        """Atomically write the config (tmpfs): temp file in the same directory → fsync → replace."""
        directory = os.path.dirname(self.config_path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".xray-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(config))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.config_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def restart(self):
        """Restart Xray (it re-reads the config from disk). Tears down active connections — call
        only on bootstrap/reconciliation, NOT on every add/remove (the api exists for those)."""
        return self.runner(self.restart_cmd)

    def add_user(self, uuid: str, email: str):
        """Add a VLESS user ON THE FLY (without a restart): `xray api adu` with a mini-config."""
        payload = {
            "inbounds": [
                {
                    "tag": self.inbound_tag,
                    "protocol": "vless",
                    "settings": {"clients": [{"id": uuid, "email": email, "level": 0}]},
                }
            ]
        }
        fd, path = tempfile.mkstemp(prefix=".adu-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(payload))
            return self.runner(["xray", "api", "adu", f"--server={self.api_addr}", path])
        finally:
            os.unlink(path)

    def remove_user(self, email: str):
        """Remove a VLESS user by email ON THE FLY: `xray api rmu -tag=<inbound> <email>`."""
        return self.runner(
            ["xray", "api", "rmu", f"--server={self.api_addr}", f"-tag={self.inbound_tag}", email]
        )
