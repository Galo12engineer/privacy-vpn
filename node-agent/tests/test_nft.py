import os
import subprocess

import pytest

from agent.nft import apply_ruleset, render_ruleset


def test_v4_masquerade_present():
    rs = render_ruleset(wan_interface="eth0", subnet_v4="10.7.0.0/24", wg_interface="wg0")
    assert "masquerade" in rs
    assert "10.7.0.0/24" in rs
    assert 'oifname "eth0"' in rs
    assert "flush ruleset" in rs  # idempotent


def test_v6_null_route_when_no_v6():
    rs = render_ruleset(wan_interface="eth0", subnet_v4="10.7.0.0/24")
    # without v6 — client IPv6 is dropped (anti-leak), no v6 NAT
    assert "ip6 daddr ::/0 drop" in rs
    assert "table ip6 vpn_nat" not in rs


def test_v6_nat_when_node_has_v6():
    rs = render_ruleset(
        wan_interface="eth0", subnet_v4="10.7.0.0/24", subnet_v6="fd00:7::/64"
    )
    assert "table ip6 vpn_nat" in rs
    assert "fd00:7::/64" in rs
    assert "ip6 daddr ::/0 drop" not in rs


def test_alt_port_redirected_to_wg_port():
    rs = render_ruleset(
        wan_interface="eth0",
        subnet_v4="10.7.0.0/24",
        wg_listen_port=51820,
        wg_listen_port_alt=443,
    )
    # the node listens on 51820 AND 443 with one interface
    assert "udp dport 443 redirect to :51820" in rs


def test_no_redirect_when_alt_equals_main():
    rs = render_ruleset(
        wan_interface="eth0",
        subnet_v4="10.7.0.0/24",
        wg_listen_port=51820,
        wg_listen_port_alt=None,
    )
    assert "redirect to" not in rs


# --- H2: input chain ---
def test_input_chain_default_drop_with_safe_allows():
    rs = render_ruleset(wan_interface="eth0", subnet_v4="10.7.0.0/24", control_port=8443)
    assert "chain input {" in rs
    assert "type filter hook input priority filter; policy drop;" in rs
    assert "ct state established,related accept" in rs
    assert 'iif "lo" accept' in rs
    assert "tcp dport 22 accept" in rs           # SSH is never closed
    assert "udp dport 51820 accept" in rs        # WG reachable from WAN
    assert "udp dport 443 accept" in rs          # WG alt port


def test_control_port_closed_from_wan_by_default():
    # without an allow-list the control port is NOT open (policy drop kills it)
    rs = render_ruleset(wan_interface="eth0", subnet_v4="10.7.0.0/24", control_port=8443)
    assert "tcp dport 8443 accept" not in rs


def test_control_port_open_only_from_mgmt_cidr():
    rs = render_ruleset(
        wan_interface="eth0", subnet_v4="10.7.0.0/24",
        control_port=8443, control_allow_cidrs=["198.51.100.7/32"],
    )
    assert "ip saddr 198.51.100.7/32 tcp dport 8443 accept" in rs


# --- anti-spam: forward block of port 25 ---
def test_client_smtp_blocked_in_forward():
    # client SMTP (port 25) is dropped in forward — protects the node IP reputation
    rs = render_ruleset(wan_interface="eth0", subnet_v4="10.7.0.0/24")
    assert 'iifname "wg0" tcp dport 25 drop' in rs


# --- client isolation + protecting the hoster infra via the exit ---
def test_client_isolation_and_infra_drops():
    rs = render_ruleset(wan_interface="eth0", subnet_v4="10.7.0.0/24", wg_interface="wg0")
    assert 'iifname "wg0" oifname "wg0" drop' in rs                 # client↔client
    assert 'iifname "wg0" ip daddr 169.254.0.0/16 drop' in rs       # cloud-metadata/link-local
    assert 'iifname "wg0" ip daddr 10.0.0.0/8 drop' in rs           # RFC1918
    assert 'iifname "wg0" ip daddr 172.16.0.0/12 drop' in rs
    assert 'iifname "wg0" ip daddr 192.168.0.0/16 drop' in rs
    # the drops MUST come before the general saddr-accept (otherwise accept matches first)
    assert rs.index('oifname "wg0" drop') < rs.index("ip saddr 10.7.0.0/24 accept")
    assert rs.index("daddr 192.168.0.0/16 drop") < rs.index("ip saddr 10.7.0.0/24 accept")


# --- M2: input validation before substitution into the ruleset ---
def test_injection_in_subnet_rejected():
    with pytest.raises(ValueError):
        render_ruleset(wan_interface="eth0", subnet_v4="10.7.0.0/24 ; accept # pwn")


def test_injection_in_interface_rejected():
    with pytest.raises(ValueError):
        render_ruleset(wan_interface='eth0" accept #', subnet_v4="10.7.0.0/24")


def test_newline_injection_in_subnet_rejected():
    with pytest.raises(ValueError):
        render_ruleset(wan_interface="eth0", subnet_v4="10.7.0.0/24\n        drop")


def test_bad_mgmt_cidr_rejected():
    with pytest.raises(ValueError):
        render_ruleset(
            wan_interface="eth0", subnet_v4="10.7.0.0/24",
            control_port=8443, control_allow_cidrs=["not-a-cidr"],
        )


def test_subnet_is_canonicalized():
    # host bits do not leak raw — ipaddress normalizes them
    rs = render_ruleset(wan_interface="eth0", subnet_v4="10.7.0.5/24")
    assert "10.7.0.0/24" in rs


def test_apply_ruleset_cleans_up_tempfile():
    seen = {}

    def fake_runner(argv):
        path = argv[2]  # ["nft", "-f", <path>]
        seen["path"] = path
        seen["existed_during"] = os.path.exists(path)
        with open(path) as f:
            seen["content"] = f.read()
        return subprocess.CompletedProcess(argv, 0, "", "")

    rs = render_ruleset(wan_interface="eth0", subnet_v4="10.7.0.0/24")
    apply_ruleset(rs, runner=fake_runner)

    assert seen["existed_during"] is True       # the file existed during nft -f
    assert "flush ruleset" in seen["content"]
    assert not os.path.exists(seen["path"])      # and is removed afterwards (no leak into /tmp)
