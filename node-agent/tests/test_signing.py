import time

from agent import signing


def test_roundtrip():
    body = b'{"public_key":"PUB"}'
    ts, sig = signing.sign("secret", body)
    assert signing.verify("secret", body, ts, sig)


def test_tamper_fails():
    ts, sig = signing.sign("secret", b"a")
    assert not signing.verify("secret", b"b", ts, sig)


def test_expired_fails():
    ts, sig = signing.sign("secret", b"a", timestamp=int(time.time()) - 9999)
    assert not signing.verify("secret", b"a", ts, sig, window=300)


def test_signature_contract_golden_vector():
    # CONTRACT CP<->node: the same golden vector lives in control-plane/tests/test_signing.py.
    # Changing the signature format in one repo will break the test there → we catch the drift
    # before it takes down the whole dispatch (add/remove/block peer from the node will stop
    # being applied on WireGuard).
    ts, mac = signing.sign("contract-v1-secret", b'{"public_key":"PK="}', timestamp=1700000000)
    assert ts == "1700000000"
    assert mac == "99c66d0539ab20145e650f5112eebf04875abc801fb5b085b0d64556a28f17e1"


def test_default_window_narrowed():
    assert signing.DEFAULT_WINDOW_SECONDS == 60  # #22: was 300


def test_replay_guard_rejects_second_check_of_same_signature():
    guard = signing.ReplayGuard(window=60)
    assert guard.check("sig-a") is True
    assert guard.check("sig-a") is False
    assert guard.check("sig-b") is True


def test_replay_guard_forgets_after_window_expires(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(signing.time, "time", lambda: now[0])
    guard = signing.ReplayGuard(window=10)
    assert guard.check("sig-a") is True
    now[0] += 11
    assert guard.check("sig-a") is True


def test_verify_with_replay_guard_rejects_exact_replay():
    # #22: a replayed add_peer/remove_peer (same ts+body+sig) within the window used to pass
    guard = signing.ReplayGuard()
    body = b'{"public_key":"PUB"}'
    ts, sig = signing.sign("secret", body)
    assert signing.verify("secret", body, ts, sig, replay_guard=guard) is True
    assert signing.verify("secret", body, ts, sig, replay_guard=guard) is False
