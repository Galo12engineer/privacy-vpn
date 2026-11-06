#!/usr/bin/env python
"""Enable TOTP/2FA for an admin. Run inside the container:

    docker compose exec control-plane python scripts/enroll_admin_totp.py <username>

Prints the otpauth:// URI and the base32 secret — add it to an authenticator (Aegis / andOTP /
Google Authenticator). Then set CP_ADMIN_REQUIRE_2FA=true in .env and
`docker compose up -d control-plane` → admin login becomes 2FA-mandatory.
Requires CP_EMAIL_ENCRYPTION_KEY (the TOTP secret is Fernet-encrypted at-rest). Idempotently
overwrites the secret (the old code stops working).
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AdminUser
from app.security import totp
from app.services import admin_auth


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("usage: enroll_admin_totp.py <username>", file=sys.stderr)
        return 2
    username = sys.argv[1].strip()
    with SessionLocal() as session:
        admin = session.execute(
            select(AdminUser).where(AdminUser.username == username)
        ).scalar_one_or_none()
        if admin is None:
            print(f"ERROR: admin '{username}' не найден", file=sys.stderr)
            return 1
        secret = totp.generate_secret()
        admin_auth.enable_totp(session, admin, secret)  # Fernet cipher; requires CP_EMAIL_ENCRYPTION_KEY
        session.commit()
        uri = totp.provisioning_uri(secret, account=username, issuer="privacyvpn")
        print(f"TOTP включён для '{username}'. Добавь в authenticator:")
        print(f"  secret: {secret}")
        print(f"  uri:    {uri}")
        print("Затем: CP_ADMIN_REQUIRE_2FA=true в .env → docker compose up -d control-plane")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
