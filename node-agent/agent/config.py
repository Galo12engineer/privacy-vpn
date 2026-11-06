from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """Agent config. env prefix AGENT_. Secrets come from cloud-init, never to disk."""

    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    wg_interface: str = "wg0"
    wan_interface: str = "eth0"
    subnet_v4: str = "10.7.0.0/24"
    subnet_v6: str | None = None
    server_address: str = "10.7.0.1/24"  # comes from the CP at registration
    wg_listen_port: int = 51820
    wg_listen_port_alt: int = 443
    # Xray (VLESS+Reality) — config in tmpfs, HandlerService on localhost. For a vless node
    # AGENT_WG_LISTEN_PORT sets the reality port (usually 443), same as the node's endpoint_port in the CP.
    xray_config_path: str = "/run/xray/config.json"
    xray_api_addr: str = "127.0.0.1:10085"

    # control channel (CP → agent). We listen on all addresses, but the nft input chain
    # opens control_port ONLY from control_allow_cidrs (H2) + TLS pinning (H1).
    listen_host: str = "0.0.0.0"
    listen_port: int = 8443
    # CIDRs allowed to access the control port (the control-plane IP). Empty →
    # the port is closed from the WAN side. Set from cloud-init: AGENT_CONTROL_ALLOW_CIDRS.
    control_allow_cidrs: str = ""

    # per-node no-log resolver
    dns_listen_ip: str = "10.7.0.1"

    # link to the control plane
    cp_base_url: str = "http://localhost:8000"
    registration_token: str | None = None
    health_interval_seconds: int = 30
    # The node→CP path drops packets (transit loss, ~20% of SYNs as measured on 29.07). A push that
    # opened a fresh connection every time paid a full TCP+TLS handshake per cycle — exactly the
    # thing being lost — and one lost handshake burned a whole interval. Two defences:
    #   keepalive: reuse ONE connection, so a handshake is paid once per health_keepalive_seconds
    #              instead of once per push. MUST stay above health_interval_seconds — httpx
    #              defaults to 5s, which would expire between pushes and hand back the old behaviour.
    #   attempts:  retry inside the interval, so a single lost packet does not cost the whole cycle.
    # Worst case per cycle = interval + attempts*timeout + (attempts-1)*retry ≈ 70s, so two full
    # cycles of failure still fit under the CP's node_stale_seconds=150 before it condemns the node.
    health_push_attempts: int = 3
    health_push_retry_seconds: float = 5.0
    health_keepalive_seconds: float = 300.0

    # persist directory: the server's WG private key + identity (node_id/agent_secret) → the node
    # survives a reboot without dropping clients. See agent/store.py.
    state_dir: str = "/var/lib/vpn-node-agent"

    # False in tests — do not touch wg/nft/network on import
    bootstrap: bool = True

    def is_cp_secure(self) -> bool:
        """#13: the node→CP channel must be encrypted — register returns agent_secret in the body.
        We allow https:// and .onion (a Tor HS E2E-encrypts even over http). localhost/127.0.0.1 is
        for dev/tests. Any other http:// (clearnet) → agent_secret leaks to a MITM → refuse."""
        u = (self.cp_base_url or "").lower()
        return (
            u.startswith("https://")
            or ".onion" in u
            or u.startswith("http://localhost")
            or u.startswith("http://127.0.0.1")
        )


@lru_cache
def get_agent_settings() -> AgentSettings:
    return AgentSettings()
