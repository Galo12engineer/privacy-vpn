# PrivacyVPN: a privacy-first WireGuard VPN platform

A complete, production-tested VPN service built around one rule: **the operator should be unable to learn who you are or what you do.** It includes the control plane, the node agent, provisioning, monitoring, backups and incident docs.

> **Status:** this ran in production from July to October 2026 on real nodes with live crypto payments, and was then decommissioned cleanly. This repository is a sanitized copy for portfolio purposes: real domains, IPs, keys and branding have been replaced with placeholders. Most internal docs are in Russian.

## Architecture

```
Client (official WireGuard app) ──WG UDP 51820/443──► Node (RAM-only)
        ▲ HTTPS / .onion                               │ wireguard · nftables NAT · per-node no-log DNS
Browser / Tor (in-browser keygen) ─► reverse proxy (no client IP) ─► Control plane (FastAPI) ─► PostgreSQL
                                                                         ▲ pinned TLS + HMAC   │ age-encrypted offsite backups
                                                                         └─ aggregate health ◄─ node agent
```

| Component | What it does |
|---|---|
| `control-plane/` | FastAPI + SQLAlchemy + Alembic: accounts, time-credit billing, nodes, IPAM, peer lifecycle, payments, customer portal, admin panel, scheduler |
| `node-agent/` | RAM-only agent: applies WireGuard and nftables changes, HMAC-signed commands over pinned TLS, auto-registration with one-time tokens, aggregate health reporting |
| `web/`, `admin/` | Server-rendered portal that works without JavaScript; admin panel with scrypt + TOTP 2FA |
| `infra/` | cloud-init provisioning, systemd units, nftables rate limiting, Tor hidden service, reverse proxy, monitoring, backups |
| `support-bot/` | Support relay bot |

## Security and privacy design

- **Client private keys never reach the server.** X25519 keys are generated in the browser, and CI checks the implementation against the RFC 7748 test vectors. The server only ever sees the public key.
- **No-logs as code, not as a promise.** The reverse proxy strips client IPs. Each node runs its own no-log DNS resolver. A CI check fails the build if PII could reach the logs. See [`docs/no-logs-inventory.md`](docs/no-logs-inventory.md).
- **RAM-only nodes:** no swap, journald in memory. Seizing a node yields no per-user data.
- **Hardened control channel:** the control plane talks to nodes over TLS pinned to each node's self-signed certificate, and every command carries an HMAC signature with a per-node secret, a timestamp window and replay protection. Together they give mutual authentication without a PKI (not mTLS in the strict sense: there are no client certificates).
- **Data minimization:** account-number login (no email), the link between a payment and an account is scrubbed after activation, expired peers are revoked automatically.
- **Defense in depth:** nftables rate limiting, a hidden admin path, 2FA, and IP reputation checks for infrastructure.

## Engineering

- **740+ automated tests:** control plane (665), node agent (75), support bot (21), plus JS keygen and QR checks
- GitHub Actions CI: ruff, a migration up/down/up round-trip, tests, i18n parity and policy checks (no PII in logs, no external resources)
- Daily `age`-encrypted PostgreSQL backups pulled offsite, with a documented restore procedure
- Ops docs: [incident postmortem](docs/incident-2026-08-04-monitoring.md), [disaster recovery](docs/disaster-recovery.md), [deploy](docs/deploy.md), [architecture decisions](docs/decisions.md), [CHANGELOG](CHANGELOG.md)

## Run locally

```bash
cp .env.example .env          # fill in secrets: scripts/gen-secrets.sh helps
docker compose up --build     # control plane + db + scheduler + proxy + tor
```

Without Docker:

```bash
cd control-plane && python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]" && alembic upgrade head && uvicorn app.main:app
```

## Tests

```bash
cd control-plane && pytest
cd node-agent && pytest
node scripts/jscheck.cjs
bash scripts/check_no_pii_logs.sh
```

## How it was built

I designed the architecture and the security model, made the technical decisions, reviewed the code, and deployed and operated the service in production. I wrote the implementation with AI coding agents (Claude Code).
