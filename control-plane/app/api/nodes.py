"""Node-facing API: auto-registration and ingestion of aggregate health metrics."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Node, utcnow
from app.models.enums import NodeStatus
from app.security import signing
from app.services import node_registration

router = APIRouter(prefix="/v1/nodes", tags=["nodes"])

# #22: RAM-TTL replay-guard, one per process — the signature already depends on the node's
# secret, so a guard shared across all nodes gives no cross-node collisions (you'd need the secret).
_replay_guard = signing.ReplayGuard()


@router.post("/register")
async def register(request: Request, session: Session = Depends(get_session)):
    data = await request.json()
    try:
        out = node_registration.register_node(
            session,
            token=data["token"],
            public_key=data["public_key"],
            endpoint_host=data.get("endpoint_host"),
            endpoint_port=data.get("endpoint_port"),
            agent_tls_cert=data.get("agent_tls_cert"),
        )
    except KeyError:
        raise HTTPException(status_code=422, detail="missing field")
    except node_registration.RegistrationError:
        # identical response to "no token" — we don't disclose the existence of nodes
        raise HTTPException(status_code=403, detail="invalid registration token")
    session.commit()
    return out


@router.post("/{node_id}/reattach")
async def reattach(node_id: uuid.UUID, request: Request, session: Session = Depends(get_session)):
    """Node coming back after a reboot: authenticated by agent_secret (HMAC, WITHOUT the one-time
    token — it has already been consumed). Re-pins a fresh TLS cert, marks the node active and
    returns the active peers for reconciling wg state."""
    node = session.get(Node, node_id)
    if node is None or not node.agent_secret:
        raise HTTPException(status_code=404, detail="unknown node")
    body = await request.body()
    if not signing.verify(
        node.agent_secret,
        body,
        request.headers.get("X-Timestamp", ""),
        request.headers.get("X-Signature", ""),
        replay_guard=_replay_guard,
    ):
        raise HTTPException(status_code=401, detail="bad signature")
    data = json.loads(body or b"{}")
    out = node_registration.reattach_node(
        session,
        node,
        public_key=data.get("public_key"),
        endpoint_host=data.get("endpoint_host"),
        endpoint_port=data.get("endpoint_port"),
        agent_tls_cert=data.get("agent_tls_cert"),
    )
    session.commit()
    return out


@router.post("/{node_id}/health", status_code=204)
async def health(node_id: uuid.UUID, request: Request, session: Session = Depends(get_session)):
    node = session.get(Node, node_id)
    if node is None or not node.agent_secret:
        raise HTTPException(status_code=404, detail="unknown node")
    body = await request.body()
    if not signing.verify(
        node.agent_secret,
        body,
        request.headers.get("X-Timestamp", ""),
        request.headers.get("X-Signature", ""),
        replay_guard=_replay_guard,
    ):
        raise HTTPException(status_code=401, detail="bad signature")
    data = json.loads(body or b"{}")
    node.health_cpu_pct = data.get("cpu_pct")
    node.health_mem_pct = data.get("mem_pct")
    node.health_peers = data.get("peers")
    node.last_health_at = utcnow()
    # node came back after a failure → active again and back on sale
    if node.status == NodeStatus.DOWN:
        node.status = NodeStatus.ACTIVE
        node.available_for_sale = True
    session.commit()
    return Response(status_code=204)
