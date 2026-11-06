"""The config-ready page must guide desktop users, not only phones.

Dogfood finding (Linux buyer): after adding a device the page framed everything around a phone —
"You need the WireGuard app", "scan the QR", a phone-only QR — and linked only to the generic
/guides index. A desktop user was left with a .conf file and a phone QR and no idea what to do.
The page now states both paths (desktop file-import, phone QR) and links every per-OS guide.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Account
from app.models.enums import AccountStatus, NodeStatus
from app.payments import registry
from app.services.account_number import generate_account_number, hash_account_number


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    app.dependency_overrides.clear()
    registry.clear()


def _config_page(client, session, node):
    """Drive the real add-device path so device_result.html renders as a customer sees it."""
    number = generate_account_number()
    session.add(
        Account(
            account_number_hash=hash_account_number(number, get_settings().account_number_pepper),
            status=AccountStatus.ACTIVE,
            expires_at=datetime.now(UTC) + timedelta(days=30),
            device_limit=5,
        )
    )
    node.available_for_sale = True
    node.status = NodeStatus.ACTIVE
    node.public_key = "SERVER_PUBKEY="
    session.commit()
    client.post("/login", data={"account_number": number})
    return client.post(
        "/devices/add",
        data={"public_key": "CLIENT_PUBKEY", "node_id": str(node.id), "name": "asss"},
    )


def test_config_page_covers_desktop_and_phone_and_links_every_os_guide(client, session, node):
    r = _config_page(client, session, node)

    assert r.status_code == 200, r.text[:400]
    body = r.text
    assert "Desktop" in body or "Компьютер" in body
    assert "Phone" in body or "Телефон" in body
    for os_name in ("windows", "macos", "linux", "android", "ios"):
        assert f"/guides/{os_name}" in body, f"missing link to the {os_name} guide"


def test_guide_links_open_in_a_new_tab(client, session, node):
    """The config page is no-store; a same-tab guide click + Back = "Document Expired" and the
    .conf (and its key, which lives only here) is gone. Guides must open in a new tab."""
    r = _config_page(client, session, node)

    body = r.text
    import re

    guide_links = re.findall(r'<a[^>]*href="/guides/(?:windows|macos|linux|android|ios)"[^>]*>', body)
    assert len(guide_links) == 5, f"expected 5 OS guide links, found {len(guide_links)}"
    for tag in guide_links:
        assert 'target="_blank"' in tag, f"guide link must open a new tab: {tag}"
        assert "noopener" in tag, f"a _blank link needs rel=noopener: {tag}"


def test_desktop_instruction_names_file_import(client, session, node):
    r = _config_page(client, session, node)

    body = r.text
    assert ".conf" in body
    assert "Import" in body or "Импорт" in body, "desktop users need the file-import step spelled out"
