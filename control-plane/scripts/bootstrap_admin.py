#!/usr/bin/env python
"""Create the first admin from env (idempotent). Run inside the container:

    docker compose run --rm control-plane python scripts/bootstrap_admin.py

Env: CP_BOOTSTRAP_ADMIN_USER, CP_BOOTSTRAP_ADMIN_PASSWORD.
The password is NOT printed to the log. TOTP/2FA is enabled later from the admin console.
"""
from __future__ import annotations

import os
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AdminUser
from app.services.admin_auth import create_admin


def main() -> int:
    username = os.environ.get("CP_BOOTSTRAP_ADMIN_USER", "").strip()
    password = os.environ.get("CP_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not username or not password:
        print("ERROR: set CP_BOOTSTRAP_ADMIN_USER and CP_BOOTSTRAP_ADMIN_PASSWORD", file=sys.stderr)
        return 2
    if len(password) < 12:
        print("ERROR: admin password must be >= 12 chars", file=sys.stderr)
        return 2

    with SessionLocal() as session:
        existing = session.execute(
            select(AdminUser).where(AdminUser.username == username)
        ).scalar_one_or_none()
        if existing is not None:
            print(f"admin '{username}' already exists — nothing to do")
            return 0
        create_admin(session, username=username, password=password)
        session.commit()
        print(f"admin '{username}' created. Включи TOTP/2FA из админки следующим шагом.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
