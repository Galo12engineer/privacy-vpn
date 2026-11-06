from app.services.config_builder import (
    IPV6_NULLROUTE_PREFIX,
    PRIVATE_KEY_PLACEHOLDER,
    build_peer_config,
    render_conf,
    server_allowed_ips,
    to_browser_payload,
)
from app.services.ipam import allocate


def _set_pubkey(node):
    node.public_key = "SERVER_PUBKEY_BASE64_44_CHARS_PLACEHOLDER="
    return node


def test_config_never_contains_private_key(session, node):
    _set_pubkey(node)
    lease = allocate(session, node)
    cfg = build_peer_config(node, lease)
    conf = render_conf(cfg)
    # the private key is only a placeholder, there is no real key
    assert f"PrivateKey = {PRIVATE_KEY_PLACEHOLDER}" in conf
    assert "private_key" not in to_browser_payload(cfg)
    assert cfg.server_public_key == node.public_key


def test_full_tunnel_allowed_ips(session, node):
    lease = allocate(session, node)
    cfg = build_peer_config(node, lease)
    assert cfg.allowed_ips == ["0.0.0.0/0", "::/0"]
    assert cfg.address_v4 == "10.7.0.2/32"


def test_ipv6_null_route_when_node_lacks_v6(session, node):
    lease = allocate(session, node)
    cfg = build_peer_config(node, lease)
    assert cfg.v6_null_routed is True
    # the client is issued a ULA from the blackhole prefix, ::/0 goes into the tunnel and is dropped
    assert cfg.address_v6.startswith("fd00:0:0:dead:")
    assert "::/0" in cfg.allowed_ips
    assert IPV6_NULLROUTE_PREFIX  # the prefix is set


def test_ipv6_real_when_node_has_v6(session, node_v6):
    lease = allocate(session, node_v6)
    cfg = build_peer_config(node_v6, lease)
    assert cfg.v6_null_routed is False
    assert cfg.address_v6 == "fd00:8::2/128"


def test_server_allowed_ips_scopes_to_peer(session, node, node_v6):
    lease4 = allocate(session, node)
    assert server_allowed_ips(node, lease4) == ["10.7.0.2/32"]
    lease6 = allocate(session, node_v6)
    assert server_allowed_ips(node_v6, lease6) == ["10.8.0.2/32", "fd00:8::2/128"]


def test_endpoint_uses_node_ports(session, node):
    lease = allocate(session, node)
    cfg = build_peer_config(node, lease)
    assert cfg.endpoint == "pa1.example.net:51820"
    assert cfg.endpoint_alt == "pa1.example.net:443"
