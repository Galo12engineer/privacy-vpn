"""nftables: NAT (masquerade) + forwarding for the client subnet + input-firewall.

Idempotent (flush+set). All dynamic substitutions (subnet/interface/port) are
VALIDATED before they reach the ruleset (M2): subnets are canonicalized via ipaddress,
interface names go through a strict regex, ports — a range check. This closes injection
via a forged registration response (subnet_v4/subnet_v6 come from the CP).

Input chain (H2): policy drop + explicit allow for loopback/established/ICMP, SSH(22),
wg ports and (optionally) the agent's control port ONLY from the management-allow-list. By
default the control port is closed from the WAN side — the operator opens it explicitly for the CP IP.

If the node has no IPv6 — we add a drop of client v6 forwarding (protection against an IPv6 leak):
the client pushes ::/0 into the tunnel, the node drops it.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]

# Linux IFNAMSIZ-1 = 15; we allow only safe interface-name characters.
_IFACE_RE = re.compile(r"^[A-Za-z0-9._-]{1,15}$")


def _valid_iface(name: str) -> str:
    if not isinstance(name, str) or not _IFACE_RE.match(name):
        raise ValueError(f"invalid interface name: {name!r}")
    return name


def _valid_cidr(cidr: str) -> str:
    """Canonicalize the CIDR via ipaddress → the output is a clean string with no injections."""
    if not isinstance(cidr, str):
        raise ValueError(f"invalid CIDR: {cidr!r}")
    return str(ipaddress.ip_network(cidr.strip(), strict=False))


def _valid_port(port: int) -> int:
    p = int(port)
    if not (1 <= p <= 65535):
        raise ValueError(f"port out of range: {port!r}")
    return p


def render_ruleset(
    *,
    wan_interface: str,
    subnet_v4: str,
    subnet_v6: str | None = None,
    wg_interface: str = "wg0",
    wg_listen_port: int = 51820,
    wg_listen_port_alt: int | None = 443,
    control_port: int | None = None,
    control_allow_cidrs: Sequence[str] = (),
    ssh_port: int = 22,
    dns_port: int = 53,
    protocol: str = "wireguard",
) -> str:
    # --- validation/canonicalization of all dynamic inputs (M2) ---
    wan_interface = _valid_iface(wan_interface)
    wg_interface = _valid_iface(wg_interface)
    subnet_v4 = _valid_cidr(subnet_v4)
    subnet_v6 = _valid_cidr(subnet_v6) if subnet_v6 else None
    wg_listen_port = _valid_port(wg_listen_port)
    wg_listen_port_alt = _valid_port(wg_listen_port_alt) if wg_listen_port_alt else None
    ssh_port = _valid_port(ssh_port)
    dns_port = _valid_port(dns_port)
    control_port = _valid_port(control_port) if control_port else None
    allow_cidrs = [_valid_cidr(c) for c in control_allow_cidrs if str(c).strip()]

    forward_v6 = ""
    nat_v6 = ""
    if subnet_v6:
        forward_v6 = (
            f'        iifname "{wg_interface}" ip6 saddr {subnet_v6} accept\n'
        )
        nat_v6 = (
            "table ip6 vpn_nat {\n"
            "    chain postrouting {\n"
            "        type nat hook postrouting priority srcnat; policy accept;\n"
            f'        ip6 saddr {subnet_v6} oifname "{wan_interface}" masquerade\n'
            "    }\n"
            "}\n"
        )
    else:
        # null-route v6: drop client IPv6 coming from the tunnel (anti-leak)
        forward_v6 = f'        iifname "{wg_interface}" ip6 daddr ::/0 drop\n'

    # alt port (443) → main wg port (51820): the node listens on both with a single interface
    prerouting = ""
    if wg_listen_port_alt and wg_listen_port_alt != wg_listen_port:
        prerouting = (
            "    chain prerouting {\n"
            "        type nat hook prerouting priority dstnat; policy accept;\n"
            f'        iifname "{wan_interface}" udp dport {wg_listen_port_alt} '
            f"redirect to :{wg_listen_port}\n"
            "    }\n"
        )

    # Client isolation + protection of the hoster's infra via the exit (before the general saddr-accept):
    # - client↔client: WG routes between peers (allowed-ips /32 does not isolate) →
    #   without an explicit drop clients see each other (scan/attack). oifname=wg → peer↔peer forward.
    # - cloud metadata (169.254.169.254) and RFC1918 (the hoster's LAN) are reachable via the exit →
    #   SSRF/infra-leak. We drop them as destination. The node's DNS (e.g. 10.7.0.1) is NOT affected —
    #   it is addressed to the node itself → it goes to input, not to forward.
    forward_isolation = (
        f'        iifname "{wg_interface}" oifname "{wg_interface}" drop\n'
        f'        iifname "{wg_interface}" ip daddr 169.254.0.0/16 drop\n'
        f'        iifname "{wg_interface}" ip daddr 10.0.0.0/8 drop\n'
        f'        iifname "{wg_interface}" ip daddr 172.16.0.0/12 drop\n'
        f'        iifname "{wg_interface}" ip daddr 192.168.0.0/16 drop\n'
    )

    # --- input chain (H2): drop by default, explicit allows ---
    input_chain = _render_input_chain(
        wan_interface=wan_interface,
        wg_interface=wg_interface,
        wg_listen_port=wg_listen_port,
        wg_listen_port_alt=wg_listen_port_alt,
        control_port=control_port,
        allow_cidrs=allow_cidrs,
        ssh_port=ssh_port,
        dns_port=dns_port,
        protocol=protocol,
    )

    return (
        "flush ruleset\n\n"
        "table inet vpn_filter {\n"
        f"{input_chain}"
        "    chain forward {\n"
        "        type filter hook forward priority filter; policy drop;\n"
        "        ct state established,related accept\n"
        # anti-spam (the hoster's red line): we cut client SMTP in forward,
        # so that the node's IP does not land in Spamhaus and get suspended
        f'        iifname "{wg_interface}" tcp dport 25 drop\n'
        f"{forward_isolation}"
        f'        iifname "{wg_interface}" ip saddr {subnet_v4} accept\n'
        f"{forward_v6}"
        "    }\n"
        "}\n\n"
        "table ip vpn_nat {\n"
        f"{prerouting}"
        "    chain postrouting {\n"
        "        type nat hook postrouting priority srcnat; policy accept;\n"
        f'        ip saddr {subnet_v4} oifname "{wan_interface}" masquerade\n'
        "    }\n"
        "}\n"
        f"{nat_v6}"
    )


def _render_input_chain(
    *,
    wan_interface: str,
    wg_interface: str,
    wg_listen_port: int,
    wg_listen_port_alt: int | None,
    control_port: int | None,
    allow_cidrs: list[str],
    ssh_port: int,
    dns_port: int,
    protocol: str = "wireguard",
) -> str:
    lines = [
        "    chain input {\n",
        "        type filter hook input priority filter; policy drop;\n",
        "        ct state established,related accept\n",
        "        ct state invalid drop\n",
        '        iif "lo" accept\n',
        # ICMP/ICMPv6 — ping + PMTU discovery (without them the MTU negotiation breaks)
        "        meta l4proto icmp accept\n",
        "        meta l4proto ipv6-icmp accept\n",
        # SSH — NEVER close it (otherwise the operator loses access to the node)
        f"        tcp dport {ssh_port} accept\n",
        # WireGuard: the main port is always open from the WAN side
        f"        udp dport {wg_listen_port} accept\n",
    ]
    # VLESS+Reality (Xray) listens on TCP on this port (usually 443) — we open TCP
    if protocol == "vless_reality":
        lines.append(f"        tcp dport {wg_listen_port} accept\n")
    if wg_listen_port_alt and wg_listen_port_alt != wg_listen_port:
        lines.append(f"        udp dport {wg_listen_port_alt} accept\n")
    # per-node no-log resolver: only from the tunnel, not from the WAN
    lines.append(f'        iifname "{wg_interface}" udp dport {dns_port} accept\n')
    lines.append(f'        iifname "{wg_interface}" tcp dport {dns_port} accept\n')
    # the agent's control port: open ONLY from the management-allow-list (the CP's IP).
    # Empty → the port is closed from the WAN side (secure by default, H2).
    if control_port and allow_cidrs:
        for cidr in allow_cidrs:
            fam = "ip6" if ":" in cidr else "ip"
            lines.append(
                f"        {fam} saddr {cidr} tcp dport {control_port} accept\n"
            )
    lines.append("    }\n")
    return "".join(lines)


def apply_ruleset(ruleset: str, *, runner: Runner) -> subprocess.CompletedProcess:
    # we always delete the temp file (RAM-only ethos + no accumulation in /tmp), like the PSK in wg.py
    fd, path = tempfile.mkstemp(suffix=".nft")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(ruleset)
        return runner(["nft", "-f", path])
    finally:
        os.unlink(path)
