from app.models.enums import NodeProtocol
from app.services import admin_service, awg_profile, node_registration

_PUBKEY = "PUBKEY_44_CHARS_PLACEHOLDER_BASE64_XXXXXXXX="


def _add(session, name, subnet, *, protocol=NodeProtocol.WIREGUARD, port=None):
    kwargs = dict(
        name=name, endpoint_host=f"{name}.example.net", location_code="nl",
        location_name="NL", subnet_v4=subnet, dns_ip=subnet.replace("0/24", "1"),
        protocol=protocol,
    )
    if port is not None:
        kwargs["endpoint_port"] = port
    return admin_service.add_node(session, **kwargs)


def test_add_node_amneziawg_generates_valid_profile(session):
    node, _token = _add(session, "awg-a", "10.90.0.0/24", protocol=NodeProtocol.AMNEZIAWG, port=39000)
    profile = awg_profile.profile_from_node(node)
    assert profile is not None
    awg_profile.validate_profile(profile)  # must not raise
    assert node.protocol == NodeProtocol.AMNEZIAWG
    assert node.endpoint_port == 39000


def test_add_node_wireguard_has_no_profile(session):
    node, _token = _add(session, "wg-a", "10.91.0.0/24")
    assert node.protocol == NodeProtocol.WIREGUARD
    assert awg_profile.profile_from_node(node) is None


def test_register_response_carries_protocol_and_profile(session):
    _node, token = _add(session, "awg-r", "10.92.0.0/24", protocol=NodeProtocol.AMNEZIAWG, port=39001)
    out = node_registration.register_node(session, token=token, public_key=_PUBKEY)
    assert out["protocol"] == "amneziawg"
    assert out["amnezia"] is not None
    assert set(out["amnezia"]) == set(awg_profile.FIELDS)
    assert out["wg_listen_port"] == 39001  # the non-standard port reached the agent


def test_register_response_wireguard_amnezia_none(session):
    _node, token = _add(session, "wg-r", "10.93.0.0/24")
    out = node_registration.register_node(session, token=token, public_key=_PUBKEY)
    assert out["protocol"] == "wireguard"
    assert out["amnezia"] is None
