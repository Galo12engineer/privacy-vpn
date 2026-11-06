from cryptography.fernet import Fernet

from app.security import totp
from app.services import admin_auth, admin_service

KEY = Fernet.generate_key().decode()


def test_password_hash_and_verify():
    h = admin_auth.hash_password("s3cret")
    assert h.startswith("scrypt$")
    assert admin_auth.verify_password("s3cret", h)
    assert not admin_auth.verify_password("wrong", h)


def test_authenticate(session):
    admin_auth.create_admin(session, username="root", password="pw")
    session.commit()
    assert admin_auth.authenticate(session, username="root", password="pw") is not None
    assert admin_auth.authenticate(session, username="root", password="bad") is None


def test_authenticate_unknown_user_returns_none(session):
    # #32: a non-existent username → None (and the constant-work dummy-scrypt in this branch does not fail).
    admin_auth.create_admin(session, username="root", password="pw")
    session.commit()
    assert admin_auth.authenticate(session, username="ghost", password="pw") is None


def test_totp_2fa(session):
    admin = admin_auth.create_admin(session, username="a2", password="pw")
    session.commit()
    assert admin_auth.has_totp(admin) is False
    assert admin_auth.verify_totp(admin, "anything", key=KEY) is True  # not configured → skip

    secret = totp.generate_secret()
    admin_auth.enable_totp(session, admin, secret, key=KEY)
    session.commit()
    assert admin_auth.has_totp(admin) is True
    assert admin_auth.verify_totp(admin, totp.now_code(secret), key=KEY) is True
    assert admin_auth.verify_totp(admin, "000000", key=KEY) is False


def test_tariff_crud_is_data_driven(session):
    plan = admin_service.create_plan(
        session, code="1m", name="Month", duration_days=30, price_fiat_cents=500, device_limit=5
    )
    session.commit()
    assert plan.id is not None
    admin_service.update_plan(session, plan, price_fiat_cents=600, device_limit=7)
    session.commit()
    assert plan.price_fiat_cents == 600
    assert plan.device_limit == 7
    admin_service.update_plan(session, plan, is_active=False)
    session.commit()
    assert [p.code for p in admin_service.list_plans(session, active_only=True)] == []


def test_add_node_mints_token(session):
    node, token = admin_service.add_node(
        session,
        name="pa-9",
        endpoint_host="pa9.example.net",
        location_code="pa",
        location_name="Panama",
        subnet_v4="10.30.0.0/24",
        dns_ip="10.30.0.1",
    )
    session.commit()
    assert token  # one-time registration token
    assert node.registration_token_hash is not None
    assert node.id in [n.id for n in admin_service.list_nodes(session)]
