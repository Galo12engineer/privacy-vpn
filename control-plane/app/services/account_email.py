"""Management of the account's optional email: setting it (opt-in), removing it (unsubscribe)."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.models import Account
from app.services.email_crypto import decrypt_email, encrypt_email

_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


class InvalidEmail(Exception):
    pass


def set_email(
    session: Session, account: Account, email: str, *, opt_in: bool = True, key: str | None = None
) -> None:
    email = (email or "").strip()
    if not _EMAIL_RE.match(email):
        raise InvalidEmail(email)
    account.email_encrypted = encrypt_email(email, key=key)
    account.reminders_opt_in = opt_in


def clear_email(session: Session, account: Account) -> None:
    """Unsubscribe / minimization: remove the email and the consent."""
    account.email_encrypted = None
    account.reminders_opt_in = False
    account.reminder_3d_sent_at = None
    account.reminder_1d_sent_at = None


def get_email(account: Account, *, key: str | None = None) -> str | None:
    if not account.email_encrypted:
        return None
    return decrypt_email(account.email_encrypted, key=key)
