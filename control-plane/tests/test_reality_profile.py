import base64

import pytest

from app.services import reality_profile


def _unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_x25519_keys_valid_base64url_32_bytes():
    priv, pub = reality_profile.generate_x25519()
    assert len(_unb64url(priv)) == 32
    assert len(_unb64url(pub)) == 32
    # the private key is clamped the same way `xray x25519` does it (RFC 7748)
    raw = _unb64url(priv)
    assert raw[0] & 7 == 0
    assert raw[31] & 128 == 0
    assert raw[31] & 64 == 64


def test_short_id_hex_even_length():
    sid = reality_profile.generate_short_id()
    assert len(sid) == 16
    int(sid, 16)  # valid hex → does not raise


def test_server_profile_generates_and_validates():
    p = reality_profile.generate_server_profile(
        dest="www.example.com:443", server_names=["www.example.com"]
    )
    reality_profile.validate_server_profile(p)  # does not raise
    assert p["fingerprint"] == "firefox"
    assert p["private_key"] and p["public_key"] and p["private_key"] != p["public_key"]


def test_validate_rejects_bad_short_id():
    p = reality_profile.generate_server_profile(dest="a:443", server_names=["a"])
    p["short_ids"] = ["xyz"]  # not hex / odd length
    with pytest.raises(ValueError):
        reality_profile.validate_server_profile(p)


def test_validate_rejects_dest_without_port():
    p = reality_profile.generate_server_profile(dest="a:443", server_names=["a"])
    p["dest"] = "no-port"
    with pytest.raises(ValueError):
        reality_profile.validate_server_profile(p)


def test_profile_roundtrip_via_node_json():
    p = reality_profile.generate_server_profile(dest="a.com:443", server_names=["a.com"])

    class _Node:
        reality_config = reality_profile.dump_profile(p)

    assert reality_profile.profile_from_node(_Node()) == p


def test_profile_from_node_none_when_unset():
    class _Node:
        reality_config = None

    assert reality_profile.profile_from_node(_Node()) is None


def test_generate_client_unique_uuid_and_email():
    c1 = reality_profile.generate_client("peer-1")
    c2 = reality_profile.generate_client("peer-2")
    assert c1["uuid"] != c2["uuid"]
    assert c1["email"] == "p-peer-1"
