"""Security headers for the web surface (cabinet/admin). Defense-in-depth.

CSP: scripts from self only (there are no inline scripts in the templates); inline styles are
allowed (the design uses them, the risk is low next to a strict script-src). There are no external
resources → CSP-friendly, .onion/Tor.
Cache-Control: no-store on dynamic pages so that the account number/invoice do not settle in the
browser/proxy cache; static files are cached.
"""

from __future__ import annotations

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)

_BASE_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    # ignored over http, takes effect behind a TLS proxy
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}


def set_security_headers(response, *, is_static: bool = False) -> None:
    for key, value in _BASE_HEADERS.items():
        response.headers[key] = value
    # static files are cached; everything else (cabinet/invoice/number) — never
    response.headers["Cache-Control"] = (
        "public, max-age=3600" if is_static else "no-store"
    )
