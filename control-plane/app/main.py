"""Control-plane FastAPI app. Node-facing API + payment webhooks. Cabinet/admin UI — Phase 5."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.admin_ui import router as admin_router
from app.api.cabinet import router as cabinet_router
from app.api.nodes import router as nodes_router
from app.api.payments import router as payments_router
from app.config import get_settings
from app.logging.no_pii_filter import install_scrubbing
from app.payments.bootstrap import build_providers

try:
    from importlib.metadata import version as _pkg_version

    _VERSION = _pkg_version("vpn-control-plane")
except Exception:  # noqa: BLE001 — the version is not critical for operation, degrade to "unknown"
    _VERSION = "unknown"

# globally strip PII/client-IP/secrets from the logs (no-logs policy). We attach it to the handlers +
# lastResort, and not only to the root logger — otherwise records from cabinet/admin_ui/uvicorn.access
# (child loggers) slip past the filter. uvicorn configures logging BEFORE app is imported → it survives.
install_scrubbing()


get_settings().assert_secrets_safe()  # fail-closed: default secrets in prod → refuse to start
build_providers()

# The API docs (/docs, /redoc, /openapi.json) are disabled outside dev mode: in prod they
# leaked, without any authorization, the map of all 28 endpoints (incl. /v1/nodes/*, /admin/*).
# Found by a ptai scan on 2026-07-06. CP_DEV_MODE=true → the docs are available locally again.
_docs_on = get_settings().dev_mode
app = FastAPI(
    title="privacyvpn control plane",
    docs_url="/docs" if _docs_on else None,
    redoc_url="/redoc" if _docs_on else None,
    openapi_url="/openapi.json" if _docs_on else None,
)
app.include_router(nodes_router)
app.include_router(payments_router)
app.include_router(cabinet_router)
app.include_router(admin_router)
if get_settings().dev_fake_payments:
    from app.api.dev import router as dev_router

    app.include_router(dev_router)

_REPO_ROOT = Path(__file__).resolve().parents[2]
app.mount("/static", StaticFiles(directory=str(_REPO_ROOT / "web" / "static")), name="static")


class HeadAsGet:
    """Serve HEAD wherever we serve GET.

    Starlette's plain Route adds HEAD to every GET route; FastAPI's APIRoute does NOT
    (routing.py: `route.methods = {method.upper() for method in methods}`), so every page
    answered HEAD with 405. bingbot and several AI fetchers issue HEAD to check freshness
    before spending a GET, and a 405 reads as an error rather than "unchanged" — the
    opposite of what a site that wants to be crawled needs.

    Done as ASGI middleware rather than by adding "HEAD" to every route's method set,
    because FastAPI ≥0.138 does not flatten included routers into app.routes — they are
    wrapped in a private _IncludedRouter, so reaching the real APIRoutes means walking
    `original_router`, and a version bump would break that silently. Rewriting the method
    on the way in depends on nothing.

    HEAD is GET without a body, so we run the same handler and drop the body on the way
    out. Headers (including Content-Length) are kept, which is what RFC 9110 asks for.
    We hand the app a COPY of the scope: the server's own scope still says HEAD, so
    uvicorn's body suppression (h11_impl: `data = b"" if method == "HEAD"`) stays correct.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "HEAD":
            return await self.app(scope, receive, send)

        async def _drop_body(message):
            if message["type"] == "http.response.body":
                message = {**message, "body": b""}
            await send(message)

        await self.app({**scope, "method": "GET"}, receive, _drop_body)


# added last → sits outermost, so it sees the raw HEAD before anything routes on it
app.add_middleware(HeadAsGet)


@app.middleware("http")
async def _security_headers(request, call_next):
    """CSP + anti-cache on dynamic pages (the account number must not settle in a cache)."""
    from app.security.headers import set_security_headers

    response = await call_next(request)
    set_security_headers(response, is_static=request.url.path.startswith("/static"))
    return response


@app.get("/healthz")
def healthz():
    """Readiness: we check the DB connection (otherwise a static ok is useless for monitoring/
    the compose healthcheck — the container is "alive", but without the DB it serves nothing).
    DB unavailable → 503."""
    from fastapi import Response
    from sqlalchemy import text

    from app.db import SessionLocal

    try:
        with SessionLocal() as s:
            s.execute(text("SELECT 1"))
    except Exception:
        return Response('{"status":"db_error"}', status_code=503, media_type="application/json")
    return {"status": "ok", "version": _VERSION}
