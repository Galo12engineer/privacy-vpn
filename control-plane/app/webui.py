"""Web-UI infrastructure: templates, i18n, session helpers, login rate-limit."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Account, AdminUser
from app.security import sessions
from app.security.rate_limit import RateLimiter

REPO_ROOT = Path(__file__).resolve().parents[2]
cabinet_templates = Jinja2Templates(directory=str(REPO_ROOT / "web" / "templates"))
admin_templates = Jinja2Templates(directory=str(REPO_ROOT / "admin" / "templates"))

# Server-side dictionary translating the RU source of the templates → EN (extracted from the
# designer's i18n.js). Templates are written in Russian and wrapped in _('...'); when lang=en we
# substitute the EN string.
_EN_BY_RU = json.loads((REPO_ROOT / "web" / "i18n" / "en_by_ru.json").read_text(encoding="utf-8"))

_settings = get_settings()
# per-target limiter: the cabinet keys by IP, the admin panel by username (M7, see admin_ui.login).
login_limiter = RateLimiter(_settings.login_rate_max, _settings.login_rate_window)
# global admin-login ceiling: catches floods (and bounds the growth of the per-username key dict).
admin_flood_limiter = RateLimiter(30, _settings.login_rate_window)
# global ceiling on invoice creation: without it an anonymous user mints account+invoice without
# limit + 2 BlockBee calls per request. Caddy strips the client-IP → there is no reliable per-client
# key, hence a coarse global limit: generous for MVP demand, but bounds runaway abuse. Over the
# limit → 429.
buy_limiter = RateLimiter(60, 60)
# global ceiling on FAILED promo-code attempts. buy_limiter does not cover this: validating a code
# creates no invoice, so a code-guesser never touches that bucket. Only failures draw on this one —
# a customer with a working code is never throttled. Global for the same reason as buy_limiter:
# Caddy strips the client IP (no-logs), so there is no honest per-client key. See
# cabinet._resolve_promo for what this is and is not defending.
promo_limiter = RateLimiter(30, 60)
# global ceiling on the /buy PREVIEW (the Apply button). That branch returns before buy_limiter and
# creates nothing, so neither of the other two buckets covers it — leaving an unauthenticated
# DB-query-plus-full-page-render reachable without limit, over .onion where there is no client IP
# to key on. Higher than promo_limiter because a real customer previewing repeatedly is normal.
preview_limiter = RateLimiter(120, 60)
# global ceiling on FAILED gift-token attempts (/redeem). Only failures draw on it, so a buyer with
# a real token is never turned away by someone else's flood. An 80-bit token cannot be guessed at
# any rate — this bounds the DoS surface of an unauthenticated lookup-plus-render, nothing more.
redeem_limiter = RateLimiter(30, 60)


LANGS = ("en", "ru")


def get_lang(request: Request) -> str:
    """EN is the primary language; RU is opt-in (cookie `lang`, set by the /lang route).

    Switching happens via ordinary links in the topbar (no JS, .onion-friendly). RU = passthrough
    of the templates' source strings, EN = the server-side en_by_ru.json dictionary.
    """
    lang = request.cookies.get("lang", "")
    return lang if lang in LANGS else "en"


def translator(lang: str):
    """Returns the translation function for templates. RU = passthrough, EN = dictionary lookup.

    No-JS-friendly: translation happens on the server, does not depend on JavaScript (important
    for .onion).
    """
    if lang != "en":
        return lambda s="": s

    def _t(s: str = "") -> str:
        if not s:
            return s
        return _EN_BY_RU.get(s.strip(), s)

    return _t


def current_account(request: Request, session: Session) -> Account | None:
    token = request.cookies.get("session")
    if not token:
        return None
    aid = sessions.verify(token, scope="cabinet")
    if aid is None:
        return None
    account = session.get(Account, aid)
    if account is None:
        return None
    # #17: the token is bound to session_epoch → "log out everywhere" (an increment) invalidates
    # old cabinet cookies. read() returns a payload with bnd; old tokens without bnd → None != "0"
    # → rejected.
    data = sessions.read(token)
    if not data or data.get("bnd") != str(account.session_epoch):
        return None
    return account


def admin_session_binding(admin: AdminUser) -> str:
    """Admin-session stamp derived from password_hash (M5): a password change revokes old tokens."""
    secret = get_settings().session_secret.encode()
    msg = ("admin-stamp:" + (admin.password_hash or "")).encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:16]


def current_admin(request: Request, session: Session) -> AdminUser | None:
    token = request.cookies.get("admin_session")
    if not token:
        return None
    data = sessions.read(token)
    if not data or data.get("scope") != "admin":
        return None
    try:
        aid = uuid.UUID(data["sub"])
    except (ValueError, KeyError):
        return None
    admin = session.get(AdminUser, aid)
    if not (admin and admin.is_active):
        return None
    # revocation: the token is bound to the password_hash stamp — a mismatch → rejected
    if data.get("bnd") != admin_session_binding(admin):
        return None
    return admin


def set_cookie(
    response, name: str, value: str, *, max_age: int | None = None, secure: bool | None = None
) -> None:
    # secure=None → take the global cookie_secure. An explicit override is needed for the admin
    # panel over .onion: it is served over http (inside Tor's encryption), and the browser drops a
    # Secure cookie over http → without the override a login over Tor would not hold the session.
    response.set_cookie(
        name,
        value,
        httponly=True,
        secure=get_settings().cookie_secure if secure is None else secure,
        samesite="lax",
        max_age=max_age if max_age is not None else get_settings().session_ttl_seconds,
    )


# --- Jinja shims for the designer's templates (he wrote them for Flask: url_for/_/flash/links) ---
_ROUTE_TABLE = {
    "landing": "/",
    "login": "/login",
    "logout": "/logout",
    "dashboard": "/dashboard",
    "add_device": "/devices/add",
    "device_create": "/devices/add",
    "purchase": "/buy",
    "invoice_create": "/buy",
    "redeem": "/redeem",
    "guides": "/guides",
    "support": "/support",
    "faq": "/faq",
    "email_optin": "/email",
    # admin routes live under a secret prefix (CP_ADMIN_PATH; default /admin)
    "admin_login": f"{get_settings().admin_path}/login",
    "admin_dashboard": get_settings().admin_path,
    "admin_plans": f"{get_settings().admin_path}/plans",
    "admin_promos": f"{get_settings().admin_path}/promos",
    "admin_nodes": f"{get_settings().admin_path}/nodes",
    "admin_invoices": f"{get_settings().admin_path}/invoices",
}


_STATIC_DIR = REPO_ROOT / "web" / "static"
_static_fingerprints: dict[str, tuple[float, str]] = {}


def static_fingerprint(filename: str) -> str:
    """Short content hash for /static URLs. Empty string if the file is missing.

    Static is served with `Cache-Control: public, max-age=3600` (security/headers.py), so after a
    deploy a visitor keeps the OLD style.css/app.js for up to an hour while getting the NEW HTML.
    That mix renders visibly broken — it is exactly how the hero account number came out as one
    unbroken run of digits: fresh markup (groups in <span>) against a cached stylesheet that had no
    rule to space them. A stale wg-keys.js/tt-glue.js would be worse: those carry the browser
    keypair generation and the local private-key substitution.

    Keyed on mtime so a changed file is re-hashed without a restart (the dev server reloads
    templates but not this module's state), and unchanged files still hit the browser cache.

    `filename` only ever comes from the templates, but it is validated anyway: `_STATIC_DIR / "/x"`
    would discard the base entirely (pathlib join semantics) and `..` is not rejected on its own.
    Anything outside web/static, or not a regular file, degrades to an unversioned URL rather than
    hashing an arbitrary file or raising inside a template render.
    """
    if not filename or filename.startswith("/") or ".." in filename:
        return ""
    path = _STATIC_DIR / filename
    try:
        if not path.is_file():
            return ""
        mtime = path.stat().st_mtime
        cached = _static_fingerprints.get(filename)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    except OSError:
        return ""
    _static_fingerprints[filename] = (mtime, digest)
    return digest


def jinja_url_for(endpoint: str, **values) -> str:
    if endpoint == "static":
        filename = values.get("filename", "")
        fingerprint = static_fingerprint(filename)
        return f"/static/{filename}?v={fingerprint}" if fingerprint else f"/static/{filename}"
    if endpoint == "device_delete":
        return f"/devices/{values.get('id')}/remove"
    if endpoint == "invoice":
        return f"/invoice/{values.get('id')}"
    if endpoint == "guide":
        return f"/guides/{values.get('os', '')}"
    if endpoint == "admin_plan_save":
        return f"{get_settings().admin_path}/plans/{values.get('id', '')}"
    if endpoint == "conf_download":
        return "#"
    return _ROUTE_TABLE.get(endpoint, "#")


_LINKS = {
    "nolog": "/transparency#nolog",
    "canary": "/transparency#canary",
    # "audit" was dropped together with the #audit section on /transparency: there is no audit, and
    # a section about its absence advertised the gap. Its only consumer was the base.html footer.
    "ram": "/transparency#ram",
    "aup": "/aup",
    "privacy": "/privacy",
    "terms": "/terms",
    "onion": "http://privacyvpnexampleonionaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion",
}

@pass_context
def days_phrase(ctx, count) -> str:
    """"N days" in the request's language, with a correct singular ("1 day", "1 день").

    The dictionary is keyed by the Russian source, so one key cannot carry both English forms —
    hence the branch lives here, once, instead of in five templates. `pass_context` is required
    because the real translator is injected per request into the context by `_ctx`, not registered
    as a global; without a translator it degrades to the Russian source.
    """
    tr = ctx.get("_") or (lambda s="": s)
    try:
        n = int(count)
    except (TypeError, ValueError):
        return f"{count} {tr('дн.')}"  # not a number — print it as it came, do not break the page
    return f"{n} " + (tr("день") if n == 1 else tr("дн."))


for _env in (cabinet_templates.env, admin_templates.env):
    _env.globals["url_for"] = jinja_url_for
    _env.globals["days_phrase"] = days_phrase
    # Default only: a RU passthrough for templates rendered outside a request. Real requests get
    # the per-request translator() injected into the context by _ctx — translation is server-side.
    _env.globals["_"] = lambda s="": s
    _env.globals["gettext"] = lambda s="": s
    _env.globals["get_flashed_messages"] = lambda *a, **k: []
    _env.globals["links"] = _LINKS
