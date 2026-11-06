# Архитектура (обзор)

5 слоёв; ничего не заглушка. Полный data-flow и инвентарь — [no-logs-inventory.md](no-logs-inventory.md).

```
Клиент (офиц. WireGuard) ──WG UDP 51820/443──► Нода (RAM-only)
        ▲ HTTPS / .onion                          │ wg · nftables(NAT) · per-node no-log DNS
Браузер/Tor (WASM-keygen) ─► reverse-proxy(без client-IP) ─► Control Plane (FastAPI) ─► PostgreSQL(шифр at-rest)
                                                   │                 ▲ mTLS+HMAC           │ шифр-бэкап оффсайт
                                                   └─ Payments(Monero/BTCPay/шлюз) ───────┘ агрегатный health ◄── агент
```

## Компоненты
1. **Control plane** ([control-plane/](../control-plane)) — источник правды: аккаунты, time-credit биллинг,
   ноды, peer'ы, IPAM, ревокация, платежи, кабинет, админка, шедулер. FastAPI + SQLAlchemy + Alembic.
2. **Node-agent** ([node-agent/](../node-agent)) — RAM-only; принимает команды по mTLS+HMAC, применяет
   wg/nftables, пушит агрегатный health, авто-регистрируется по токену.
3. **Веб-кабинет (+ .onion)** — server-rendered, без обязательного JS; браузерный X25519 keygen + локальный QR.
4. **Админка** — логин scrypt + TOTP 2FA; data-driven тарифы; ноды; инвойсы; блок peer'а.
5. **Гайды** — [web/static/guides](../web/static/guides) (Win/macOS/iOS/Android/Linux + QR).

## Границы доверия
- Приватники клиентов — только в браузере; сервер видит лишь pubkey.
- Платёж ↔ аккаунт — скрабится после активации.
- DNS — по-нодно, без логов; нет центральной точки корреляции.
- Ноды RAM-only; на CP — только агрегаты, без пер-юзер данных.

## Запуск
`docker compose up` (CP+БД+scheduler+proxy+tor). Ноды — `infra/provision-node.sh` через cloud-init.
