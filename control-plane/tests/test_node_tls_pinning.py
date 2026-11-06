"""H1: pinning of the self-signed cert of the CP→node control channel.

We check: registration pins the cert + fingerprint; node_client builds an SSL context
that trusts EXACTLY the cert that was sent (real TLS handshake) and rejects a foreign one;
without a cert, dispatch over https raises NodeChannelError (no silent plaintext).
"""

from __future__ import annotations

import datetime
import socket
import ssl
import threading
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.models import Node
from app.models.enums import NodeStatus
from app.services import node_client, node_registration


def _gen_cert(cn: str = "privacyvpn-node") -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


def _try_handshake(server_cert_pem: str, server_key_pem: str, client_ctx: ssl.SSLContext) -> None:
    """Bring up a TLS server with the given cert and check that a client with client_ctx accepts it.

    Raises ssl.SSLCertVerificationError if the pinning rejects the server's cert.
    """
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as cf, \
            tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as kf:
        cf.write(server_cert_pem)
        kf.write(server_key_pem)
        cert_file, key_file = cf.name, kf.name

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_file, key_file)

    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)
    host, port = lsock.getsockname()

    def _serve():
        try:
            conn, _ = lsock.accept()
            try:
                server_ctx.wrap_socket(conn, server_side=True).close()
            except ssl.SSLError:
                pass  # the client rejected the handshake — this is expected in the negative case
        except OSError:
            pass

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    client_err: list[Exception] = []
    try:
        raw = socket.create_connection((host, port), timeout=5)
        try:
            client_ctx.wrap_socket(raw, server_hostname="privacyvpn-node").close()
        except Exception as e:  # noqa: BLE001 — we propagate the cause upwards
            client_err.append(e)
    finally:
        t.join(timeout=5)
        lsock.close()
    if client_err:
        raise client_err[0]


def _node(session, **extra) -> Node:
    n = Node(
        name=extra.pop("name", "tls-1"),
        endpoint_host="tls1.example.net",
        location_code="pa",
        location_name="Panama",
        subnet_v4="10.20.0.0/24",
        dns_ip="10.20.0.1",
        status=NodeStatus.PROVISIONING,
        **extra,
    )
    token = node_registration.mint_registration_token(n)
    session.add(n)
    session.commit()
    return n, token


def test_register_node_pins_cert_and_fingerprint(session):
    n, token = _node(session)
    cert_pem, _ = _gen_cert()
    out = node_registration.register_node(
        session, token=token, public_key="PUB=", agent_tls_cert=cert_pem
    )
    session.refresh(n)
    assert n.agent_tls_cert == cert_pem
    assert n.mtls_cert_fingerprint == node_registration.cert_fingerprint(cert_pem)
    assert out["agent_secret"]


def test_dispatch_without_cert_raises(session):
    # node without a pinned cert + scheme=https → NodeChannelError (not plaintext)
    assert get_settings().agent_control_scheme == "https"
    n = SimpleNamespace(id="n1", agent_tls_cert=None, agent_secret="s", endpoint_host="h")
    with pytest.raises(node_client.NodeChannelError):
        node_client.dispatch_remove_peer(n, "PUBKEY=")


def test_pinning_accepts_matching_cert():
    cert_pem, key_pem = _gen_cert()
    node = SimpleNamespace(id="n1", agent_tls_cert=cert_pem, agent_secret="s", endpoint_host="h")
    ctx = node_client._verify_for(node)
    assert isinstance(ctx, ssl.SSLContext)
    _try_handshake(cert_pem, key_pem, ctx)  # must not raise


def test_pinning_rejects_different_cert():
    pinned_pem, _ = _gen_cert()                      # CP pinned this one
    attacker_cert, attacker_key = _gen_cert()        # but the server presents a different one
    node = SimpleNamespace(id="n1", agent_tls_cert=pinned_pem, agent_secret="s", endpoint_host="h")
    ctx = node_client._verify_for(node)
    with pytest.raises(ssl.SSLError):
        _try_handshake(attacker_cert, attacker_key, ctx)
