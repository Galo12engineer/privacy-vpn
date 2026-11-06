"""#26: PII-retention — long-expired accounts lose email/opt-in, the number hash remains."""

from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet

from app.models import Account
from app.models.enums import AccountStatus
from app.scheduler.account_retention import purge_stale_account_pii
from app.services.account_email import set_email

KEY = Fernet.generate_key().decode()
NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _acct(session, suffix, expires_at, *, email=True) -> Account:
    a = Account(
        account_number_hash=f"ar{suffix}".ljust(64, "0"),
        status=AccountStatus.EXPIRED,
        expires_at=expires_at,
    )
    session.add(a)
    session.commit()
    if email:
        set_email(session, a, f"u{suffix}@e.com", opt_in=True, key=KEY)
        session.commit()
    return a


def test_purges_pii_of_long_expired(session):
    a = _acct(session, "old", NOW - timedelta(days=120))
    n = purge_stale_account_pii(session, now=NOW, retention_days=90)
    assert n == 1
    assert a.email_encrypted is None
    assert a.reminders_opt_in is False


def test_keeps_recent_and_active(session):
    recent = _acct(session, "recent", NOW - timedelta(days=10))
    active = _acct(session, "active", NOW + timedelta(days=30))
    n = purge_stale_account_pii(session, now=NOW, retention_days=90)
    assert n == 0
    assert recent.email_encrypted is not None
    assert active.email_encrypted is not None


def test_account_hash_preserved(session):
    a = _acct(session, "hash", NOW - timedelta(days=200))
    h = a.account_number_hash
    purge_stale_account_pii(session, now=NOW, retention_days=90)
    assert a.account_number_hash == h  # a returning user is not locked out


def test_disabled_when_retention_zero(session):
    a = _acct(session, "off", NOW - timedelta(days=500))
    assert purge_stale_account_pii(session, now=NOW, retention_days=0) == 0
    assert a.email_encrypted is not None
