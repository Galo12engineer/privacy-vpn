"""Building the WireGuard config WITHOUT the PrivateKey.

The server hands out everything except the private key (server pubkey, endpoint, the
allocated IP, DNS, AllowedIPs, MTU). The browser inserts the private key and renders the
QR LOCALLY. The private key never passes through the server.

IPv6: if the node has no v6 — the client is given a ULA from a null-route prefix, ::/0 goes
into the tunnel and is dropped on the node (protection against an IPv6 leak).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from app.models.enums import NodeProtocol
from app.services.awg_profile import profile_from_node

DEFAULT_KEEPALIVE = 25
# Order of the AmneziaWG obfuscation lines in [Interface] (profile key → label in .conf)
_AMNEZIA_KEYS = (
    ("jc", "Jc"), ("jmin", "Jmin"), ("jmax", "Jmax"),
    ("s1", "S1"), ("s2", "S2"),
    ("h1", "H1"), ("h2", "H2"), ("h3", "H3"), ("h4", "H4"),
)
IPV6_NULLROUTE_PREFIX = "fd00:0:0:dead::/64"
PRIVATE_KEY_PLACEHOLDER = "<INSERT_PRIVATE_KEY_IN_BROWSER>"


@dataclass
class PeerConfig:
    address_v4: str
    address_v6: str
    dns: str
    mtu: int
    server_public_key: str
    endpoint: str
    endpoint_alt: str
    allowed_ips: list[str]
    persistent_keepalive: int
    preshared_key: str | None
    v6_null_routed: bool
    # AmneziaWG obfuscation profile (Jc,Jmin,Jmax,S1,S2,H1-H4) or None for vanilla WG
    amnezia: dict | None = None


def _v6_for(node, lease) -> tuple[str, bool]:
    if node.subnet_v6 and lease.address_v6:
        return f"{lease.address_v6}/128", False
    # null-route: the same host-offset, but in the blackhole prefix
    net4 = ipaddress.ip_network(node.subnet_v4, strict=False)
    null6 = ipaddress.ip_network(IPV6_NULLROUTE_PREFIX, strict=False)
    offset = int(ipaddress.ip_address(lease.address)) - int(net4.network_address)
    return f"{null6.network_address + offset}/128", True


def build_peer_config(
    node,
    lease,
    *,
    server_public_key: str | None = None,
    preshared_key: str | None = None,
    full_tunnel: bool = True,
    allowed_ips: list[str] | None = None,
    keepalive: int = DEFAULT_KEEPALIVE,
) -> PeerConfig:
    v6_addr, v6_null = _v6_for(node, lease)
    if allowed_ips is None:
        allowed_ips = ["0.0.0.0/0", "::/0"] if full_tunnel else []
    amnezia = None
    if getattr(node, "protocol", None) == NodeProtocol.AMNEZIAWG:
        amnezia = profile_from_node(node)
    return PeerConfig(
        address_v4=f"{lease.address}/32",
        address_v6=v6_addr,
        dns=node.dns_ip,
        mtu=node.mtu,
        server_public_key=server_public_key or node.public_key or "",
        endpoint=f"{node.endpoint_host}:{node.endpoint_port}",
        endpoint_alt=f"{node.endpoint_host}:{node.endpoint_port_alt}",
        allowed_ips=allowed_ips,
        persistent_keepalive=keepalive,
        preshared_key=preshared_key,
        v6_null_routed=v6_null,
        amnezia=amnezia,
    )


def _amnezia_lines(profile: dict) -> list[str]:
    """AmneziaWG obfuscation lines for the [Interface] section."""
    return [f"{label} = {profile[key]}" for key, label in _AMNEZIA_KEYS]


def render_conf(
    cfg: PeerConfig, *, private_key_placeholder: str = PRIVATE_KEY_PLACEHOLDER, tr=None
) -> str:
    """Server-side render of the .conf with a PLACEHOLDER instead of the private key (fallback without JS).

    For AmneziaWG the lines Jc/Jmin/Jmax/S1/S2/H1-H4 are added to [Interface] — they are understood
    only by AmneziaWG/Amnezia clients (the official WireGuard client does not support them).

    `tr` is the request's translator. The comment below is content the CUSTOMER reads — it is the
    product's central privacy claim, stated exactly where it matters: in the file that holds his
    key. It used to be a bare Russian literal, so every English customer got Russian inside his
    .conf. It slipped past scripts/check_i18n.py because that only inspects `tr('…')` calls and
    `error=`/`sub=` kwargs — a plain string in a list matches neither. Default = RU passthrough,
    matching the rest of the codebase (RU is the source language, EN comes from en_by_ru.json).
    """
    tr = tr or (lambda s: s)
    lines = [
        "[Interface]",
        "# " + tr("PrivateKey генерируется в браузере и подставляется ЛОКАЛЬНО; на сервер не уходит"),
        f"PrivateKey = {private_key_placeholder}",
        f"Address = {cfg.address_v4}, {cfg.address_v6}",
        f"DNS = {cfg.dns}",
        f"MTU = {cfg.mtu}",
    ]
    if cfg.amnezia:
        lines += _amnezia_lines(cfg.amnezia)
    lines += [
        "",
        "[Peer]",
        f"PublicKey = {cfg.server_public_key}",
    ]
    if cfg.preshared_key:
        lines.append(f"PresharedKey = {cfg.preshared_key}")
    lines += [
        f"AllowedIPs = {', '.join(cfg.allowed_ips)}",
        f"Endpoint = {cfg.endpoint}",
        f"PersistentKeepalive = {cfg.persistent_keepalive}",
    ]
    return "\n".join(lines) + "\n"


def to_browser_payload(cfg: PeerConfig) -> dict:
    """JSON for the cabinet: everything EXCEPT the private key. The browser assembles the .conf and QR locally."""
    return {
        "address": [cfg.address_v4, cfg.address_v6],
        "dns": cfg.dns,
        "mtu": cfg.mtu,
        "server_public_key": cfg.server_public_key,
        "endpoint": cfg.endpoint,
        "endpoint_alt": cfg.endpoint_alt,
        "allowed_ips": cfg.allowed_ips,
        "persistent_keepalive": cfg.persistent_keepalive,
        "preshared_key": cfg.preshared_key,
        "v6_null_routed": cfg.v6_null_routed,
        "amnezia": cfg.amnezia,
        "private_key_placeholder": PRIVATE_KEY_PLACEHOLDER,
    }


def server_allowed_ips(node, lease) -> list[str]:
    """AllowedIPs on the SERVER side for this peer = its /32 (+ /128 if the node has v6)."""
    ips = [f"{lease.address}/32"]
    if node.subnet_v6 and lease.address_v6:
        ips.append(f"{lease.address_v6}/128")
    return ips
