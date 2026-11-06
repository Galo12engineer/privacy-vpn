"""Admin panel authentication: password (scrypt) + optional TOTP (2FA). No external dependencies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AdminUser
from app.security import totp
from app.services.email_crypto import decrypt_email, encrypt_email

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(dk_b64)),
        )
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def create_admin(session: Session, *, username: str, password: str) -> AdminUser:
    admin = AdminUser(username=username, password_hash=hash_password(password))
    session.add(admin)
    session.flush()
    return admin


# #32 constant-work: a dummy scrypt hash of the same cost (N,R,P), so that the "no such
# admin" branch spends as much time as verifying a real password. Otherwise a fast rejection
# on a non-existent username = a timing oracle for admin existence. Computed once at import.
_DUMMY_HASH = hash_password(base64.b64encode(os.urandom(24)).decode())


def authenticate(session: Session, *, username: str, password: str) -> AdminUser | None:
    admin = session.execute(
        select(AdminUser).where(AdminUser.username == username, AdminUser.is_active.is_(True))
    ).scalar_one_or_none()
    if admin is None:
        # run scrypt against the dummy hash (result ignored) — equalizes the response time.
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, admin.password_hash):
        return None
    return admin


def enable_totp(session: Session, admin: AdminUser, secret: str, *, key: str | None = None) -> None:
    admin.totp_secret_encrypted = encrypt_email(secret, key=key)


def has_totp(admin: AdminUser) -> bool:
    return admin.totp_secret_encrypted is not None


def verify_totp(admin: AdminUser, code: str, *, key: str | None = None) -> bool:
    if admin.totp_secret_encrypted is None:
        return True  # 2FA is not configured — the step is skipped
    secret = decrypt_email(admin.totp_secret_encrypted, key=key)
    matched = totp.matched_counter(secret, code)
    if matched is None:
        return False
    # #23 anti-replay: the same code (the same counter) is not accepted a second time
    # (theft/shoulder-surfing/phishing). Requires a strictly increasing counter; on success we
    # record it (the caller commits the session).
    if admin.last_totp_counter is not None and matched <= admin.last_totp_counter:
        return False
    admin.last_totp_counter = matched
    return True
