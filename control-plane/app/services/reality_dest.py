"""Validation of a Reality dest candidate: TLS 1.3 + ALPN h2 + not behind a CDN.

Reality borrows the TLS handshake of the external dest, therefore the dest MUST support TLS 1.3 and
HTTP/2, and MUST NOT sit behind a CDN (otherwise the node becomes a CDN forwarder → its IP gets flagged for
anomalous traffic; XTLS docs). The X25519 exchange cannot be checked in pure Python 3.12 (without ssl.SSLSocket.group,
which is available from 3.13) — this is a deliberate limitation of the validator (it is checked manually
via `openssl s_client ... -tls1_3` during selection, see docs/deploy.md).
"""

from __future__ import annotations

import socket
import ssl

# markers in the Server header that reveal a CDN (a node behind a CDN = bad dest)
CDN_MARKERS = ("cloudflare", "akamai", "fastly", "cloudfront", "vercel", "cachefly", "ecacc")


class DestError(ValueError):
    """dest is not suitable for Reality (not TLS1.3 / no h2 / behind a CDN / unreachable)."""


def _cdn_marker(server_header: str | None) -> str | None:
    sh = (server_header or "").lower()
    return next((m for m in CDN_MARKERS if m in sh), None)


def evaluate(*, tls_version: str | None, alpn: str | None, server_header: str | None) -> None:
    """Pure check of the probe results. TLS1.3 and h2 are mandatory; CDN detection is
    best-effort (server_header=None → skip it). Raises DestError on violation."""
    if tls_version != "TLSv1.3":
        raise DestError(f"dest не поддерживает TLS 1.3 (согласован {tls_version or 'ничего'})")
    if alpn != "h2":
        raise DestError("dest не согласует ALPN h2 — нет HTTP/2 (Reality требует h2)")
    marker = _cdn_marker(server_header)
    if marker:
        raise DestError(
            f"dest за CDN ({marker}) — нода станет форвардером CDN и IP зафлагают; выбери non-CDN"
        )


def _tls_probe(host: str, port: int, alpn: list[str], timeout: float) -> tuple[str | None, str | None]:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.set_alpn_protocols(alpn)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock, \
                ctx.wrap_socket(sock, server_hostname=host) as tls:
            return tls.version(), tls.selected_alpn_protocol()
    except ssl.SSLError as e:
        raise DestError(f"dest не прошёл TLS 1.3 хендшейк: {type(e).__name__}") from e
    except OSError as e:
        raise DestError(f"dest недоступен ({host}:{port}): {type(e).__name__}") from e


def _server_header(host: str, port: int, timeout: float) -> str | None:
    """Best-effort Server header via a separate HTTP/1.1 connection (for CDN detection)."""
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock, \
                ctx.wrap_socket(sock, server_hostname=host) as tls:
            tls.sendall(
                f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
            )
            raw = tls.recv(2048).decode("latin-1", "replace")
    except OSError:
        return None
    for line in raw.split("\r\n"):
        key, _, value = line.partition(":")
        if key.strip().lower() == "server":
            return value.strip()
    return None


def probe(host: str, port: int = 443, *, timeout: float = 6.0) -> None:
    """Network check of the dest → evaluate(). TLS1.3+h2 are mandatory, CDN is best-effort."""
    version, alpn = _tls_probe(host, port, ["h2", "http/1.1"], timeout)
    server_header = _server_header(host, port, timeout)
    evaluate(tls_version=version, alpn=alpn, server_header=server_header)


def check_dest(dest: str, *, timeout: float = 6.0) -> None:
    """dest in the 'host:port' format → suitability check. DestError if unsuitable."""
    host, _, port = dest.partition(":")
    if not host:
        raise DestError("пустой dest")
    probe(host, int(port) if port else 443, timeout=timeout)
