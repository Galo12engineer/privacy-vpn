#!/usr/bin/env python
"""Operator grants time WITHOUT crypto (test access / compensation / manual "cash" payment).

Credits N days to an account directly (time-credit stacks on top of the current expiry). Creates
a Subscription ledger row (price_plan_code='operator-grant', invoice_id=None) — the grant
is visible in history, but is not tied to a payment invoice. There is NO idempotency: every call
credits more (which is exactly what you want for "grant yourself 30 days").

    # new account (mints the number, prints it ONCE):
    CP_DATABASE_URL=... python scripts/grant_time.py --new --days 30
    # extend an existing one:
    CP_DATABASE_URL=... python scripts/grant_time.py --account 1234-5678-9012-3456 --days 30

Run with the CP COMMITTED; writes into the same DB (CP_DATABASE_URL). NOT for prod sales —
this is an operator tool for granting access without payment (see docs/deploy.md §6.2).
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Account
from app.models.base import as_utc
from app.services import admin_service
from app.services.account_number import hash_account_number, is_valid_format
from app.services.billing import apply_credit


def main() -> int:
    ap = argparse.ArgumentParser(description="Выдать время аккаунту без оплаты (оператор).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--new", action="store_true", help="создать новый аккаунт и начислить время")
    g.add_argument("--account", help="номер существующего аккаунта (продлить)")
    ap.add_argument("--days", type=int, required=True, help="сколько дней начислить")
    ap.add_argument("--device-limit", type=int, default=None, help="переопределить лимит устройств")
    ap.add_argument("--code", default="operator-grant", help="метка в ledger (Subscription.price_plan_code)")
    args = ap.parse_args()

    if args.days <= 0:
        ap.error("--days должно быть > 0")

    s = get_settings()
    now = datetime.now(UTC)
    number: str | None = None
    with SessionLocal() as session:
        if args.new:
            # same call the admin console makes (admin_service.create_account) — mint + credit in
            # one place, so the two operator paths cannot drift on what a new account gets
            account, number = admin_service.create_account(
                session,
                days=args.days,
                device_limit=args.device_limit,
                price_plan_code=args.code,
                now=now,
            )
        else:
            if not is_valid_format(args.account):
                ap.error("--account: неверный формат номера")
            h = hash_account_number(args.account, s.account_number_pepper)
            account = session.execute(
                select(Account).where(Account.account_number_hash == h)
            ).scalar_one_or_none()
            if account is None:
                print("ERROR: аккаунт с таким номером не найден")
                return 1
            apply_credit(
                session,
                account,
                days=args.days,
                now=now,
                price_plan_code=args.code,
                device_limit=args.device_limit,
            )
        session.commit()
        expires = as_utc(account.expires_at)

    print("=== GRANT OK ===")
    if number:
        print(f"ACCOUNT_NUMBER={number}")
    print(f"DAYS_GRANTED={args.days}")
    print(f"EXPIRES_AT={expires.strftime('%Y-%m-%d %H:%M UTC') if expires else '—'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
