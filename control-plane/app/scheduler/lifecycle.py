"""Subscription lifecycle: active → grace → expired → auto-revocation of all peers.

Revocation is idempotent (the account.peers_revoked_at marker). Removing peers from
nodes goes through an injectable dispatcher (in tests — recording, in prod — node_client).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account
from app.models.base import utcnow
from app.models.enums import AccountStatus
from app.services.billing import evaluate_status
from app.services.provisioning import revoke_account_peers

Dispatcher = Callable[[uuid.UUID, str], None]


def refresh_statuses(session: Session, *, now: datetime | None = None, grace_days: int = 3) -> None:
    """Refresh statuses (active/grace/expired) of not-yet-revoked accounts — for the banner."""
    now = now or utcnow()
    accounts = session.execute(
        select(Account).where(
            Account.expires_at.is_not(None), Account.peers_revoked_at.is_(None)
        )
    ).scalars().all()
    for account in accounts:
        account.status = evaluate_status(account, now=now, grace_days=grace_days)


def run_revocations(
    session: Session,
    *,
    now: datetime | None = None,
    grace_days: int = 3,
    dispatcher: Dispatcher | None = None,
) -> list[tuple[uuid.UUID, list[tuple[uuid.UUID, str]]]]:
    """Remove peers of accounts whose grace has run out. Returns [(account_id, [(node_id, pubkey)...])]."""
    now = now or utcnow()
    result: list[tuple[uuid.UUID, list[tuple[uuid.UUID, str]]]] = []
    accounts = session.execute(
        select(Account).where(
            Account.expires_at.is_not(None), Account.peers_revoked_at.is_(None)
        )
    ).scalars().all()
    for account in accounts:
        if evaluate_status(account, now=now, grace_days=grace_days) != AccountStatus.EXPIRED:
            continue
        targets = revoke_account_peers(session, account, now=now)  # sets peers_revoked_at
        account.status = AccountStatus.EXPIRED
        if dispatcher:
            for node_id, pubkey in targets:
                dispatcher(node_id, pubkey)
        result.append((account.id, targets))
    return result
