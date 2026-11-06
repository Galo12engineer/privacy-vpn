"""C1 guard: the control-plane refuses to start with default secrets outside dev mode."""

import pytest

from app.config import Settings


def test_default_secrets_rejected_in_prod():
    s = Settings(
        dev_mode=False,
        session_secret="dev-only-insecure-session-secret-change-me",
        account_number_pepper="dev-only-insecure-pepper-change-me",
    )
    with pytest.raises(RuntimeError) as e:
        s.assert_secrets_safe()
    assert "CP_SESSION_SECRET" in str(e.value)
    assert "CP_ACCOUNT_NUMBER_PEPPER" in str(e.value)


def test_dev_mode_allows_defaults():
    # local/tests: default secrets are acceptable when CP_DEV_MODE=true
    Settings(dev_mode=True).assert_secrets_safe()


def test_real_secrets_pass():
    s = Settings(
        dev_mode=False,
        session_secret="x" * 48,
        account_number_pepper="y" * 48,
    )
    s.assert_secrets_safe()


def test_dev_fake_payments_rejected_in_prod():
    # M6: fake payments are forbidden outside dev mode even with real secrets
    s = Settings(
        dev_mode=False,
        session_secret="x" * 48,
        account_number_pepper="y" * 48,
        dev_fake_payments=True,
    )
    with pytest.raises(RuntimeError) as e:
        s.assert_secrets_safe()
    assert "DEV_FAKE_PAYMENTS" in str(e.value)


def test_dev_fake_payments_allowed_in_dev():
    Settings(dev_mode=True, dev_fake_payments=True).assert_secrets_safe()


def test_default_db_password_rejected_in_prod():
    # ops: default POSTGRES_PASSWORD=changeme in CP_DATABASE_URL → refuse to start
    s = Settings(
        dev_mode=False,
        session_secret="x" * 48,
        account_number_pepper="y" * 48,
        database_url="postgresql+psycopg://vpn:changeme@db:5432/vpn",
    )
    with pytest.raises(RuntimeError) as e:
        s.assert_secrets_safe()
    assert "POSTGRES_PASSWORD" in str(e.value)


def test_custom_db_password_passes():
    Settings(
        dev_mode=False,
        session_secret="x" * 48,
        account_number_pepper="y" * 48,
        database_url="postgresql+psycopg://vpn:" + "z" * 40 + "@db:5432/vpn",
    ).assert_secrets_safe()
