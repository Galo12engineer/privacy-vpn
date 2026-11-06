"""Regression: the client smoke builder (scripts/ru_smoke.py) MUST mirror the server.

Catches drift between vless:// link generation (services/vless_link.py), the server-side
Reality profile (services/reality_profile.py) and the smoke test's client config. If
someone changes the transport (e.g. network xhttp→tcp or adds flow), these tests will
fail before the operator burns an RU proxy on a bogus run.
"""

import sys
from pathlib import Path

import pytest

from app.services import reality_profile
from app.services.vless_link import to_vless_link

# scripts/ is not a package — add it to path, the same way the operator does when running it
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ru_smoke  # noqa: E402


class _Node:
    name = "nl-1"
    endpoint_host = "198.51.100.31"
    endpoint_port = 443
    location_name = "Amsterdam"
    reality_config = None


class _Peer:
    vless_uuid = "6f1e2d3c-aaaa-bbbb-cccc-0123456789ab"


def _node_with_profile():
    n = _Node()
    n.reality_config = reality_profile.dump_profile(
        reality_profile.generate_server_profile(
            dest="dl.google.com:443", server_names=["dl.google.com"]
        )
    )
    return n


def test_selftest_passes():
    assert ru_smoke._selftest() == 0


def test_client_config_mirrors_server_link_roundtrip():
    """The link the server hands to the client is parsed by the smoke into a consistent config."""
    node = _node_with_profile()
    profile = reality_profile.profile_from_node(node)
    link = to_vless_link(node, _Peer(), label="privacyvpn Amsterdam")

    lf = ru_smoke.parse_vless_link(link)
    cfg = ru_smoke.build_client_config(lf, ru_smoke.parse_socks("socks5://u:p@10.0.0.1:1080"))

    proxy = cfg["outbounds"][0]
    ss = proxy["streamSettings"]
    rs = ss["realitySettings"]

    # vnext = the node
    assert lf["uuid"] == _Peer.vless_uuid
    vnext = proxy["settings"]["vnext"][0]
    assert vnext["address"] == node.endpoint_host and vnext["port"] == node.endpoint_port
    assert vnext["users"][0]["encryption"] == "none" and "flow" not in vnext["users"][0]

    # client reality parameters == server profile
    assert ss["network"] == "xhttp"
    assert ss["security"] == "reality"
    assert rs["serverName"] == profile["server_names"][0]
    assert rs["publicKey"] == profile["public_key"]
    assert rs["shortId"] == profile["short_ids"][0]
    assert rs["fingerprint"] == profile["fingerprint"]
    assert ss["xhttpSettings"] == {"path": profile["path"], "mode": "auto"}

    # the handshake MUST go through the RU proxy (otherwise the test does not exercise TSPU/DPI)
    assert ss["sockopt"]["dialerProxy"] == "ru-socks"
    assert cfg["outbounds"][1]["tag"] == "ru-socks"
    assert cfg["outbounds"][1]["protocol"] == "socks"


def test_rejects_non_vless_and_non_reality():
    with pytest.raises(ru_smoke.SmokeError):
        ru_smoke.parse_vless_link("wireguard://x@h:51820")  # AmneziaWG/UDP — not tested here
    with pytest.raises(ru_smoke.SmokeError):
        ru_smoke.parse_vless_link("vless://x@h:443?security=tls&sni=a&pbk=b")  # not reality
    with pytest.raises(ru_smoke.SmokeError):
        ru_smoke.parse_vless_link("vless://x@h:443?security=reality&type=tcp&sni=a&pbk=b")  # not xhttp


def test_socks_auth_optional():
    lf = ru_smoke.parse_vless_link(to_vless_link(_node_with_profile(), _Peer()))
    with_auth = ru_smoke.build_client_config(lf, ru_smoke.parse_socks("socks5://u:p@1.2.3.4:1080"))
    no_auth = ru_smoke.build_client_config(lf, ru_smoke.parse_socks("1.2.3.4:1080"))
    assert with_auth["outbounds"][1]["settings"]["servers"][0]["users"][0] == {"user": "u", "pass": "p"}
    assert "users" not in no_auth["outbounds"][1]["settings"]["servers"][0]
