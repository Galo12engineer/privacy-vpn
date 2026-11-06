"""Scheduler runner. systemd/cron: `python -m app.scheduler.run`. Or run_once() from cron."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import Node
from app.models.base import utcnow
from app.payments import registry
from app.payments.bootstrap import build_providers
from app.scheduler import (
    account_retention,
    invoice_expiry,
    invoice_reconcile,
    invoice_scrub,
    lifecycle,
    node_health,
    reminders,
)
from app.services import node_client
from app.services.emailer import Emailer, emailer_from_settings

log = logging.getLogger("scheduler")


def _job(session: Session, name: str, fn):
    """A single scheduler job in isolation: success → commit; error → rollback + warn, but do NOT
    bring down the rest (one SMTP error must not cancel revocations/scrub/health)."""
    try:
        result = fn()
        session.commit()
        return result
    except Exception as e:  # noqa: BLE001 — deliberately broad: job isolation
        session.rollback()
        log.warning("scheduler job %s failed: %s", name, type(e).__name__)
        return None


def default_dispatcher(session: Session):
    """Removing a peer from the node via node_client. A node failure does not break the revocation (RAM-only will reset it)."""

    def _dispatch(node_id: uuid.UUID, pubkey: str) -> None:
        node = session.get(Node, node_id)
        if node is None:
            return
        try:
            node_client.dispatch_remove_peer(node, pubkey)
        except Exception:
            pass

    return _dispatch


def run_once(
    session: Session,
    *,
    now: datetime | None = None,
    emailer: Emailer | None = None,
    dispatcher=None,
    key: str | None = None,
    listening_since: datetime | None = None,
) -> dict:
    now = now or utcnow()
    s = get_settings()
    # every job is isolated and commits on its own → a failure of one neither rolls back nor blocks the others
    _job(session, "refresh_statuses",
         lambda: lifecycle.refresh_statuses(session, now=now, grace_days=s.grace_period_days))
    revoked = _job(session, "revocations",
                   lambda: lifecycle.run_revocations(
                       session, now=now, grace_days=s.grace_period_days, dispatcher=dispatcher)) or []
    sent = (_job(session, "reminders",
                 lambda: reminders.send_due_reminders(session, emailer=emailer, now=now, key=key))
            or 0) if emailer else 0
    reconciled = _job(session, "invoice_reconcile",
                      lambda: invoice_reconcile.reconcile_pending_invoices(session, now=now)) or 0
    expired = _job(session, "invoice_expiry",
                   lambda: invoice_expiry.expire_stale_invoices(session, now=now)) or 0
    scrubbed = _job(session, "invoice_scrub",
                    lambda: invoice_scrub.scrub_due_invoices(session, now=now)) or 0
    down = _job(session, "node_health",
                lambda: node_health.sweep_node_availability(
                    session, now=now, stale_seconds=s.node_offsale_seconds,
                    listening_since=listening_since)) or []
    purged = _job(session, "account_retention",
                  lambda: account_retention.purge_stale_account_pii(session, now=now)) or 0
    return {
        "revoked": len(revoked),
        "reminders": sent,
        "reconciled": reconciled,
        "expired": expired,
        "scrubbed": scrubbed,
        "nodes_down": len(down),
        "pii_purged": purged,
    }


def main() -> None:
    s = get_settings()
    # Without this the registry in THIS process is empty and invoice_reconcile — the only safety net
    # for a payment whose callback never arrived — silently reconciles nothing. It used to be called
    # only at app.main import time, which the scheduler never does. See app/payments/bootstrap.py.
    build_providers()
    # Level matters: the container runs at WARNING, so an info line here is invisible — which is
    # how an empty registry stayed unnoticed for weeks in the first place. An empty registry IS the
    # alarm (no lost-payment recovery at all), so log it as one; the healthy case stays quiet.
    providers = registry.names()
    if providers:
        log.info("scheduler starting, payment providers: %s", providers)
    else:
        log.warning("scheduler starting with NO payment providers — lost-payment recovery DISABLED")
    emailer = emailer_from_settings(s)
    # From this instant we can actually observe health pushes. Silence older than it says nothing
    # about a node — it is our own downtime (see node_health.sweep_node_availability).
    listening_since = utcnow()
    while True:
        try:
            with SessionLocal() as session:
                run_once(session, emailer=emailer, dispatcher=default_dispatcher(session),
                         listening_since=listening_since)
        except Exception as e:  # noqa: BLE001 — the scheduler loop must NOT crash as a whole
            log.warning("scheduler cycle failed: %s", type(e).__name__)
        time.sleep(s.scheduler_interval_seconds)


if __name__ == "__main__":
    main()
