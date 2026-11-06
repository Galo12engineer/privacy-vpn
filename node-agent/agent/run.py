"""Real node entrypoint: bootstrap → health-loop → uvicorn. systemd: python -m agent.run."""

from __future__ import annotations

import logging
import threading
import time

import httpx
import uvicorn

from agent import bootstrap as bootstrap_mod
from agent import health, registration
from agent.config import get_agent_settings
from agent.main import app, state

log = logging.getLogger("agent.run")


def _safe_peer_count() -> int | None:
    try:
        return state.wg.peer_count() if state.wg else None
    except Exception:
        return None


def _health_limits(settings) -> httpx.Limits:
    return httpx.Limits(
        max_connections=2,
        max_keepalive_connections=2,
        keepalive_expiry=settings.health_keepalive_seconds,
    )


def _health_client(settings) -> httpx.Client:
    """One long-lived connection to the CP instead of a handshake per push (see config)."""
    return httpx.Client(timeout=10, limits=_health_limits(settings))


def _push_with_retries(settings, client, *, node_id, agent_secret, metrics, sleep=time.sleep) -> bool:
    """Retry ONLY transport errors (timeout / connection reset) — those are the lost packets we are
    fighting. A non-2xx is a decision by a CP that heard us: 401 = agent_secret desync, and hammering
    it three times per cycle would only delay the warning that #27 exists to surface."""
    for attempt in range(1, settings.health_push_attempts + 1):
        try:
            registration.push_health(
                settings,
                node_id=node_id,
                agent_secret=agent_secret,
                metrics=metrics,
                client=client,
            )
            return True
        except httpx.TransportError as e:
            if attempt == settings.health_push_attempts:
                log.warning("health push failed after %d attempts: %s", attempt, e)
                return False
            sleep(settings.health_push_retry_seconds)
        except Exception as e:
            log.warning("health push failed: %s", e)  # best-effort, but visible in the logs
            return False
    return False


def _health_loop(settings) -> None:
    with _health_client(settings) as client:
        while True:
            time.sleep(settings.health_interval_seconds)
            if not (state.node_id and state.agent_secret):
                continue
            metrics = health.collect(peer_count=_safe_peer_count())
            _push_with_retries(
                settings,
                client,
                node_id=state.node_id,
                agent_secret=state.agent_secret,
                metrics=metrics,
            )


def main() -> None:
    settings = get_agent_settings()
    if settings.bootstrap:
        bootstrap_mod.bootstrap(settings, state)
        threading.Thread(target=_health_loop, args=(settings,), daemon=True).start()
    # H1: the control channel comes up ONLY over TLS (the cert is generated in bootstrap, in tmpfs).
    # Without a cert (bootstrap skipped) we forbid startup — no plaintext channel whatsoever.
    if not (state.tls_cert_path and state.tls_key_path):
        raise RuntimeError("no TLS cert for the control channel — bootstrap did not run; refusing plaintext")
    uvicorn.run(
        app,
        host=settings.listen_host,
        port=settings.listen_port,
        ssl_certfile=state.tls_cert_path,
        ssl_keyfile=state.tls_key_path,
    )


if __name__ == "__main__":
    main()
