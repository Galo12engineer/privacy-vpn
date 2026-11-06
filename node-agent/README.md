# Node Agent (RAM-only)

Accepts commands from the control plane over **mTLS + HMAC signature**, applies `wg`/`nftables`,
pushes **aggregate** health metrics. Logs nothing per-user. The node key lives in tmpfs (RAM).

## Flow
1. cloud-init → `infra/provision-node.sh`: packages, `ip_forward`, **no-swap**, journald in RAM,
   per-node no-log DNS (unbound), systemd unit.
2. `agent.run`: `bootstrap()` → genkey (RAM) → registration with the CP via a one-time token →
   `wg up` → `nft` (NAT + forward + redirect 443→51820 + v6 null-route) → health-loop → uvicorn.
3. The CP sends `POST /v1/peers` / `/v1/peers/remove` (signed) → the agent applies `wg set`.

## Endpoints (control channel)
- `POST /v1/peers` — add a peer (pubkey + allowed-ips). HMAC-verified.
- `POST /v1/peers/remove` — remove a peer.
- `GET /healthz` — liveness.

## Running (dev)
```bash
cd node-agent
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Prod: systemd unit `infra/systemd/vpn-node-agent.service`, env from `/run/vpn-agent/agent.env` (RAM).

## What it does NOT do (no-logs)
Does not write IP sessions, per-user traffic, DNS queries, or handshake timestamps. Only aggregates
(cpu/mem/peers) go to the CP. Ports: UDP **51820 and 443** (vanilla WG; obfuscation is post-MVP, the
nodes are made pluggable for an obfuscation layer).
