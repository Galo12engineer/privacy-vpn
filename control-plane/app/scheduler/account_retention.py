"""PII retention (#26): wipe opt-in email/markers on long-expired accounts.

'Minimum data for the minimum time' is a selling point of the product. The encrypted email
and the reminder markers must not sit indefinitely on accounts that expired months ago.
We do NOT touch the account-number hash — a returning user must not lose access.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Account
from app.models.base import as_utc, utcnow


def purge_stale_account_pii(
    session: Session, *, now: datetime | None = None, retention_days: int | None = None
) -> int:
    """Null out email/opt-in/markers on accounts that expired > retention_days ago.

    Returns the number cleaned. retention_days<=0 → disabled (no-op).
    """
    now = now or utcnow()
    rd = retention_days if retention_days is not None else get_settings().account_retention_days
    if rd <= 0:
        return 0
    cutoff = now - timedelta(days=rd)
    # only those that still have something to clean (email or enabled reminders)
    accounts = session.execute(
        select(Account).where(
            Account.expires_at.is_not(None),
            (Account.email_encrypted.is_not(None)) | (Account.reminders_opt_in.is_(True)),
        )
    ).scalars().all()
    count = 0
    for a in accounts:
        exp = as_utc(a.expires_at)
        if exp is None or exp > cutoff:
            continue  # still active or expired recently — leave it alone
        a.email_encrypted = None
        a.reminders_opt_in = False
        a.reminder_3d_sent_at = None
        a.reminder_1d_sent_at = None
        count += 1
    return count
