"""Taking a failed node off sale: if health is older than the threshold — available_for_sale=False, DOWN."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Node
from app.models.base import as_utc, utcnow
from app.models.enums import NodeStatus

log = logging.getLogger("scheduler.node_health")


def sweep_node_availability(
    session: Session,
    *,
    now: datetime | None = None,
    stale_seconds: int = 600,
    listening_since: datetime | None = None,
) -> list[uuid.UUID]:
    """Returns the ids of nodes taken off sale in this pass.

    The caller passes node_offsale_seconds, NOT node_stale_seconds — pulling a location off /buy is
    a customer-facing action and gets the looser of the two thresholds. See config for why they are
    separate; the default here matches node_offsale_seconds so a bare call cannot silently be strict.

    listening_since: when this process became able to OBSERVE health pushes (the scheduler's start).
    Silence before that moment is not evidence about a node, and treating it as such cost real
    sales: nodes push health TO the control plane, so every second the control plane is down
    produces silence by construction. Coming back after a deploy longer than stale_seconds — which
    a rebuild routinely is — the very first sweep saw every node as unreported and took the whole
    fleet off sale. The fleet then healed itself on the next tick (agents push every 30s against a
    150s threshold), but /buy had no locations to sell in the meantime, and nothing in the logs
    said why.

    So a node is stale only if it stayed silent WHILE WE WERE LISTENING: the clock starts at
    max(last_health_at, listening_since). A genuinely dead node is still caught — just one cycle
    later. Pass None (cron-style one-shot runs) to keep the old wall-clock behaviour, since a fresh
    process there would otherwise never condemn anything.
    """
    now = now or utcnow()
    since = as_utc(listening_since)
    taken_off: list[uuid.UUID] = []
    nodes = session.execute(
        select(Node).where(Node.status.in_([NodeStatus.ACTIVE, NodeStatus.PROVISIONING]))
    ).scalars().all()
    for node in nodes:
        last = as_utc(node.last_health_at)
        seen = [t for t in (last, since) if t is not None]
        # no reference at all (never reported, no start marker) → stale, as before
        stale = not seen or (now - max(seen)).total_seconds() > stale_seconds
        if stale and node.status == NodeStatus.ACTIVE:
            node.status = NodeStatus.DOWN
            node.available_for_sale = False
            taken_off.append(node.id)
            # A location vanishing from /buy used to leave no trace at all: after the 29.07 incident
            # the logs could not answer whether the fleet had ever been pulled. Node name and silence
            # age only — no client data is involved here.
            age = int((now - max(seen)).total_seconds()) if seen else -1
            log.warning("node %s off sale: silent for %ss (limit %ss)", node.name, age, stale_seconds)
    return taken_off
