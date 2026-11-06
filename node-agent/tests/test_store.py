"""Node persist: the WG key survives a reboot (pubkey is stable) + identity round-trip."""

import os
import subprocess

from agent import store


def _fake_wg(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[1] == "genkey":
            return subprocess.CompletedProcess(argv, 0, "PRIVKEY\n", "")
        if argv[1] == "pubkey":
            return subprocess.CompletedProcess(argv, 0, "PUBKEY\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(store.subprocess, "run", fake_run)
    return calls


def test_wg_key_created_then_reused(monkeypatch, tmp_path):
    calls = _fake_wg(monkeypatch)
    priv1, pub1, path = store.load_or_create_wg_key(str(tmp_path))
    assert priv1 == "PRIVKEY" and pub1 == "PUBKEY"
    assert os.path.exists(path)
    assert oct(os.stat(path).st_mode)[-3:] == "600"
    genkeys = sum(1 for c in calls if c[1] == "genkey")

    # second call (reboot emulation) — reads the same file, does NOT re-create the key
    priv2, pub2, _ = store.load_or_create_wg_key(str(tmp_path))
    assert priv2 == priv1
    assert sum(1 for c in calls if c[1] == "genkey") == genkeys  # pubkey is stable


def test_identity_roundtrip(tmp_path):
    assert store.load_identity(str(tmp_path)) is None
    store.save_identity(str(tmp_path), node_id="N1", agent_secret="S1")
    assert store.load_identity(str(tmp_path)) == {"node_id": "N1", "agent_secret": "S1"}
    path = os.path.join(str(tmp_path), "identity.json")
    assert oct(os.stat(path).st_mode)[-3:] == "600"


def test_corrupt_identity_degrades_to_none_not_crash(tmp_path):
    """A power cut during the write truncates identity.json. load_identity MUST return None
    (as on the first run), and not raise JSONDecodeError → otherwise bootstrap goes into an
    eternal restart-loop and the node is "bricked". Cf. load_config, which already degrades
    the same way."""
    path = os.path.join(str(tmp_path), "identity.json")
    for garbage in ("", '{"node_id": "N1", "agent_sec', "\x00\x00not json"):
        with open(path, "w") as f:
            f.write(garbage)
        assert store.load_identity(str(tmp_path)) is None  # does not raise


def test_saves_are_atomic_no_partial_file_on_crash(tmp_path, monkeypatch):
    """We simulate an fsync failure in the middle of the write: the target must not end up
    truncated, and no .tmp garbage must be left behind in the directory."""
    store.save_identity(str(tmp_path), node_id="N1", agent_secret="S1")

    def boom(_fd):
        raise OSError("disk died mid-write")

    monkeypatch.setattr(store.os, "fsync", boom)
    try:
        store.save_identity(str(tmp_path), node_id="N2", agent_secret="S2")
    except OSError:
        pass
    # the previous valid value survived (os.replace did not execute)
    assert store.load_identity(str(tmp_path)) == {"node_id": "N1", "agent_secret": "S1"}
    # the temporary file was cleaned up
    leftovers = [p for p in os.listdir(str(tmp_path)) if p.startswith(".tmp-")]
    assert leftovers == []
