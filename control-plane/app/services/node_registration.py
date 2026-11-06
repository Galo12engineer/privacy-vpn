"""Node auto-registration. The node brings a one-time token → receives agent_secret and the network."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Node
from app.models.enums import NodeProtocol, NodeStatus
from app.services import reality_profile
from app.services.awg_profile import profile_from_node

log = logging.getLogger("node_registration")


class RegistrationError(Exception):
    pass


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def cert_fingerprint(cert_pem: str) -> str:
    """colon-hex SHA-256 of the pinned cert's DER (same as openssl -fingerprint -sha256)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    der = cert.public_bytes(serialization.Encoding.DER)
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i : i + 2] for i in range(0, len(h), 2))


def mint_registration_token(node: Node) -> str:
    """Generate a one-time token. The plaintext is handed out only once (in cloud-init)."""
    token = secrets.token_urlsafe(32)
    node.registration_token_hash = hash_token(token)
    return token


def server_address(node: Node) -> str:
    net = ipaddress.ip_network(node.subnet_v4, strict=False)
    return f"{next(net.hosts())}/{net.prefixlen}"


def _base_config(node: Node) -> dict:
    return {
        "server_address": server_address(node),
        "subnet_v4": node.subnet_v4,
        "subnet_v6": node.subnet_v6,
        "dns_ip": node.dns_ip,
        "mtu": node.mtu,
        "wg_listen_port": node.endpoint_port,
        "wg_listen_port_alt": node.endpoint_port_alt,
        # transport + profiles for the agent. amnezia=None → plain wireguard; reality=None →
        # not vless_reality. For vless_reality the agent brings up Xray with this profile.
        "protocol": node.protocol.value,
        "amnezia": profile_from_node(node),
        "reality": reality_profile.profile_from_node(node),
    }


def reattach_node(
    session: Session,
    node: Node,
    *,
    public_key: str | None = None,
    endpoint_host: str | None = None,
    endpoint_port: int | None = None,
    agent_tls_cert: str | None = None,
) -> dict:
    """Node returning after a reboot. Authentication by agent_secret is already checked in the route.
    We update pubkey/endpoint/cert (re-pinning), mark it active and return the network config +
    the list of active peers — the agent will restore wg state (after a reboot it is empty)."""
    # #18: reattach is authenticated by agent_secret only and silently overwrites
    # endpoint/cert. Changing endpoint_host or the cert fingerprint (when values are ALREADY set) is
    # a sensitive event (a leaked secret could redirect traffic / re-pin the cert):
    # we do not block reattach (a node reboot is legitimate), but log loudly for monitoring (no PII).
    if public_key:
        node.public_key = public_key
    if endpoint_host:
        if node.endpoint_host and endpoint_host != node.endpoint_host:
            log.warning(
                "node %s reattach changes endpoint_host %s->%s — check it (agent_secret leak?)",
                node.id, node.endpoint_host, endpoint_host,
            )
        node.endpoint_host = endpoint_host
    if endpoint_port:
        node.endpoint_port = endpoint_port
    if agent_tls_cert:
        new_fp = cert_fingerprint(agent_tls_cert)
        if node.mtls_cert_fingerprint and new_fp != node.mtls_cert_fingerprint:
            log.warning("node %s reattach re-pins the TLS cert (fingerprint changed)", node.id)
        node.agent_tls_cert = agent_tls_cert
        node.mtls_cert_fingerprint = new_fp
    node.status = NodeStatus.ACTIVE
    node.available_for_sale = True
    session.flush()

    from app.models import Peer
    from app.services.config_builder import server_allowed_ips

    peers = session.execute(
        select(Peer).where(Peer.node_id == node.id)
    ).scalars().all()
    peer_out = []
    for p in peers:
        if p.protocol_type == NodeProtocol.VLESS_REALITY:
            # VLESS user: the agent will apply it via the xray api (not via wg)
            peer_out.append(
                {"protocol": "vless_reality", "uuid": p.vless_uuid, "email": p.vless_email}
            )
        else:
            peer_out.append(
                {
                    "protocol": p.protocol_type.value,
                    "public_key": p.public_key,
                    "allowed_ips": server_allowed_ips(node, p.ip_lease),
                    "preshared_key": p.preshared_key,
                }
            )
    return {**_base_config(node), "peers": peer_out}


def register_node(
    session: Session,
    *,
    token: str,
    public_key: str,
    endpoint_host: str | None = None,
    endpoint_port: int | None = None,
    agent_tls_cert: str | None = None,
) -> dict:
    node = session.execute(
        select(Node).where(Node.registration_token_hash == hash_token(token))
    ).scalar_one_or_none()
    if node is None:
        raise RegistrationError("invalid registration token")

    node.public_key = public_key
    if endpoint_host:
        node.endpoint_host = endpoint_host
    if endpoint_port:
        node.endpoint_port = endpoint_port
    # H1: we pin the self-signed cert of the control channel (TOFU on top of the one-time token).
    # Without a cert the dispatch will refuse to talk over https → the channel won't silently fall back to plaintext.
    if agent_tls_cert:
        node.agent_tls_cert = agent_tls_cert
        node.mtls_cert_fingerprint = cert_fingerprint(agent_tls_cert)
    node.agent_secret = secrets.token_urlsafe(32)
    node.registration_token_hash = None  # one-time
    node.status = NodeStatus.ACTIVE
    node.available_for_sale = True
    session.flush()

    return {
        "node_id": str(node.id),
        "agent_secret": node.agent_secret,
        **_base_config(node),
    }
