"""Self-signed TLS for the agent's control channel (H1).

The key + cert are generated IN RAM at bootstrap and placed into tmpfs (/dev/shm) —
nothing settles onto disk (RAM-only ethos, same as the wg private key). CP pins this
cert upon registration (trust-on-first-use on top of the one-time registration token) —
a separate PKI/CA is not needed. On top of TLS there remains an HMAC signature of the
body (proves possession of agent_secret) → mutual authentication.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

TLS_DIR = "/dev/shm/agent-tls"


@dataclass
class CertMaterial:
    cert_pem: str
    key_pem: str
    fingerprint: str  # colon-hex SHA-256 DER (like `openssl x509 -fingerprint -sha256`)


def fingerprint_pem(cert_pem: str) -> str:
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    der = cert.public_bytes(serialization.Encoding.DER)
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i : i + 2] for i in range(0, len(h), 2))


def generate_self_signed(common_name: str = "privacyvpn-node", *, days: int = 3650) -> CertMaterial:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    san: list[x509.GeneralName] = [x509.DNSName(common_name)]
    try:  # if common_name is an IP, add an IPAddress SAN
        san.append(x509.IPAddress(ipaddress.ip_address(common_name)))
    except ValueError:
        pass
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        # CA:TRUE → the cert is unambiguously usable as a trust-anchor when pinned on the CP side
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return CertMaterial(cert_pem=cert_pem, key_pem=key_pem, fingerprint=fingerprint_pem(cert_pem))


def write_materials(material: CertMaterial, *, directory: str = TLS_DIR) -> tuple[str, str]:
    """Write cert+key into tmpfs (0600). Return the paths for uvicorn ssl_*file."""
    os.makedirs(directory, exist_ok=True)
    os.chmod(directory, 0o700)
    cert_path = os.path.join(directory, "cert.pem")
    key_path = os.path.join(directory, "key.pem")
    for path, data in ((cert_path, material.cert_pem), (key_path, material.key_pem)):
        with open(path, "w") as f:
            f.write(data)
        os.chmod(path, 0o600)
    return cert_path, key_path
