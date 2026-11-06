import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from agent import main as agent_main
from agent import signing
from agent.main import app, state
from agent.wg import WireGuard


class RecordingRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


@pytest.fixture()
def agent(monkeypatch):
    runner = RecordingRunner()
    state.agent_secret = "node-secret"
    state.wg = WireGuard("wg0", runner=runner)
    # #22 replay-guard fresh per test: the tests share one fixed secret "node-secret",
    # a process-wide guard across tests would yield false "replays" on a matching body+ts
    agent_main._replay_guard = signing.ReplayGuard()
    yield TestClient(app), runner
    state.agent_secret = None
    state.wg = None
    state.xray = None


def _signed(client, path, payload):
    body = json.dumps(payload).encode()
    ts, sig = signing.sign("node-secret", body)
    return client.post(
        path,
        content=body,
        headers={"Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": sig},
    )


def test_add_peer_applies_wg(agent):
    client, runner = agent
    resp = _signed(client, "/v1/peers", {"public_key": "PUB1", "allowed_ips": ["10.7.0.5/32"]})
    assert resp.status_code == 200
    assert runner.calls[-1] == [
        "wg", "set", "wg0", "peer", "PUB1", "allowed-ips", "10.7.0.5/32"
    ]


def test_remove_peer_applies_wg(agent):
    client, runner = agent
    resp = _signed(client, "/v1/peers/remove", {"public_key": "PUB1"})
    assert resp.status_code == 204
    assert runner.calls[-1] == ["wg", "set", "wg0", "peer", "PUB1", "remove"]


def test_add_peer_vless_calls_xray(agent):
    client, runner = agent
    from agent.xray import Xray
    state.xray = Xray(config_path="/run/xray/config.json", runner=runner)
    resp = _signed(client, "/v1/peers", {"protocol": "vless_reality", "uuid": "U1", "email": "p-1"})
    assert resp.status_code == 200
    assert runner.calls[-1][:4] == ["xray", "api", "adu", "--server=127.0.0.1:10085"]


def test_remove_peer_vless_calls_xray(agent):
    client, runner = agent
    from agent.xray import Xray
    state.xray = Xray(config_path="/run/xray/config.json", runner=runner)
    resp = _signed(client, "/v1/peers/remove", {"protocol": "vless_reality", "email": "p-1"})
    assert resp.status_code == 204
    assert runner.calls[-1] == [
        "xray", "api", "rmu", "--server=127.0.0.1:10085", "-tag=vless-in", "p-1"
    ]


def test_bad_signature_rejected(agent):
    client, runner = agent
    body = json.dumps({"public_key": "PUB1", "allowed_ips": ["10.7.0.5/32"]}).encode()
    resp = client.post(
        "/v1/peers",
        content=body,
        headers={"Content-Type": "application/json", "X-Timestamp": "123", "X-Signature": "bad"},
    )
    assert resp.status_code == 401
    assert runner.calls == []  # wg was not touched


def test_replayed_add_peer_rejected(agent):
    # #22: an add_peer intercepted and re-sent WITH THE SAME (ts, sig) would previously
    # pass as valid inside the window and restore a removed peer.
    client, runner = agent
    body = json.dumps({"public_key": "PUB1", "allowed_ips": ["10.7.0.5/32"]}).encode()
    ts, sig = signing.sign("node-secret", body)
    headers = {"Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": sig}

    first = client.post("/v1/peers", content=body, headers=headers)
    assert first.status_code == 200
    assert len(runner.calls) == 1

    replay = client.post("/v1/peers", content=body, headers=headers)
    assert replay.status_code == 401
    assert len(runner.calls) == 1  # wg NOT called again
