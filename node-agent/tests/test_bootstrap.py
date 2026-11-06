import subprocess

import httpx
import pytest

from agent import bootstrap as bmod
from agent.bootstrap import wg_bringup_commands
from agent.config import AgentSettings


def test_wg_bringup_commands_order():
    s = AgentSettings(wg_interface="wg0")
    cmds = wg_bringup_commands(
        s, server_address="10.7.0.1/24", listen_port=51820, private_key_path="/dev/shm/wg/private.key"
    )
    assert cmds == [
        ["ip", "link", "add", "dev", "wg0", "type", "wireguard"],
        ["wg", "set", "wg0", "listen-port", "51820", "private-key", "/dev/shm/wg/private.key"],
        ["ip", "addr", "add", "10.7.0.1/24", "dev", "wg0"],
        ["ip", "link", "set", "wg0", "up"],
    ]


class _State:
    node_id = agent_secret = tls_cert_path = tls_key_path = wg = None


def _mock_env(monkeypatch, tmp_path, *, identity):
    monkeypatch.setattr(bmod.store, "load_or_create_wg_key", lambda sd: ("PRIV", "PUB", str(tmp_path / "k")))
    monkeypatch.setattr(bmod.store, "load_identity", lambda sd: identity)
    monkeypatch.setattr(bmod.tls, "generate_self_signed", lambda: type("C", (), {"cert_pem": "CERT"})())
    monkeypatch.setattr(bmod.tls, "write_materials", lambda c: ("/tmp/cert", "/tmp/key"))
    # #13-extra: pinning opens a real TLS socket to the CP — in tests we don't hit the network
    monkeypatch.setattr(bmod.cp_pinning, "check_and_pin", lambda *a, **k: None)


def _runner(cmds):
    return lambda argv: cmds.append(list(argv)) or subprocess.CompletedProcess(list(argv), 0, "", "")


def _settings(tmp_path):
    return AgentSettings(state_dir=str(tmp_path), wan_interface="eth0", subnet_v4="10.7.0.0/24")


def test_bootstrap_first_boot_registers_and_persists(monkeypatch, tmp_path):
    _mock_env(monkeypatch, tmp_path, identity=None)  # no identity → first boot
    saved = {}
    monkeypatch.setattr(
        bmod.store, "save_identity",
        lambda sd, *, node_id, agent_secret: saved.update(node_id=node_id, agent_secret=agent_secret),
    )

    def handler(req):
        assert req.url.path.endswith("/v1/nodes/register")  # first boot → register
        return httpx.Response(200, json={
            "node_id": "N1", "agent_secret": "S1",
            "server_address": "10.7.0.1/24", "subnet_v4": "10.7.0.0/24",
        })

    cmds = []
    state = _State()
    reg = bmod.bootstrap(_settings(tmp_path), state,
                         runner=_runner(cmds), http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert reg["node_id"] == "N1"
    assert saved == {"node_id": "N1", "agent_secret": "S1"}  # identity persisted
    assert state.node_id == "N1" and state.agent_secret == "S1"
    assert ["ip", "link", "set", "wg0", "up"] in cmds  # wg brought up


def test_bootstrap_reboot_reattaches_and_reconciles_peers(monkeypatch, tmp_path):
    _mock_env(monkeypatch, tmp_path, identity={"node_id": "N1", "agent_secret": "S1"})  # identity present → reboot

    def _no_register(*a, **k):
        raise AssertionError("a reboot must not re-register with the token")

    monkeypatch.setattr(bmod.store, "save_identity", _no_register)

    def handler(req):
        assert req.url.path.endswith("/v1/nodes/N1/reattach")  # reboot → reattach
        return httpx.Response(200, json={
            "server_address": "10.7.0.1/24", "subnet_v4": "10.7.0.0/24",
            "peers": [{"public_key": "PK1", "allowed_ips": ["10.7.0.2/32"], "preshared_key": None}],
        })

    cmds = []
    state = _State()
    reg = bmod.bootstrap(_settings(tmp_path), state,
                         runner=_runner(cmds), http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert reg["node_id"] == "N1"  # set from identity
    # peer reconciliation: the active peer is added back onto wg
    assert any(c[:3] == ["wg", "set", "wg0"] and "PK1" in c for c in cmds)


def test_bootstrap_cp_down_serves_from_cache(monkeypatch, tmp_path):
    # #14: CP is unreachable at startup, but a last-known net-config exists → the data-plane comes up, the agent does NOT crash
    _mock_env(monkeypatch, tmp_path, identity={"node_id": "N1", "agent_secret": "S1"})
    bmod.store.save_config(str(tmp_path), server_address="10.7.0.1/24", subnet_v4="10.7.0.0/24")

    def handler(req):
        raise httpx.ConnectError("CP unreachable")

    cmds = []
    state = _State()
    reg = bmod.bootstrap(
        _settings(tmp_path), state, runner=_runner(cmds),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        enroll_attempts=2, enroll_backoff=0, sleep=lambda _: None,
    )
    assert ["ip", "link", "set", "wg0", "up"] in cmds  # wg brought up from the cache
    assert state.node_id == "N1"  # from identity, we didn't crash
    assert reg["peers"] == []  # peers will be restored on a successful reattach


def test_bootstrap_first_boot_cp_down_raises(monkeypatch, tmp_path):
    # first boot (no identity/cache) + CP down → there is nothing to bring up, systemd will restart us
    _mock_env(monkeypatch, tmp_path, identity=None)
    monkeypatch.setattr(bmod.store, "save_identity", lambda *a, **k: None)

    def handler(req):
        raise httpx.ConnectError("down")

    with pytest.raises(Exception):
        bmod.bootstrap(
            _settings(tmp_path), _State(), runner=_runner([]),
            http=httpx.Client(transport=httpx.MockTransport(handler)),
            enroll_attempts=1, enroll_backoff=0, sleep=lambda _: None,
        )


def test_cp_insecure_http_rejected(monkeypatch, tmp_path):
    # #13: clearnet http:// cp_base_url → refuse BEFORE enroll (the agent_secret won't leak to a MITM)
    _mock_env(monkeypatch, tmp_path, identity=None)
    s = AgentSettings(state_dir=str(tmp_path), wan_interface="eth0", subnet_v4="10.7.0.0/24",
                      cp_base_url="http://cp.example.net:8000")
    with pytest.raises(RuntimeError, match="insecure"):
        bmod.bootstrap(s, _State(), runner=_runner([]))


def test_cp_secure_urls_accepted():
    for url in ("https://privacyvpn.example.com", "http://abc.onion", "http://localhost:8000"):
        assert AgentSettings(cp_base_url=url).is_cp_secure()
    assert not AgentSettings(cp_base_url="http://1.2.3.4:8000").is_cp_secure()


def test_bootstrap_invokes_cp_cert_pinning(monkeypatch, tmp_path):
    # #13-extra: pinning is actually invoked on every bootstrap (not just mocked out in the helper)
    _mock_env(monkeypatch, tmp_path, identity=None)
    calls = []
    monkeypatch.setattr(
        bmod.cp_pinning, "check_and_pin",
        lambda state_dir, base_url, **k: calls.append((state_dir, base_url)),
    )

    def handler(req):
        return httpx.Response(200, json={
            "node_id": "N1", "agent_secret": "S1",
            "server_address": "10.7.0.1/24", "subnet_v4": "10.7.0.0/24",
        })

    s = _settings(tmp_path)
    bmod.bootstrap(s, _State(), runner=_runner([]),
                   http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert calls == [(s.state_dir, s.cp_base_url)]


def test_bootstrap_survives_cp_pinning_failure(monkeypatch, tmp_path):
    # the extra layer must not bring down bootstrap if pinning itself failed (e.g. a network failure)
    _mock_env(monkeypatch, tmp_path, identity=None)

    def boom(*a, **k):
        raise RuntimeError("dns broke")

    monkeypatch.setattr(bmod.cp_pinning, "check_and_pin", boom)

    def handler(req):
        return httpx.Response(200, json={
            "node_id": "N1", "agent_secret": "S1",
            "server_address": "10.7.0.1/24", "subnet_v4": "10.7.0.0/24",
        })

    reg = bmod.bootstrap(_settings(tmp_path), _State(), runner=_runner([]),
                         http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert reg["node_id"] == "N1"  # enroll went through anyway
