"""Attribution: postback to a Keitaro-compatible tracker on the activation event.

click_id from the UTM landing page travels into the cabinet (cookie/param) → is written onto the
account → postback fires once on activation. No link to the purchase is stored; click_id is not PII.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Account

# sender: (url, params) -> http status. Injected in tests.
Sender = Callable[[str, dict], int]


def _default_sender(url: str, params: dict) -> int:
    import httpx

    with httpx.Client(timeout=10) as client:
        return client.get(url, params=params).status_code


def fire_postback(
    click_id: str, *, base_url: str, status: str = "sale", sender: Sender | None = None
) -> int:
    sender = sender or _default_sender
    return sender(base_url, {"subid": click_id, "status": status})


def maybe_fire(
    session: Session,
    account: Account,
    *,
    base_url: str | None = None,
    sender: Sender | None = None,
) -> bool:
    """One postback per activation. True if it was sent right now."""
    base_url = base_url or get_settings().keitaro_postback_url
    if not base_url or not account.attribution_click_id or account.attribution_fired:
        return False
    fire_postback(account.attribution_click_id, base_url=base_url, sender=sender)
    account.attribution_fired = True
    return True
