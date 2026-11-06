"""Opt-in email reminders about expiry, 3 days and 1 day ahead. An anonymous user without an email gets only the banner."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account
from app.models.base import as_utc, utcnow
from app.services.email_crypto import decrypt_email
from app.services.emailer import Emailer

log = logging.getLogger("reminders")


def _message(email: str, *, days: int, exp: datetime) -> dict:
    when = exp.strftime("%Y-%m-%d %H:%M UTC")
    subject = f"VPN: подписка истекает через {days} дн. / expires in {days} day(s)"
    body = (
        f"RU: Ваша подписка истекает {when} (через {days} дн.).\n"
        f"Продлите в кабинете в пару кликов. Восстановления account-number нет — храните токен.\n\n"
        f"EN: Your subscription expires {when} (in {days} day(s)).\n"
        f"Renew in your cabinet in a couple of clicks. There is no account-number recovery — keep your token.\n"
    )
    return {"to": email, "subject": subject, "body": body}


def send_due_reminders(
    session: Session, *, emailer: Emailer, now: datetime | None = None, key: str | None = None
) -> int:
    """Send out due reminders. Returns the number of emails sent."""
    now = now or utcnow()
    sent = 0
    accounts = session.execute(
        select(Account).where(
            Account.reminders_opt_in.is_(True),
            Account.email_encrypted.is_not(None),
            Account.expires_at.is_not(None),
        )
    ).scalars().all()
    for account in accounts:
        exp = as_utc(account.expires_at)
        if exp is None or now >= exp:
            continue  # already expired — that's the banner, not a reminder
        # per-account isolation: a single undecryptable (key rotation) or SMTP-recipient-rejected
        # row MUST NOT bring down the whole batch (otherwise _job rolls back the already
        # set *_sent_at → duplicate emails + the remaining accounts get skipped).
        try:
            email = decrypt_email(account.email_encrypted, key=key)
            # the windows are mutually exclusive: the 3-day reminder only BEFORE entering the
            # 1-day window. Otherwise a short plan (< 3 days) is, at purchase time, already past
            # the threshold of both → it would send both emails at once. Prod plans of
            # 30/90/365 days are unaffected (their windows are separated by days).
            if (
                exp - timedelta(days=3) <= now < exp - timedelta(days=1)
                and account.reminder_3d_sent_at is None
            ):
                emailer.send(**_message(email, days=3, exp=exp))
                account.reminder_3d_sent_at = now
                sent += 1
            if now >= exp - timedelta(days=1) and account.reminder_1d_sent_at is None:
                emailer.send(**_message(email, days=1, exp=exp))
                account.reminder_1d_sent_at = now
                sent += 1
        except Exception as e:  # noqa: BLE001 — per-account isolation
            log.warning("reminder skipped for one account: %s", type(e).__name__)
    return sent
