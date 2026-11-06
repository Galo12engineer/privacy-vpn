import json

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app
from app.models import Node
from app.models.enums import NodeStatus
from app.security import signing
from app.services.node_registration import mint_registration_token


@pytest.fixture()
def client(session):
    def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def provisioning_node(session):
    node = Node(
        name="reg-1",
        endpoint_host="reg1.example.net",
        location_code="pa",
        location_name="Panama",
        subnet_v4="10.9.0.0/24",
        dns_ip="10.9.0.1",
        status=NodeStatus.PROVISIONING,
    )
    token = mint_registration_token(node)
    session.add(node)
    session.commit()
    return node, token


def test_register_node_auto_registers(client, session, provisioning_node):
    node, token = provisioning_node
    resp = client.post(
        "/v1/nodes/register",
        json={"token": token, "public_key": "NODEPUBKEY="},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_secret"]
    assert data["server_address"] == "10.9.0.1/24"
    session.refresh(node)
    assert node.status == NodeStatus.ACTIVE
    assert node.available_for_sale is True


def test_registration_token_is_one_time(client, provisioning_node):
    node, token = provisioning_node
    first = client.post("/v1/nodes/register", json={"token": token, "public_key": "K1="})
    assert first.status_code == 200
    second = client.post("/v1/nodes/register", json={"token": token, "public_key": "K2="})
    assert second.status_code == 403  # token already used


def test_invalid_token_rejected(client):
    resp = client.post("/v1/nodes/register", json={"token": "nope", "public_key": "K="})
    assert resp.status_code == 403


def test_health_requires_valid_signature(client, session, provisioning_node):
    node, token = provisioning_node
    client.post("/v1/nodes/register", json={"token": token, "public_key": "K="})
    session.refresh(node)
    secret = node.agent_secret

    body = json.dumps({"cpu_pct": 12, "mem_pct": 40, "peers": 3}).encode()
    ts, sig = signing.sign(secret, body)
    ok = client.post(
        f"/v1/nodes/{node.id}/health",
        content=body,
        headers={"X-Timestamp": ts, "X-Signature": sig, "Content-Type": "application/json"},
    )
    assert ok.status_code == 204
    session.refresh(node)
    assert node.health_peers == 3
    assert node.last_health_at is not None

    bad = client.post(
        f"/v1/nodes/{node.id}/health",
        content=body,
        headers={"X-Timestamp": ts, "X-Signature": "deadbeef", "Content-Type": "application/json"},
    )
    assert bad.status_code == 401


def test_reattach_reauths_and_returns_active_peers(client, session, provisioning_node):
    # #1 (reboot-survival): the node comes back after a reboot, authenticating with its agent_secret
    # (without the one-time token), and receives the active peers to reconcile wg state.
    from app.models import Account, Peer
    from app.models.ip_lease import IpLease

    node, token = provisioning_node
    client.post("/v1/nodes/register", json={"token": token, "public_key": "OLDKEY="})
    session.refresh(node)
    secret = node.agent_secret

    acct = Account(account_number_hash="r".ljust(64, "0"), device_limit=5)
    session.add(acct)
    session.flush()
    lease = IpLease(node_id=node.id, address="10.9.0.2")
    session.add(lease)
    session.flush()
    session.add(Peer(account_id=acct.id, node_id=node.id, ip_lease_id=lease.id, public_key="CLIENTPK="))
    session.commit()

    body = json.dumps({"public_key": "OLDKEY=", "endpoint_port": 51820}).encode()
    ts, sig = signing.sign(secret, body)
    resp = client.post(
        f"/v1/nodes/{node.id}/reattach",
        content=body,
        headers={"X-Timestamp": ts, "X-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["server_address"] == "10.9.0.1/24"
    assert "CLIENTPK=" in [p["public_key"] for p in data["peers"]]  # peer reconciliation
    session.refresh(node)
    assert node.status == NodeStatus.ACTIVE

    bad = client.post(
        f"/v1/nodes/{node.id}/reattach",
        content=body,
        headers={"X-Timestamp": ts, "X-Signature": "deadbeef", "Content-Type": "application/json"},
    )
    assert bad.status_code == 401


def test_reattach_warns_on_endpoint_change(session, provisioning_node, caplog):
    # #18: an endpoint_host change on reattach is sensitive (agent_secret leak) → alert,
    # but the reattach itself goes through (a node reboot is legitimate).
    from app.services.node_registration import reattach_node

    node, _ = provisioning_node
    node.endpoint_host = "old.example.net"
    node.public_key = "PK="
    session.commit()
    with caplog.at_level("WARNING", logger="node_registration"):
        reattach_node(session, node, public_key="PK=", endpoint_host="new.evil.net")
    assert any("endpoint_host" in r.getMessage() for r in caplog.records)
    assert node.endpoint_host == "new.evil.net"


def test_replayed_health_request_rejected(client, session, provisioning_node):
    # #22: the same (ts, sig, body) sent a second time is a replay, not a fresh metrics push.
    node, token = provisioning_node
    client.post("/v1/nodes/register", json={"token": token, "public_key": "K="})
    session.refresh(node)
    secret = node.agent_secret

    body = json.dumps({"cpu_pct": 12, "mem_pct": 40, "peers": 3}).encode()
    ts, sig = signing.sign(secret, body)
    headers = {"X-Timestamp": ts, "X-Signature": sig, "Content-Type": "application/json"}

    first = client.post(f"/v1/nodes/{node.id}/health", content=body, headers=headers)
    assert first.status_code == 204

    replay = client.post(f"/v1/nodes/{node.id}/health", content=body, headers=headers)
    assert replay.status_code == 401


def test_health_revives_down_node(client, session, provisioning_node):
    from app.models.enums import NodeStatus

    node, token = provisioning_node
    client.post("/v1/nodes/register", json={"token": token, "public_key": "K="})
    session.refresh(node)
    secret = node.agent_secret
    node.status = NodeStatus.DOWN  # the node had gone down
    node.available_for_sale = False
    session.commit()

    body = json.dumps({"cpu_pct": 5, "mem_pct": 20, "peers": 1}).encode()
    ts, sig = signing.sign(secret, body)
    resp = client.post(
        f"/v1/nodes/{node.id}/health",
        content=body,
        headers={"X-Timestamp": ts, "X-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 204
    session.refresh(node)
    assert node.status == NodeStatus.ACTIVE  # back in service
    assert node.available_for_sale is True
