"""Persist node state across reboots (operator's choice: persist to disk).

We store ONLY the node's own identity — the server's WG private key + (node_id,
agent_secret). Client keys, logs and PII are not here and never will be (browser keygen,
no-log). Persist makes the node reboot-survivable: the server pubkey stays stable (client
configs don't break), and agent_secret allows re-authenticating to the CP without a one-time
token. Files 0600, directory 0700 (on an FDE disk, if there is one).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile


def _ensure_dir(state_dir: str) -> None:
    os.makedirs(state_dir, exist_ok=True)
    os.chmod(state_dir, 0o700)


def _atomic_write(path: str, text: str) -> None:
    """Write a 0600 file atomically: a power loss/SIGKILL won't leave a truncated file.

    We write to a temporary file IN THE SAME directory (same fs → os.replace is atomic), fsync
    it, then replace over the target. Otherwise a half-written identity.json/… would crash load
    on the next start (see load_identity, the same pattern as in wg.py/nft.py)."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_or_create_wg_key(state_dir: str) -> tuple[str, str, str]:
    """Return (private_key, public_key, key_path). The key survives a reboot → pubkey is stable."""
    _ensure_dir(state_dir)
    path = os.path.join(state_dir, "wg-private.key")
    if os.path.exists(path):
        with open(path) as f:
            priv = f.read().strip()
    else:
        priv = subprocess.run(
            ["wg", "genkey"], capture_output=True, text=True, check=True
        ).stdout.strip()
        _atomic_write(path, priv)
    pub = subprocess.run(
        ["wg", "pubkey"], input=priv, capture_output=True, text=True, check=True
    ).stdout.strip()
    return priv, pub, path


def load_identity(state_dir: str) -> dict | None:
    """(node_id, agent_secret) from the previous registration — or None if the node hasn't registered yet."""
    path = os.path.join(state_dir, "identity.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (ValueError, OSError):
        # corrupt/truncated identity (power loss during a write) → treat as a first run: the agent
        # does NOT crash into an eternal restart-loop, it brings the data-plane up from cache and re-registers.
        return None


def save_identity(state_dir: str, *, node_id: str, agent_secret: str) -> None:
    _ensure_dir(state_dir)
    path = os.path.join(state_dir, "identity.json")
    _atomic_write(path, json.dumps({"node_id": node_id, "agent_secret": agent_secret}))


def load_config(state_dir: str) -> dict | None:
    """Last-known NETWORK config of the node (server_address/subnet) — for data-plane-first
    bringup (#14): bring wg+nft up from cache if the CP is unreachable at start. Client peers are
    NOT here (storing someone else's pubkey on disk = violating no-client-data) — peers are
    restored via reattach."""
    path = os.path.join(state_dir, "node-config.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (ValueError, OSError):
        return None  # corrupt cache → as if it doesn't exist (don't crash the agent)


def save_config(
    state_dir: str,
    *,
    server_address: str,
    subnet_v4: str,
    subnet_v6: str | None = None,
    protocol: str = "wireguard",
    amnezia: dict | None = None,
    reality: dict | None = None,
) -> None:
    # protocol/amnezia/reality — this is the CONFIG OF THE NODE ITSELF (transport + parameters), not
    # client data: they are needed for data-plane-first bringup when the CP is unreachable (AmneziaWG
    # can't be brought up without a profile; Xray can't be configured without a reality profile). The list
    # of clients (WG peers / VLESS users) is NOT stored here — it arrives via reattach.
    _ensure_dir(state_dir)
    path = os.path.join(state_dir, "node-config.json")
    _atomic_write(
        path,
        json.dumps({
            "server_address": server_address,
            "subnet_v4": subnet_v4,
            "subnet_v6": subnet_v6,
            "protocol": protocol,
            "amnezia": amnezia,
            "reality": reality,
        }),
    )


def load_pinned_cp_cert(state_dir: str) -> str | None:
    """SHA-256 fingerprint of the CP's TLS cert, pinned on first contact (#13-extra TOFU)."""
    path = os.path.join(state_dir, "cp-cert.pin")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read().strip() or None


def save_pinned_cp_cert(state_dir: str, fingerprint: str) -> None:
    _ensure_dir(state_dir)
    path = os.path.join(state_dir, "cp-cert.pin")
    _atomic_write(path, fingerprint)
