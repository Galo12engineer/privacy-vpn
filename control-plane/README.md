# Control Plane

Источник правды: аккаунты, подписки (time-credit), ноды, peer'ы, IPAM, биллinг, ревокация.
REST/JSON API (Фаза 5) — единственный интерфейс для кабинета, .onion и админки.

## Стек
Python 3.12 · FastAPI · SQLAlchemy 2.0 · Alembic · PostgreSQL (prod) / SQLite (dev).

## Установка (dev)

```bash
cd control-plane
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # отредактировать секреты
```

## Миграции

```bash
alembic upgrade head          # применить
alembic downgrade base        # откатить всё
alembic current               # текущая ревизия
```

URL БД берётся из `CP_DATABASE_URL` (см. `migrations/env.py`), не из `alembic.ini`.

## Тесты

```bash
pytest -q
```

Покрытие Фазы 1:
- `test_account_number.py` — энтропия ≥60 бит, формат Mullvad, детерминированный HMAC, pepper-зависимость.
- `test_ipam.py` — выдача/пропуск .1/переиспользование/исчерпание, v6-маппинг, отсутствие истории после ревока.
- `test_billing.py` — стекинг времени (new/future/lapsed), лайфцикл-статусы, скраб invoice↔account.
- `test_no_pii_filter.py` — вырезание client-IP/email из логов.

## Структура

```
app/
  config.py            # настройки (env CP_*)
  db.py                # движок/сессии (SQLite+Postgres)
  models/              # SQLAlchemy 2.0 модели + enums
  services/
    account_number.py  # генерация/нормализация/HMAC номера
    ipam.py            # выдача/освобождение IP
    billing.py         # time-credit, статусы, скраб
  logging/
    no_pii_filter.py   # запрет PII/client-IP в логах
migrations/            # Alembic
tests/
```

## Что НЕ хранится (no-logs)
Приватники клиентов, client-IP, IP-сессии, пер-юзер трафик, DNS-запросы, долгоживущая связь
платёж↔аккаунт, PII. См. [`docs/no-logs-inventory.md`](../docs/no-logs-inventory.md).
