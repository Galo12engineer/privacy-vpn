import base64
from urllib.parse import parse_qs, urlparse

import pytest

from app.services import reality_profile
from app.services.vless_link import to_subscription, to_vless_link


class _Node:
    name = "PrivacyVPN NL"
    endpoint_host = "node1.example.net"
    endpoint_port = 443
    reality_config = None


class _Peer:
    vless_uuid = "b21f7279-e7d1-4635-8fb0-3d5657c31252"


def _node_with_profile(path="/dl"):
    n = _Node()
    n.reality_config = reality_profile.dump_profile(
        reality_profile.generate_server_profile(
            dest="www.example.com:443", server_names=["www.example.com"], path=path
        )
    )
    return n


def test_vless_link_has_all_reality_params():
    node = _node_with_profile()
    link = to_vless_link(node, _Peer())
    assert link.startswith(
        "vless://b21f7279-e7d1-4635-8fb0-3d5657c31252@node1.example.net:443?"
    )
    q = parse_qs(urlparse(link).query)
    assert q["security"] == ["reality"]
    assert q["type"] == ["xhttp"]
    assert q["encryption"] == ["none"]
    assert q["fp"] == ["firefox"]
    assert q["sni"] == ["www.example.com"]
    assert q["path"] == ["/dl"]
    assert q["pbk"] and q["sid"]
    assert "flow" not in q  # Vision is incompatible with XHTTP


def test_vless_link_requires_profile_and_uuid():
    with pytest.raises(ValueError):
        to_vless_link(_Node(), _Peer())  # a node without a reality_config

    node = _node_with_profile()

    class _NoUuid:
        vless_uuid = None

    with pytest.raises(ValueError):
        to_vless_link(node, _NoUuid())


def test_subscription_is_base64_of_links():
    node = _node_with_profile()
    link = to_vless_link(node, _Peer())
    sub = to_subscription([link])
    assert base64.b64decode(sub).decode() == link
