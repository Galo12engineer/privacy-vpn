"""Encryption of the optional email (Fernet). The key lives in the secret-store, not in the DB/repo.

Email is the only PII, and even that is optional (only for reminders). We encrypt it at-rest
and allow deletion (data minimization).
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from app.config import get_settings


class EmailEncryptionDisabled(Exception):
    pass


def _fernet(key: str | None = None) -> Fernet:
    key = key or get_settings().email_encryption_key
    if not key:
        raise EmailEncryptionDisabled("CP_EMAIL_ENCRYPTION_KEY not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_email(plaintext: str, *, key: str | None = None) -> bytes:
    return _fernet(key).encrypt(plaintext.encode())


def decrypt_email(ciphertext: bytes, *, key: str | None = None) -> str:
    return _fernet(key).decrypt(ciphertext).decode()
