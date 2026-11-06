"""The dashboard must surface a live pending invoice, so a lost payment tab is recoverable.

Dogfood finding: create an invoice → press Back → Firefox "Document Expired" (the page is
Cache-Control: no-store) → the payment address is gone with no route back to it. The dashboard
never linked the invoice. The session cookie set at POST /buy survives a tab close, so /dashboard
is the natural recovery path — it now shows the pending invoice with a link to /invoice/{id}.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Account, Invoice
from app.models.enums import AccountStatus, InvoiceStatus, PaymentMethod
from app.payments import registry
from app.security import sessions
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


def _account(session, status=AccountStatus.EXPIRED) -> Account:
    a = Account(
        account_number_hash=hash_account_number(generate_account_number(), get_settings().account_number_pepper),
        status=status,
        expires_at=(datetime.now(UTC) + timedelta(days=30)) if status == AccountStatus.ACTIVE else None,
        device_limit=5,
    )
    session.add(a)
    session.commit()
    return a


def _login(client, account):
    token = sessions.issue(account.id, scope="cabinet", binding=str(account.session_epoch))
    client.cookies.set("session", token)


def _invoice(session, account, *, status=InvoiceStatus.CREATED, minutes_left=40):
    inv = Invoice(
        account_id=account.id,
        status=status,
        method=PaymentMethod.LITECOIN,
        days_granted=30,
        amount_fiat_cents=500,
        crypto_amount="0.111643",
        crypto_currency="LTC",
        payment_address="MKNagQTh1uVRMCkASJ2xLkbVVTaVHapprH",
        expires_at=datetime.now(UTC) + timedelta(minutes=minutes_left),
    )
    session.add(inv)
    session.commit()
    return inv


def test_pending_invoice_is_shown_with_a_link_back(client, session):
    acc = _account(session)
    inv = _invoice(session, acc)
    _login(client, acc)

    r = client.get("/dashboard")

    assert r.status_code == 200
    assert f"/invoice/{inv.id}" in r.text, "the dashboard must link back to the payment page"
    assert "0.111643" in r.text and "LTC" in r.text


def test_no_banner_without_a_pending_invoice(client, session):
    acc = _account(session, status=AccountStatus.ACTIVE)
    _login(client, acc)

    r = client.get("/dashboard")

    assert r.status_code == 200
    assert "/invoice/" not in r.text


def test_expired_invoice_is_not_offered(client, session):
    """A dead invoice must not be advertised as payable — that would send coins into a void."""
    acc = _account(session)
    _invoice(session, acc, minutes_left=-1)  # already past its deadline
    _login(client, acc)

    r = client.get("/dashboard")

    assert r.status_code == 200
    assert "/invoice/" not in r.text


def test_paid_invoice_is_not_shown_as_pending(client, session):
    acc = _account(session, status=AccountStatus.ACTIVE)
    _invoice(session, acc, status=InvoiceStatus.PAID)
    _login(client, acc)

    r = client.get("/dashboard")

    assert r.status_code == 200
    assert "/invoice/" not in r.text
