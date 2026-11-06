import subprocess

from agent import store
from agent.bootstrap import wg_bringup_commands
from agent.config import AgentSettings
from agent.wg import WireGuard, tool_for_protocol

_PROFILE = {
    "jc": 6, "jmin": 40, "jmax": 70, "s1": 88, "s2": 145,
    "h1": 1106457265, "h2": 249455488, "h3": 1209847463, "h4": 1646644382,
}


def _rec(calls):
    return lambda argv: calls.append(list(argv)) or subprocess.CompletedProcess(list(argv), 0, "", "")


def test_tool_for_protocol():
    assert tool_for_protocol("amneziawg") == "awg"
    assert tool_for_protocol("wireguard") == "wg"
    assert tool_for_protocol(None) == "wg"


def test_wireguard_uses_awg_tool_for_peers():
    calls = []
    wg = WireGuard("awg0", tool="awg", runner=_rec(calls))
    wg.add_peer("PUB", ["10.9.0.2/32"])
    wg.remove_peer("PUB")
    assert calls[0][:2] == ["awg", "set"]
    assert calls[1] == ["awg", "set", "awg0", "peer", "PUB", "remove"]


def test_bringup_amneziawg_commands():
    s = AgentSettings(wg_interface="awg0")
    cmds = wg_bringup_commands(
        s, server_address="10.9.0.1/24", listen_port=39000,
        private_key_path="/dev/shm/awg/private.key",
        protocol="amneziawg", amnezia=_PROFILE,
    )
    assert cmds[0] == ["modprobe", "amneziawg"]
    assert cmds[1] == ["ip", "link", "add", "dev", "awg0", "type", "amneziawg"]
    set_cmd = cmds[2]
    assert set_cmd[:3] == ["awg", "set", "awg0"]
    # inline obfuscation profile (tokens in lowercase)
    for key in ("jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4"):
        i = set_cmd.index(key)
        assert set_cmd[i + 1] == str(_PROFILE[key])
    assert cmds[-1] == ["ip", "link", "set", "awg0", "up"]


def test_bringup_wireguard_unchanged():
    s = AgentSettings(wg_interface="wg0")
    cmds = wg_bringup_commands(
        s, server_address="10.7.0.1/24", listen_port=51820,
        private_key_path="/dev/shm/wg/private.key",
    )
    flat = [tok for cmd in cmds for tok in cmd]
    assert "modprobe" not in flat
    assert "amneziawg" not in flat
    assert cmds[0] == ["ip", "link", "add", "dev", "wg0", "type", "wireguard"]
    assert cmds[1][0] == "wg"


def test_save_config_roundtrips_protocol_and_profile(tmp_path):
    store.save_config(
        str(tmp_path), server_address="10.9.0.1/24", subnet_v4="10.9.0.0/24",
        protocol="amneziawg", amnezia=_PROFILE,
    )
    cfg = store.load_config(str(tmp_path))
    assert cfg["protocol"] == "amneziawg"
    assert cfg["amnezia"] == _PROFILE


def test_save_config_defaults_wireguard(tmp_path):
    store.save_config(str(tmp_path), server_address="10.7.0.1/24", subnet_v4="10.7.0.0/24")
    cfg = store.load_config(str(tmp_path))
    assert cfg["protocol"] == "wireguard"
    assert cfg["amnezia"] is None
