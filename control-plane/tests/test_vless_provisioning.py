import pytest

from app.models.enums import NodeProtocol
from app.services import admin_service, node_registration, reality_profile
from app.services.provisioning import DeviceLimitReached, create_vless_peer

_PUBKEY = "PUBKEY_44_CHARS_PLACEHOLDER_BASE64_XXXXXXXX="


def _add_vless_node(session, name="rl-1", subnet="10.50.0.0/24"):
    return admin_service.add_node(
        session, name=name, endpoint_host=f"{name}.example.net", location_code="nl",
        location_name="NL", subnet_v4=subnet, dns_ip=subnet.replace("0/24", "1"),
        endpoint_port=443, protocol=NodeProtocol.VLESS_REALITY,
        reality_dest="www.example.com:443", reality_server_names=["www.example.com"],
    )


def test_add_vless_node_generates_valid_reality_profile(session):
    node, _token = _add_vless_node(session)
    assert node.protocol == NodeProtocol.VLESS_REALITY
    profile = reality_profile.profile_from_node(node)
    assert profile is not None
    reality_profile.validate_server_profile(profile)  # does not raise
    assert profile["server_names"] == ["www.example.com"]
    assert profile["dest"] == "www.example.com:443"
    assert profile["fingerprint"] == "firefox"


def test_add_vless_node_validate_dest_calls_checker(session, monkeypatch):
    # validate_dest=True → add_node calls the network check_dest (we mock it, no network)
    from app.services import admin_service

    called = {}
    monkeypatch.setattr(admin_service, "check_dest", lambda d, **k: called.setdefault("dest", d))
    admin_service.add_node(
        session, name="rl-vd", endpoint_host="vd.example.net", location_code="nl",
        location_name="NL", subnet_v4="10.57.0.0/24", dns_ip="10.57.0.1", endpoint_port=443,
        protocol=NodeProtocol.VLESS_REALITY, reality_dest="good.example:443",
        reality_server_names=["good.example"], validate_dest=True,
    )
    assert called["dest"] == "good.example:443"


def test_add_vless_node_default_skips_dest_check(session, monkeypatch):
    # default validate_dest=False → we do NOT go to the network (important for tests/CI)
    from app.services import admin_service

    monkeypatch.setattr(admin_service, "check_dest", lambda *a, **k: pytest.fail("не должно вызываться"))
    _add_vless_node(session, name="rl-nocheck", subnet="10.58.0.0/24")  # validate_dest not passed


def test_add_vless_node_requires_dest_and_sni(session):
    with pytest.raises(ValueError):
        admin_service.add_node(
            session, name="rl-bad", endpoint_host="x.example.net", location_code="nl",
            location_name="NL", subnet_v4="10.51.0.0/24", dns_ip="10.51.0.1",
            protocol=NodeProtocol.VLESS_REALITY,  # without dest/server_names
        )


def test_register_response_carries_reality(session):
    _node, token = _add_vless_node(session, name="rl-reg", subnet="10.52.0.0/24")
    out = node_registration.register_node(session, token=token, public_key=_PUBKEY)
    assert out["protocol"] == "vless_reality"
    assert out["reality"] is not None
    assert out["amnezia"] is None
    assert "private_key" in out["reality"]


def test_create_vless_peer_generates_identifiers_no_lease(session, active_account):
    node, _t = _add_vless_node(session, name="rl-peer", subnet="10.53.0.0/24")
    peer, client = create_vless_peer(session, active_account, node)
    assert peer.protocol_type == NodeProtocol.VLESS_REALITY
    assert peer.vless_uuid == client["uuid"]
    assert peer.vless_email == client["email"]
    assert peer.ip_lease_id is None   # no IPAM
    assert peer.public_key is None    # no WG key


def test_create_vless_peer_respects_device_limit(session, active_account):
    node, _t = _add_vless_node(session, name="rl-lim", subnet="10.54.0.0/24")
    active_account.device_limit = 1
    create_vless_peer(session, active_account, node)
    with pytest.raises(DeviceLimitReached):
        create_vless_peer(session, active_account, node)


def test_reattach_returns_vless_users(session, active_account):
    node, token = _add_vless_node(session, name="rl-rt", subnet="10.55.0.0/24")
    node_registration.register_node(session, token=token, public_key=_PUBKEY)
    peer, _c = create_vless_peer(session, active_account, node)
    out = node_registration.reattach_node(session, node)
    users = [p for p in out["peers"] if p.get("protocol") == "vless_reality"]
    assert len(users) == 1
    assert users[0]["uuid"] == peer.vless_uuid
    assert users[0]["email"] == peer.vless_email


def test_node_client_dispatch_vless(session, active_account, monkeypatch):
    from app.services import node_client

    node, _t = _add_vless_node(session, name="rl-nc", subnet="10.56.0.0/24")
    peer, _c = create_vless_peer(session, active_account, node)
    captured = {}
    monkeypatch.setattr(
        node_client, "_signed_post",
        lambda node, path, payload, **kw: captured.update(path=path, payload=payload),
    )
    node_client.dispatch_add_peer(node, peer, None)
    assert captured["path"] == "/v1/peers"
    assert captured["payload"] == {
        "protocol": "vless_reality", "uuid": peer.vless_uuid, "email": peer.vless_email
    }
    node_client.dispatch_remove_peer(node, protocol="vless_reality", email=peer.vless_email)
    assert captured["payload"] == {"protocol": "vless_reality", "email": peer.vless_email}
