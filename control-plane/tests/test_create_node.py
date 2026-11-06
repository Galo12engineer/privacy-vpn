"""Regression for scripts/create_node.py — operator-side node creation without the web admin panel.

The script must produce EXACTLY the same result as the admin panel (it calls the very same
admin_service.add_node): vless profile, one-time token, correct subnet/dns/port; auto-subnet
without collisions.
"""

import ipaddress
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from app.models import Node
from app.models.enums import NodeProtocol
from app.services import admin_service

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import create_node  # noqa: E402


def _args(**over):
    base = dict(
        host="1.2.3.4", location_code="de", location_name=None, name=None,
        protocol="vless_reality", dest="dl.google.com:443", sni=None,
        subnet=None, port=None, no_validate_dest=True, dry_run=False,
    )
    base.update(over)
    return Namespace(**base)


def test_creates_vless_node_with_token(session):
    node, token = admin_service.add_node(session, **create_node.build_kwargs(session, _args()))
    session.flush()
    assert node.protocol == NodeProtocol.VLESS_REALITY
    assert node.endpoint_port == 443, "vless-порт по умолчанию должен быть 443"
    assert node.reality_config, "серверный reality-профиль должен быть записан"
    assert token and node.registration_token_hash, "токен выдан и его хэш сохранён"
    assert node.registration_token_hash != token, "в БД лежит ХЭШ, не сам токен"
    net = ipaddress.ip_network(node.subnet_v4)
    assert node.dns_ip == str(next(net.hosts())), "dns_ip = первый хост подсети"
    assert json.loads(node.reality_config)["server_names"] == ["dl.google.com"]
    assert json.loads(node.reality_config)["dest"] == "dl.google.com:443"


def test_subnet_auto_allocation_no_collision(session):
    n1, _ = admin_service.add_node(session, **create_node.build_kwargs(session, _args(host="1.1.1.1")))
    session.flush()
    n2, _ = admin_service.add_node(session, **create_node.build_kwargs(session, _args(host="2.2.2.2")))
    session.flush()
    a, b = ipaddress.ip_network(n1.subnet_v4), ipaddress.ip_network(n2.subnet_v4)
    assert not a.overlaps(b), (n1.subnet_v4, n2.subnet_v4)
    assert n1.name != n2.name, "авто-имена уникальны"


def test_allocate_skips_existing_subnet(session):
    session.add(Node(
        name="occupied", endpoint_host="h", location_code="x", location_name="X",
        subnet_v4="10.77.0.0/24", dns_ip="10.77.0.1",
    ))
    session.flush()
    sub = create_node.allocate_subnet(session)
    assert not ipaddress.ip_network(sub).overlaps(ipaddress.ip_network("10.77.0.0/24"))


def test_explicit_subnet_name_and_sni(session):
    k = create_node.build_kwargs(session, _args(subnet="10.90.0.0/24", name="ru-1", sni="www.samsung.com"))
    assert k["subnet_v4"] == "10.90.0.0/24"
    assert k["dns_ip"] == "10.90.0.1"
    assert k["name"] == "ru-1"
    assert k["reality_server_names"] == ["www.samsung.com"]


def test_wireguard_protocol_defaults_port_51820(session):
    k = create_node.build_kwargs(session, _args(protocol="wireguard"))
    assert k["endpoint_port"] == 51820
    assert "reality_dest" not in k  # not reality — no reality fields


def test_explicit_subnet_overlapping_existing_is_rejected(session):
    """HIGH fix: an explicit --subnet overlapping a live node must abort with an error, not create it."""
    session.add(Node(
        name="nl-1", endpoint_host="h", location_code="nl", location_name="NL",
        subnet_v4="10.10.0.0/24", dns_ip="10.10.0.1",
    ))
    session.flush()
    # exact match
    with pytest.raises(SystemExit, match="пересекается"):
        create_node.build_kwargs(session, _args(subnet="10.10.0.0/24"))
    # supernet that swallows the existing /24
    with pytest.raises(SystemExit, match="пересекается"):
        create_node.build_kwargs(session, _args(subnet="10.10.0.0/16"))
    # non-overlapping — passes
    k = create_node.build_kwargs(session, _args(subnet="10.90.0.0/24"))
    assert k["subnet_v4"] == "10.90.0.0/24"


def test_malformed_db_subnet_fails_closed(session):
    """MEDIUM fix: a malformed subnet_v4 in the DB must not be silently ignored (fail-open)."""
    session.add(Node(
        name="broken", endpoint_host="h", location_code="x", location_name="X",
        subnet_v4="not-a-cidr", dns_ip="0.0.0.0",
    ))
    session.flush()
    with pytest.raises(SystemExit, match="некорректная подсеть"):
        create_node.allocate_subnet(session)
