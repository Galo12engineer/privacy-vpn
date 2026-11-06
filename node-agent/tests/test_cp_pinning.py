"""#13-extra: TOFU pinning of the CP TLS cert. fetch_fingerprint is injected — no real network."""

from agent import cp_pinning, store


def test_fetch_fingerprint_skips_non_https():
    assert cp_pinning.fetch_fingerprint("http://cp.example.net") is None
    assert cp_pinning.fetch_fingerprint("http://abc.onion") is None


def test_fetch_fingerprint_skips_missing_host():
    assert cp_pinning.fetch_fingerprint("https://") is None


def test_first_contact_pins(tmp_path):
    fetch = lambda base_url: "fp-aaa"  # noqa: E731
    cp_pinning.check_and_pin(str(tmp_path), "https://cp.example.net", fetch=fetch)
    assert store.load_pinned_cp_cert(str(tmp_path)) == "fp-aaa"


def test_matching_fingerprint_stays_pinned(tmp_path, caplog):
    store.save_pinned_cp_cert(str(tmp_path), "fp-aaa")
    fetch = lambda base_url: "fp-aaa"  # noqa: E731
    with caplog.at_level("WARNING", logger="agent.cp_pinning"):
        cp_pinning.check_and_pin(str(tmp_path), "https://cp.example.net", fetch=fetch)
    assert store.load_pinned_cp_cert(str(tmp_path)) == "fp-aaa"
    assert not [r for r in caplog.records if r.name == "agent.cp_pinning"]  # no noise


def test_changed_fingerprint_warns_but_repins(tmp_path, caplog):
    store.save_pinned_cp_cert(str(tmp_path), "fp-old")
    fetch = lambda base_url: "fp-new"  # noqa: E731
    with caplog.at_level("WARNING", logger="agent.cp_pinning"):
        cp_pinning.check_and_pin(str(tmp_path), "https://cp.example.net", fetch=fetch)
    assert any("changed" in r.getMessage() for r in caplog.records)
    # ratchet forward — the channel is NOT blocked, the new fingerprint becomes the current pin
    assert store.load_pinned_cp_cert(str(tmp_path)) == "fp-new"


def test_fetch_failure_does_not_touch_pin(tmp_path):
    store.save_pinned_cp_cert(str(tmp_path), "fp-aaa")
    fetch = lambda base_url: None  # noqa: E731 — network failure/non-https
    cp_pinning.check_and_pin(str(tmp_path), "https://cp.example.net", fetch=fetch)
    assert store.load_pinned_cp_cert(str(tmp_path)) == "fp-aaa"  # untouched


def test_pin_roundtrip_permissions(tmp_path):
    import os

    assert store.load_pinned_cp_cert(str(tmp_path)) is None
    store.save_pinned_cp_cert(str(tmp_path), "fp-xyz")
    path = os.path.join(str(tmp_path), "cp-cert.pin")
    assert oct(os.stat(path).st_mode)[-3:] == "600"
