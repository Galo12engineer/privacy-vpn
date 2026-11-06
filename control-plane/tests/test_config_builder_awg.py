from app.models.enums import NodeProtocol
from app.services import awg_profile
from app.services.config_builder import build_peer_config, render_conf, to_browser_payload
from app.services.ipam import allocate


def _as_amneziawg(node) -> dict:
    node.public_key = "SERVER_PUBKEY_BASE64_44_CHARS_PLACEHOLDER="
    node.protocol = NodeProtocol.AMNEZIAWG
    profile = awg_profile.generate_profile()
    for key, value in profile.items():
        setattr(node, f"awg_{key}", value)
    return profile


def test_wireguard_node_has_no_amnezia_lines(session, node):
    lease = allocate(session, node)
    cfg = build_peer_config(node, lease)
    assert cfg.amnezia is None
    conf = render_conf(cfg)
    assert "Jc = " not in conf
    assert "H1 = " not in conf


def test_amneziawg_conf_carries_obfuscation_lines(session, node):
    profile = _as_amneziawg(node)
    lease = allocate(session, node)
    cfg = build_peer_config(node, lease)
    assert cfg.amnezia == profile
    conf = render_conf(cfg)
    for label in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"):
        assert f"{label} = " in conf
    # obfuscation inside [Interface] — strictly BEFORE [Peer]
    assert conf.index("H1 = ") < conf.index("[Peer]")
    # zero-knowledge is not broken: the private key is still only a placeholder
    assert "PrivateKey = <INSERT_PRIVATE_KEY_IN_BROWSER>" in conf


def test_amneziawg_browser_payload_includes_profile(session, node):
    profile = _as_amneziawg(node)
    lease = allocate(session, node)
    cfg = build_peer_config(node, lease)
    payload = to_browser_payload(cfg)
    assert payload["amnezia"] == profile
    assert "private_key" not in payload
