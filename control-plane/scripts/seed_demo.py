#!/usr/bin/env python
"""Demo-data seed for a local smoke run: price plans + nodes (available for sale) + admin + demo account.
Prints the demo account's account-number (cabinet login). NOT for prod.

    CP_DATABASE_URL=sqlite+pysqlite:///./demo.db python scripts/seed_demo.py
(before this: alembic upgrade head)
"""
from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Account, AdminUser, Node, PricePlan
from app.models.enums import AccountStatus, NodeStatus
from app.services.account_number import generate_account_number, hash_account_number
from app.services.admin_auth import create_admin

PLANS = [
    ("1m", "1 месяц", 30, 500),
    ("3m", "3 месяца", 90, 1200),
    ("1y", "1 год", 365, 4000),
]
NODES = [
    ("nl-1", "nl1.example.net", "nl", "Нидерланды", "10.10.0.0/24", "10.10.0.1"),
    ("is-1", "is1.example.net", "is", "Исландия", "10.11.0.0/24", "10.11.0.1"),
]


def _fake_pubkey() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def main() -> int:
    s = get_settings()
    now = datetime.now(UTC)
    with SessionLocal() as session:
        for code, name, days, cents in PLANS:
            if not session.execute(select(PricePlan).where(PricePlan.code == code)).scalar_one_or_none():
                session.add(PricePlan(code=code, name=name, duration_days=days,
                                      price_fiat_cents=cents, fiat_currency="USD", device_limit=5))
        for name, host, lc, ln, v4, dns in NODES:
            if not session.execute(select(Node).where(Node.name == name)).scalar_one_or_none():
                session.add(Node(name=name, endpoint_host=host, location_code=lc, location_name=ln,
                                 subnet_v4=v4, dns_ip=dns, public_key=_fake_pubkey(),
                                 status=NodeStatus.ACTIVE, available_for_sale=True, last_health_at=now))
        if not session.execute(select(AdminUser).where(AdminUser.username == "root")).scalar_one_or_none():
            create_admin(session, username="root", password="demo-admin-pass-123")

        number = generate_account_number()
        session.add(Account(
            account_number_hash=hash_account_number(number, s.account_number_pepper),
            status=AccountStatus.ACTIVE,
            expires_at=now + timedelta(days=21),
            device_limit=5,
        ))
        session.commit()

    print("=== SEED OK ===")
    print(f"DEMO_ACCOUNT_NUMBER={number}")
    print("ADMIN=root / demo-admin-pass-123")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
