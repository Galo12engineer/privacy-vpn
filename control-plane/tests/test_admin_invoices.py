"""The invoices page must not call a paid invoice unpaid.

The console used to count "Paid" as `selectattr('paid_at')`. But scrub_invoice_linkage NULLs
paid_at on purpose (the exact instant of payment correlates with Subscription.starts_at and rebuilds
the payment↔account link over a public blockchain). So every invoice that actually did its job —
paid, activated, scrubbed — was reported to the operator as unpaid: prod showed "Paid 0 / 4"
with two PAID rows on the screen. The state below (PAID + scrubbed_at + paid_at IS NULL) is exactly
what prod produces, not a state invented for the test: see services/purchase.on_payment_settled →
billing.scrub_invoice_linkage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Invoice
from app.models.enums import InvoiceStatus, PaymentMethod
from app.services import admin_auth, admin_service


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False
    get_settings().email_encryption_key = Fernet.generate_key().decode()
    from app.webui import admin_flood_limiter, login_limiter

    login_limiter._buckets.clear()
    admin_flood_limiter._buckets.clear()

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    c = TestClient(app)
    admin_auth.create_admin(session, username="root", password="pw")
    session.commit()
    c.post("/admin/login", data={"username": "root", "password": "pw", "totp": ""})
    yield c
    app.dependency_overrides.clear()


def _tbody(html: str) -> str:
    """Just the table rows.

    The legend on this page quotes the same wording the cells use, so a whole-page substring match
    would pass on an empty table — the exact "green on broken code" shape this repo keeps hitting.
    Assertions about a COLUMN must look at the column.
    """
    start = html.index("<tbody>")
    return html[start:html.index("</tbody>", start)]


def _invoice(session, *, status, paid_at=None, scrubbed_at=None, days=30):
    inv = Invoice(
        method=PaymentMethod.LITECOIN,
        status=status,
        days_granted=days,
        amount_fiat_cents=500,
        paid_at=paid_at,
        scrubbed_at=scrubbed_at,
    )
    session.add(inv)
    session.commit()
    return inv


def _scrubbed_paid(session):
    """Exactly what a settled invoice looks like after the scrub: PAID, no account, no paid_at."""
    return _invoice(
        session,
        status=InvoiceStatus.PAID,
        paid_at=None,
        scrubbed_at=datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0),
    )


# --- the rule itself ---------------------------------------------------------------------------

def test_paid_is_read_from_status_not_from_paid_at(session):
    assert admin_service.invoice_is_paid(_scrubbed_paid(session)) is True
    assert admin_service.invoice_is_paid(
        _invoice(session, status=InvoiceStatus.OVERPAID, paid_at=None, scrubbed_at=None)
    ) is True
    # money NOT in: a late payer whose coins are still unconfirmed must not be counted as paid
    for st in (InvoiceStatus.CREATED, InvoiceStatus.UNDERPAID, InvoiceStatus.LATE_PAYMENT,
               InvoiceStatus.EXPIRED, InvoiceStatus.CANCELLED):
        assert admin_service.invoice_is_paid(_invoice(session, status=st)) is False, st


def test_counter_counts_a_scrubbed_paid_invoice_as_paid(session):
    _scrubbed_paid(session)
    _scrubbed_paid(session)
    _invoice(session, status=InvoiceStatus.EXPIRED)
    _invoice(session, status=InvoiceStatus.LATE_PAYMENT)

    stats = admin_service.invoice_stats(session)

    assert stats == {"total": 4, "paid": 2, "scrubbed": 2}, "prod's «Оплачено 0 / 4» was this bug"


# --- the page ----------------------------------------------------------------------------------

def test_page_shows_a_scrubbed_paid_invoice_as_paid(client, session):
    _scrubbed_paid(session)

    r = client.get("/admin/invoices")

    assert r.status_code == 200
    # the headline counter
    assert "1<small> / 1</small>" in r.text.replace("\n", ""), "1 of 1 paid"
    # the column: the badge, and an explicit reason the timestamp is missing
    rows = _tbody(r.text)
    assert "badge--ok" in rows
    assert "время стёрто скрабом" in rows


def test_page_explains_what_the_scrub_is(client, session):
    _scrubbed_paid(session)

    body = client.get("/admin/invoices").text

    assert "Как читать эту таблицу" in body
    # what the scrub actually destroys, said in the legend rather than left to be guessed
    assert "связь инвойс ↔ аккаунт" in body
    # and the trap it creates for the eye: a missing timestamp is not a missing payment
    assert "не заплатили" in body
    # the column header says what the flag means, not just "scrub"
    assert "связь стёрта" in body


def test_an_unpaid_invoice_is_still_shown_as_unpaid(client, session):
    _invoice(session, status=InvoiceStatus.LATE_PAYMENT)

    body = client.get("/admin/invoices").text

    assert "0<small> / 1</small>" in body.replace("\n", "")
    rows = _tbody(body)
    assert "время стёрто скрабом" not in rows
    assert "badge--ok badge--none" not in rows, "no «оплачен: да» badge on an unpaid invoice"


def test_totals_cover_the_whole_table_not_only_the_shown_rows(client, session, monkeypatch):
    """The counter must not silently mean "of the last N" — that is a different lie in the same box."""
    from app.api import admin_ui

    monkeypatch.setattr(admin_ui, "_INVOICE_ROWS", 1)
    _scrubbed_paid(session)
    _scrubbed_paid(session)
    _scrubbed_paid(session)

    body = client.get("/admin/invoices").text

    assert "3<small> / 3</small>" in body.replace("\n", ""), "counted over the base, not the page"
    assert _tbody(body).count("время стёрто скрабом") == 1, "the table itself is capped"


def test_invoices_page_requires_admin_auth(session):
    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    anon = TestClient(app)
    r = anon.get("/admin/invoices", follow_redirects=False)
    app.dependency_overrides.clear()

    assert r.status_code == 303
    assert r.headers["location"].endswith("/login")


def test_scrubbed_invoice_carries_no_account_linkage(session):
    """Guard for the neighbouring temptation: showing "whose payment was this" for old invoices."""
    inv = _scrubbed_paid(session)
    assert inv.account_id is None
    assert inv.payment_address is None
    # and the page never renders an account id for it
    assert inv.crypto_txs is None


def test_paid_at_still_present_is_shown_as_the_time(client, session):
    """Before the retention window closes paid_at exists — then show it, it is useful.

    A day back, not minutes: created_at is stamped "now", so a paid_at minutes away renders the same
    HH:MM as the "created" cell and the assertion below would pass on a row that printed neither.
    """
    paid_at = datetime.now(UTC) - timedelta(days=1, minutes=5)
    _invoice(session, status=InvoiceStatus.PAID, paid_at=paid_at)

    body = client.get("/admin/invoices").text

    rows = _tbody(body)
    assert "время стёрто скрабом" not in rows
    assert "badge--ok badge--none" in rows, "shown as paid"
    # the timestamp itself must be rendered, not merely "not the scrub wording"
    assert paid_at.strftime("%Y-%m-%d %H:%M") in rows
    assert "1<small> / 1</small>" in body.replace("\n", "")
