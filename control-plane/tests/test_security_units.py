import uuid

from app.security import sessions, totp
from app.security.rate_limit import RateLimiter, client_key


# --- sessions ---
def test_session_roundtrip():
    aid = uuid.uuid4()
    token = sessions.issue(aid, scope="cabinet")
    assert sessions.verify(token, scope="cabinet") == aid


def test_session_wrong_scope_rejected():
    token = sessions.issue(uuid.uuid4(), scope="cabinet")
    assert sessions.verify(token, scope="admin") is None


def test_session_expired_rejected():
    aid = uuid.uuid4()
    token = sessions.issue(aid, scope="cabinet", now=1000, ttl=10)
    assert sessions.verify(token, scope="cabinet", now=2000) is None


def test_session_tamper_rejected():
    token = sessions.issue(uuid.uuid4(), scope="cabinet")
    assert sessions.verify(token[:-2] + "xx", scope="cabinet") is None


def test_session_binding_must_match():
    # M5: a token with a binding is accepted only when the binding matches
    aid = uuid.uuid4()
    token = sessions.issue(aid, scope="admin", binding="stamp-A")
    assert sessions.verify(token, scope="admin", binding="stamp-A") == aid
    assert sessions.verify(token, scope="admin", binding="stamp-B") is None  # revocation


def test_session_read_returns_payload():
    aid = uuid.uuid4()
    token = sessions.issue(aid, scope="admin", binding="x")
    data = sessions.read(token)
    assert data["sub"] == str(aid) and data["scope"] == "admin" and data["bnd"] == "x"


# --- rate limit ---
def test_rate_limiter_blocks_after_max():
    rl = RateLimiter(max_events=3, per_seconds=60)
    k = "k"
    assert all(rl.allow(k, now=100) for _ in range(3))
    assert rl.allow(k, now=100) is False


def test_rate_limiter_window_recovers():
    rl = RateLimiter(max_events=2, per_seconds=60)
    rl.allow("k", now=0)
    rl.allow("k", now=0)
    assert rl.allow("k", now=0) is False
    assert rl.allow("k", now=120) is True  # the window has shifted


def test_client_key_does_not_store_ip():
    k = client_key("203.0.113.5", "login")
    assert "203.0.113.5" not in k  # a hash, not the IP itself
    assert len(k) == 64


# --- TOTP ---
def test_totp_verify_current_code():
    secret = totp.generate_secret()
    t = 1_700_000_000
    code = totp.now_code(secret, t=t)
    assert totp.verify(secret, code, t=t)


def test_totp_rejects_wrong_code():
    secret = totp.generate_secret()
    assert not totp.verify(secret, "000000", t=1_700_000_000)


def test_totp_window_tolerates_drift():
    secret = totp.generate_secret()
    t = 1_700_000_000
    prev = totp.now_code(secret, t=t - 30)
    assert totp.verify(secret, prev, t=t, window=1)
