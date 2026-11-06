import json
import subprocess

from agent.xray import INBOUND_TAG, Xray, render_config

_REALITY = {
    "private_key": "PRIVKEY", "public_key": "PUBKEY", "short_ids": ["0123456789abcdef"],
    "server_names": ["www.example.com"], "dest": "www.example.com:443", "path": "/dl",
    "fingerprint": "firefox",
}


def _rec(calls):
    return lambda argv: calls.append(list(argv)) or subprocess.CompletedProcess(list(argv), 0, "", "")


def test_render_config_single_inbound_reality_xhttp():
    cfg = render_config(_REALITY, listen_port=443, users=[{"uuid": "U1", "email": "p-1"}])
    assert cfg["log"]["loglevel"] == "none"
    assert cfg["api"]["services"] == ["HandlerService"]  # StatsService is disabled (no per-user accounting)
    # api WITHOUT listen: the port is listened on by the dokodemo-door inbound (otherwise a double bind → crash on startup)
    assert "listen" not in cfg["api"]
    tags = {i.get("tag"): i for i in cfg["inbounds"]}
    assert set(tags) == {"api", INBOUND_TAG}  # exactly one VLESS inbound + api
    ib = tags[INBOUND_TAG]
    assert ib["listen"] == "0.0.0.0"
    assert ib["port"] == 443
    ss = ib["streamSettings"]
    # single inbound: xhttp + reality directly (without a tcp-fallback to @xhttp)
    assert ss["network"] == "xhttp"
    assert ss["security"] == "reality"
    rs = ss["realitySettings"]
    assert rs["privateKey"] == "PRIVKEY"
    assert rs["serverNames"] == ["www.example.com"]
    assert rs["shortIds"] == ["0123456789abcdef"]
    assert ss["xhttpSettings"]["path"] == "/dl"
    assert ib["settings"]["clients"] == [{"id": "U1", "email": "p-1", "level": 0}]
    # flow is absent (Vision is incompatible with XHTTP)
    assert "flow" not in json.dumps(cfg)


def test_add_user_command():
    calls = []
    x = Xray(config_path="/run/xray/config.json", runner=_rec(calls))
    x.add_user("UUID-1", "p-1")
    argv = calls[0]
    assert argv[:4] == ["xray", "api", "adu", "--server=127.0.0.1:10085"]
    assert argv[4].endswith(".json")  # temp adu config (already deleted after the call)


def test_remove_user_command():
    calls = []
    x = Xray(config_path="/run/xray/config.json", inbound_tag="vless-in", runner=_rec(calls))
    x.remove_user("p-1")
    assert calls[0] == ["xray", "api", "rmu", "--server=127.0.0.1:10085", "-tag=vless-in", "p-1"]


def test_write_config_atomic(tmp_path):
    path = str(tmp_path / "sub" / "config.json")
    Xray(config_path=path).write_config(render_config(_REALITY, listen_port=443, users=[]))
    with open(path) as f:
        assert json.load(f)["inbounds"]


def test_apply_xray_writes_config_and_restarts(tmp_path):
    from types import SimpleNamespace

    from agent import bootstrap
    from agent.config import AgentSettings

    calls = []
    settings = AgentSettings(
        state_dir=str(tmp_path), xray_config_path=str(tmp_path / "xray.json"), wg_listen_port=443
    )
    st = SimpleNamespace(xray=None)
    cfg = {
        "protocol": "vless_reality",
        "reality": _REALITY,
        "peers": [{"protocol": "vless_reality", "uuid": "U1", "email": "p-1"}],
    }
    bootstrap._apply_xray(settings, cfg, st, runner=_rec(calls))
    written = json.load(open(str(tmp_path / "xray.json")))
    clients = [
        c for i in written["inbounds"] if i.get("tag") == "vless-in" for c in i["settings"]["clients"]
    ]
    assert clients == [{"id": "U1", "email": "p-1", "level": 0}]
    assert ["systemctl", "restart", "xray"] in calls


def test_apply_xray_skips_non_vless(tmp_path):
    from types import SimpleNamespace

    from agent import bootstrap
    from agent.config import AgentSettings

    calls = []
    st = SimpleNamespace(xray=None)
    bootstrap._apply_xray(
        AgentSettings(state_dir=str(tmp_path)), {"protocol": "wireguard"}, st, runner=_rec(calls)
    )
    assert st.xray is None
    assert calls == []
