import time

from app.security import signing


def test_sign_verify_roundtrip():
    body = b'{"public_key":"PUB"}'
    ts, sig = signing.sign("secret", body)
    assert signing.verify("secret", body, ts, sig)


def test_tampered_body_fails():
    ts, sig = signing.sign("secret", b"original")
    assert not signing.verify("secret", b"tampered", ts, sig)


def test_wrong_secret_fails():
    body = b"data"
    ts, sig = signing.sign("secret", body)
    assert not signing.verify("other", body, ts, sig)


def test_expired_timestamp_fails():
    body = b"data"
    old = int(time.time()) - 10_000
    ts, sig = signing.sign("secret", body, timestamp=old)
    assert not signing.verify("secret", body, ts, sig, window=300)


def test_missing_signature_fails():
    assert not signing.verify("secret", b"x", "123", "")


def test_signature_contract_golden_vector():
    # CONTRACT CP<->node: signing.py is duplicated across two repos. This golden vector is the
    # same in both — changing the signature format (separator/order/algorithm) in one will break
    # the test there, catching the silent drift that would otherwise take down the ENTIRE
    # add/remove/block peer dispatch.
    ts, mac = signing.sign("contract-v1-secret", b'{"public_key":"PK="}', timestamp=1700000000)
    assert ts == "1700000000"
    assert mac == "99c66d0539ab20145e650f5112eebf04875abc801fb5b085b0d64556a28f17e1"


def test_default_window_narrowed_for_state_changing_calls():
    # #22: it was 300s — a replayed add_peer/reattach/health within the 5-minute window passed as
    # valid. Narrowed to 60s (a balance against clock-skew on live nodes).
    assert signing.DEFAULT_WINDOW_SECONDS == 60


# --- #22 ReplayGuard ------------------------------------------------------------

def test_replay_guard_rejects_second_check_of_same_signature():
    guard = signing.ReplayGuard(window=60)
    assert guard.check("sig-a") is True   # first time — accept
    assert guard.check("sig-a") is False  # replay — reject
    assert guard.check("sig-b") is True   # a different signature — unaffected


def test_replay_guard_forgets_after_window_expires(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(signing.time, "time", lambda: now[0])
    guard = signing.ReplayGuard(window=10)
    assert guard.check("sig-a") is True
    now[0] += 11  # the window has passed
    assert guard.check("sig-a") is True  # TTL expired — no longer counted as a replay


def test_verify_with_replay_guard_rejects_exact_replay():
    # #22: a replayed (same ts+body+sig) add_peer/health would previously pass as valid
    # within the window — now the second identical request is rejected.
    guard = signing.ReplayGuard()
    body = b'{"public_key":"PUB"}'
    ts, sig = signing.sign("secret", body)
    assert signing.verify("secret", body, ts, sig, replay_guard=guard) is True
    assert signing.verify("secret", body, ts, sig, replay_guard=guard) is False


def test_verify_without_replay_guard_is_unaffected():
    # replay_guard is optional — calls without it (backward compatibility) do not break
    body = b"data"
    ts, sig = signing.sign("secret", body)
    assert signing.verify("secret", body, ts, sig) is True
    assert signing.verify("secret", body, ts, sig) is True  # without a guard the repeat is not tracked


def test_verify_replay_guard_not_consulted_on_bad_signature():
    # an invalid signature is rejected BEFORE the guard — we don't waste a TTL slot on junk requests
    guard = signing.ReplayGuard()
    body = b"data"
    ts, _ = signing.sign("secret", body)
    assert signing.verify("secret", body, ts, "deadbeef", replay_guard=guard) is False
    # a valid signature for the same (ts, body) still passes — the guard never saw it
    _, sig = signing.sign("secret", body, timestamp=int(ts))
    assert signing.verify("secret", body, ts, sig, replay_guard=guard) is True
