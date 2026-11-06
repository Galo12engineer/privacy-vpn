#!/usr/bin/env python
"""Create a node and issue a registration token WITHOUT the web admin UI (operator CLI).

Why: normally a node is created via the Tor admin UI (secret path + 2FA). This script does the
same thing with ONE command on the control-plane host — bypassing Tor/2FA. Under the hood it is
the same `admin_service.add_node` the admin UI uses (zero risk of divergence: reality profile,
token, fields are identical). Prints everything you need to fill into
infra/cloud-init/node-vless.example.yaml.

    # vless_reality node (for RU), minimum — public IP of the box + location code:
    docker compose exec control-plane python scripts/create_node.py --host 1.2.3.4 --location-code de
    # with explicit parameters:
    … create_node.py --host 1.2.3.4 --location-code de --location-name Frankfurt \
        --name ru-test-1 --dest dl.google.com:443 --subnet 10.90.0.0/24
    # see what would happen, writing NOTHING:
    … create_node.py --host 1.2.3.4 --location-code de --dry-run

The subnet (--subnet) may be omitted — the first free /24 that does not overlap with existing
nodes is taken. dns_ip = the first host of the subnet. vless port = 443. By default dest is
validated (TLS1.3+h2+non-CDN); if the CP host has no internet access — add --no-validate-dest.

Run with the CP COMMITTED; writes to the same DB (CP_DATABASE_URL) as grant_time.py.
"""
from __future__ import annotations

import argparse
import ipaddress

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import Node
from app.models.enums import NodeProtocol
from app.services import admin_service
from app.services.node_registration import server_address

# subnet pool for auto-allocation: 10.77.0.0/24 … 10.126.0.0/24 (50 of them)
_SUBNET_POOL_BASE = 77
_SUBNET_POOL_SIZE = 50


def _existing_subnets(session) -> list[tuple[str, ipaddress.IPv4Network | ipaddress.IPv6Network]]:
    """(name, network) for all nodes. **Fail-closed**: a broken subnet_v4 in the DB → abort, NOT
    silently (otherwise the allocator would treat it as "free space" and hand out an overlapping /24)."""
    out = []
    for name, cidr in session.execute(select(Node.name, Node.subnet_v4)).all():
        try:
            out.append((name, ipaddress.ip_network(cidr, strict=False)))
        except ValueError as e:
            raise SystemExit(
                f"ERROR: у ноды {name!r} в БД некорректная подсеть {cidr!r} ({e}). "
                f"Почини её или задай --subnet вручную — авто-выдача остановлена во избежание коллизии."
            ) from e
    return out


def allocate_subnet(session) -> str:
    """The first /24 from the pool that does not overlap with any existing node."""
    used = [net for _, net in _existing_subnets(session)]
    for i in range(_SUBNET_POOL_SIZE):
        cand = ipaddress.ip_network(f"10.{_SUBNET_POOL_BASE + i}.0.0/24")
        if not any(cand.overlaps(u) for u in used):
            return str(cand)
    raise SystemExit(
        "ERROR: свободная /24 в пуле не найдена — задай --subnet вручную (напр. 10.130.0.0/24)."
    )


def check_explicit_subnet(session, subnet: str) -> None:
    """An explicit --subnet is validated like the allocator: it must not overlap with any node.
    Without this the operator could specify a /24 overlapping a live node (nl-1) → broken topology."""
    try:
        cand = ipaddress.ip_network(subnet, strict=False)
    except ValueError as e:
        raise SystemExit(f"ERROR: --subnet {subnet!r} невалидна: {e}") from e
    for name, net in _existing_subnets(session):
        if cand.overlaps(net):
            raise SystemExit(
                f"ERROR: --subnet {subnet} пересекается с нодой {name!r} ({net}). "
                f"Выбери непересекающуюся или опусти --subnet для авто-выдачи."
            )


def _auto_name(session, location_code: str, protocol: str) -> str:
    """<location>-<proto>-<N>, N = the first free one (node name is unique in the DB)."""
    tag = "vless" if protocol == "vless_reality" else protocol[:4]
    existing = {n for (n,) in session.execute(select(Node.name)).all()}
    for i in range(1, 1000):
        name = f"{location_code}-{tag}-{i}"
        if name not in existing:
            return name
    raise SystemExit("ERROR: не подобрать уникальное имя — задай --name вручную.")


def build_kwargs(session, args) -> dict:
    """Assemble add_node arguments from the CLI (+ auto subnet/name/dns/port)."""
    protocol = NodeProtocol(args.protocol)
    if args.subnet:
        check_explicit_subnet(session, args.subnet)  # an explicit subnet is also checked for overlap
        subnet = args.subnet
    else:
        subnet = allocate_subnet(session)
    net = ipaddress.ip_network(subnet, strict=False)
    dns_ip = str(next(net.hosts()))  # first host = the node's resolver (= server_address without the prefix)
    name = args.name or _auto_name(session, args.location_code, args.protocol)
    port = args.port if args.port is not None else (443 if protocol == NodeProtocol.VLESS_REALITY else 51820)

    kwargs = dict(
        name=name,
        endpoint_host=args.host,
        location_code=args.location_code,
        location_name=args.location_name or args.location_code.upper(),
        subnet_v4=subnet,
        dns_ip=dns_ip,
        endpoint_port=port,
        protocol=protocol,
    )
    if protocol == NodeProtocol.VLESS_REALITY:
        raw = args.sni or args.dest.split(":")[0]
        # as in the admin UI (admin_ui.add_node): several SNIs can be given comma-separated
        server_names = [s.strip() for s in raw.split(",") if s.strip()]
        kwargs.update(
            reality_dest=args.dest,
            reality_server_names=server_names,
            validate_dest=not args.no_validate_dest,
        )
    return kwargs


def main() -> int:
    ap = argparse.ArgumentParser(description="Создать ноду + токен без веб-админки (оператор).")
    ap.add_argument("--host", required=True, help="публичный IP/домен бокса (endpoint_host)")
    ap.add_argument("--location-code", required=True, help="код локации, напр. de/nl/fi")
    ap.add_argument("--location-name", default=None, help="человеко-имя локации (дефолт = код заглавными)")
    ap.add_argument("--name", default=None, help="имя ноды (уникальное; дефолт авто <loc>-vless-N)")
    ap.add_argument("--protocol", default="vless_reality",
                    choices=["vless_reality", "wireguard", "amneziawg"])
    ap.add_argument("--dest", default="dl.google.com:443", help="reality dest host:port (vless)")
    ap.add_argument("--sni", default=None, help="reality SNI (дефолт = хост dest)")
    ap.add_argument("--subnet", default=None, help="/24 подсеть ноды (дефолт — первая свободная)")
    ap.add_argument("--port", type=int, default=None, help="порт (дефолт 443 vless / 51820 wg)")
    ap.add_argument("--no-validate-dest", action="store_true",
                    help="не проверять dest по сети (если хост CP без выхода в интернет)")
    ap.add_argument("--dry-run", action="store_true", help="показать, что будет, и откатить")
    args = ap.parse_args()

    with SessionLocal() as session:
        kwargs = build_kwargs(session, args)
        try:
            node, token = admin_service.add_node(session, **kwargs)
        except ValueError as e:  # DestError (unusable dest) / missing reality fields
            session.rollback()
            print(f"ERROR: {e}")
            return 2
        except IntegrityError:
            # the node name uniqueness is violated on flush() INSIDE add_node (not on commit) —
            # we catch it here, otherwise it would fly out as a traceback past the commit handler below.
            session.rollback()
            print(f"ERROR: нода с именем {kwargs['name']!r} уже есть — задай другое --name.")
            return 2
        server_addr = server_address(node)
        # a snapshot of the values BEFORE a possible rollback (after rollback the object expires)
        info = {
            "name": node.name, "id": str(node.id), "protocol": node.protocol.value,
            "endpoint_host": node.endpoint_host, "endpoint_port": node.endpoint_port,
            "subnet_v4": node.subnet_v4, "dns_ip": node.dns_ip, "server_address": server_addr,
            "reality_dest": kwargs.get("reality_dest"), "sni": ",".join(kwargs.get("reality_server_names") or []) or None,
        }
        if args.dry_run:
            session.rollback()
            print("=== DRY-RUN (ничего не записано) ===")
        else:
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                print(f"ERROR: нода с именем {info['name']!r} уже есть — задай другое --name.")
                return 2
            print("=== NODE CREATED ===")

    print(f"NAME            = {info['name']}")
    print(f"NODE_ID         = {info['id']}")
    print(f"PROTOCOL        = {info['protocol']}")
    print(f"ENDPOINT        = {info['endpoint_host']}:{info['endpoint_port']}")
    if info["reality_dest"]:
        print(f"REALITY_DEST    = {info['reality_dest']}   (SNI={info['sni']})")
    if not args.dry_run:
        print(f"\nREGISTRATION_TOKEN = {token}      # одноразовый — в cloud-init")
    print("\n# впиши в infra/cloud-init/node-vless.example.yaml:")
    print(f"#   AGENT_REGISTRATION_TOKEN = {'<см. выше>' if not args.dry_run else '<будет после запуска без --dry-run>'}")
    print(f"#   AGENT_SUBNET_V4          = {info['subnet_v4']}")
    print(f"#   AGENT_DNS_LISTEN_IP      = {info['dns_ip']}   (= server_address {info['server_address']})")
    print(f"#   AGENT_WG_LISTEN_PORT     = {info['endpoint_port']}")
    print("#   AGENT_WAN_INTERFACE      = <на боксе: ip -o -4 route show default | awk '{print $5}'>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
