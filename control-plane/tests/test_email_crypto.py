import pytest
from cryptography.fernet import Fernet

from app.models import Account
from app.models.enums import AccountStatus
from app.services.account_email import InvalidEmail, clear_email, get_email, set_email
from app.services.email_crypto import decrypt_email, encrypt_email

KEY = Fernet.generate_key().decode()


def _account(session, suffix: str) -> Account:
    a = Account(account_number_hash=f"em{suffix}".ljust(64, "0"), status=AccountStatus.ACTIVE)
    session.add(a)
    session.commit()
    return a


def test_encrypt_decrypt_roundtrip():
    ct = encrypt_email("user@example.com", key=KEY)
    assert ct != b"user@example.com"
    assert decrypt_email(ct, key=KEY) == "user@example.com"


def test_set_and_get_email(session):
    a = _account(session, "1")
    set_email(session, a, "u@e.com", key=KEY)
    assert a.reminders_opt_in is True
    assert a.email_encrypted is not None
    assert get_email(a, key=KEY) == "u@e.com"


def test_clear_email_unsubscribes(session):
    a = _account(session, "2")
    set_email(session, a, "u@e.com", key=KEY)
    clear_email(session, a)
    assert a.email_encrypted is None
    assert a.reminders_opt_in is False
    assert get_email(a, key=KEY) is None


def test_invalid_email_rejected(session):
    a = _account(session, "3")
    with pytest.raises(InvalidEmail):
        set_email(session, a, "not-an-email", key=KEY)
