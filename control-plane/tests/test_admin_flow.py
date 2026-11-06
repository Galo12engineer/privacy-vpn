import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import PricePlan
from app.security import totp
from app.services import admin_auth


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False
    get_settings().email_encryption_key = Fernet.generate_key().decode()  # for TOTP encryption
    from app.webui import admin_flood_limiter, login_limiter

    login_limiter._buckets.clear()
    admin_flood_limiter._buckets.clear()

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_admin_login_and_create_plan(client, session):
    admin_auth.create_admin(session, username="root", password="pw")
    session.commit()

    login = client.post("/admin/login", data={"username": "root", "password": "pw", "totp": ""})
    assert login.status_code == 200
    assert login.url.path == "/admin"

    created = client.post(
        "/admin/plans",
        data={
            "code": "3m",
            "name": "Quarter",
            "duration_days": 90,
            "price_fiat_cents": 1200,
            "fiat_currency": "USD",
            "device_limit": 5,
            "sort_order": 1,
        },
    )
    assert created.status_code == 200
    assert session.query(PricePlan).filter_by(code="3m").count() == 1


def test_admin_bad_password_rejected(client, session):
    admin_auth.create_admin(session, username="root", password="pw")
    session.commit()
    resp = client.post("/admin/login", data={"username": "root", "password": "WRONG", "totp": ""})
    assert resp.status_code == 401


def test_admin_2fa_enforced(client, session):
    admin = admin_auth.create_admin(session, username="root2", password="pw")
    secret = totp.generate_secret()
    admin_auth.enable_totp(session, admin, secret)
    session.commit()

    no_code = client.post("/admin/login", data={"username": "root2", "password": "pw", "totp": ""})
    assert no_code.status_code == 401

    with_code = client.post(
        "/admin/login",
        data={"username": "root2", "password": "pw", "totp": totp.now_code(secret)},
    )
    assert with_code.status_code == 200


def test_admin_totp_code_not_reusable(client, session):
    # #23: the same TOTP code cannot be replayed (theft/shoulder-surfing/phishing)
    admin = admin_auth.create_admin(session, username="root3", password="pw")
    secret = totp.generate_secret()
    admin_auth.enable_totp(session, admin, secret)
    session.commit()
    code = totp.now_code(secret)
    ok = client.post("/admin/login", data={"username": "root3", "password": "pw", "totp": code})
    assert ok.status_code == 200
    replay = client.post("/admin/login", data={"username": "root3", "password": "pw", "totp": code})
    assert replay.status_code == 401  # counter already used


def test_admin_require_2fa_blocks_login_without_totp(client, session):
    # Phase 3: admin_require_2fa=True + the admin has no TOTP → login is closed (enroll first)
    from app.config import get_settings

    admin_auth.create_admin(session, username="root4", password="pw")
    session.commit()
    get_settings().admin_require_2fa = True
    try:
        r = client.post("/admin/login", data={"username": "root4", "password": "pw", "totp": ""})
        assert r.status_code == 401
    finally:
        get_settings().admin_require_2fa = False


def test_admin_password_only_ok_when_2fa_not_required(client, session):
    # default False (fail-safe against lockout) → password login without TOTP works
    admin_auth.create_admin(session, username="root5", password="pw")
    session.commit()
    r = client.post("/admin/login", data={"username": "root5", "password": "pw", "totp": ""})
    assert r.status_code == 200


def test_admin_add_node_shows_token(client, session):
    admin_auth.create_admin(session, username="root", password="pw")
    session.commit()
    client.post("/admin/login", data={"username": "root", "password": "pw", "totp": ""})
    resp = client.post(
        "/admin/nodes",
        data={
            "name": "is-2",
            "endpoint_host": "is2.example.net",
            "location_code": "is",
            "location_name": "Iceland",
            "subnet_v4": "10.40.0.0/24",
            "dns_ip": "10.40.0.1",
            "subnet_v6": "",
        },
    )
    assert resp.status_code == 200
    # the node is created and the token is shown once on the nodes page (?token=...)
    from app.models import Node

    assert session.query(Node).filter_by(name="is-2").count() == 1
    assert b"token" in resp.url.query  # redirect to /admin/nodes?token=...


def test_admin_routes_require_auth(client):
    resp = client.get("/admin/plans")
    assert resp.status_code == 200
    assert resp.url.path == "/admin/login"


def _login_admin(client, session):
    admin_auth.create_admin(session, username="root", password="pw")
    session.commit()
    client.post("/admin/login", data={"username": "root", "password": "pw", "totp": ""})


def test_block_peer_surfaces_dispatch_failure(client, session, node, active_account):
    """M4: if the node is unreachable, the block is applied in the DB, but the admin SEES a warning."""
    from app.services.provisioning import create_peer

    _login_admin(client, session)
    node.agent_secret = "node-secret"  # enable the dispatch branch
    node.agent_tls_cert = None         # https + no pinned cert → dispatch will fail visibly
    session.commit()
    peer, _ = create_peer(session, active_account, node, public_key="CLIENTPUB")
    session.commit()
    pid = peer.id

    resp = client.post(f"/admin/peers/{pid}/block")
    assert resp.status_code == 200
    assert "warn=node_unreachable" in str(resp.url)   # redirect with the flag
    assert "подтвердила" in resp.text                 # the banner is visible to the admin
    from app.models import Peer

    assert session.get(Peer, pid) is None             # the peer is removed in the DB anyway


def test_admin_login_rate_limited_per_username(client, session):
    """M7: password brute-force for one username is throttled per-username; another login is unaffected."""
    admin_auth.create_admin(session, username="root", password="pw")
    session.commit()
    for _ in range(5):
        r = client.post("/admin/login", data={"username": "root", "password": "x", "totp": ""})
        assert r.status_code == 401
    blocked = client.post("/admin/login", data={"username": "root", "password": "x", "totp": ""})
    assert blocked.status_code == 429
    # another username does not share a bucket with "root" → still gets through (401, not 429)
    other = client.post("/admin/login", data={"username": "other", "password": "x", "totp": ""})
    assert other.status_code == 401


def test_admin_session_revoked_on_password_change(client, session):
    """M5: changing the admin password invalidates old sessions."""
    admin = admin_auth.create_admin(session, username="root", password="pw")
    session.commit()
    client.post("/admin/login", data={"username": "root", "password": "pw", "totp": ""})
    assert client.get("/admin").url.path == "/admin"  # the session is valid

    admin.password_hash = admin_auth.hash_password("new-pw")  # the password was changed
    session.commit()

    revoked = client.get("/admin")  # the old token no longer authorizes
    assert revoked.url.path == "/admin/login"


def test_block_peer_no_dispatch_when_node_has_no_agent(client, session, node, active_account):
    """A node without agent_secret → we do not attempt dispatch, a normal redirect without warn."""
    from app.services.provisioning import create_peer

    _login_admin(client, session)
    assert node.agent_secret is None
    peer, _ = create_peer(session, active_account, node, public_key="CLIENTPUB2")
    session.commit()
    pid = peer.id

    resp = client.post(f"/admin/peers/{pid}/block")
    assert resp.status_code == 200
    assert "warn=" not in str(resp.url)
    from app.models import Peer

    assert session.get(Peer, pid) is None
