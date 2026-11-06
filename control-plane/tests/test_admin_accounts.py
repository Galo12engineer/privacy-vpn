"""Operator manual grant/extend from the admin panel.

Requested after dogfood: "someone wrote me and paid — let me extend their subscription through the
admin panel: search the account, pick a plan, extend by however much." This is also the manual
path for split payments (the customer contacts support → the operator extends here).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Account, PricePlan
from app.models.base import as_utc
from app.models.enums import AccountStatus
from app.services import admin_auth, admin_service
from app.services.account_number import generate_account_number, hash_account_number

_SHOWN_NUMBER = re.compile(r'<code id="new-acct">([\d ]+)</code>')


def _shown_number(html: str) -> str:
    """The account number as the console showed it — the only place it exists after creation."""
    m = _SHOWN_NUMBER.search(html)
    assert m, "the new account number must be rendered exactly once, in the response body"
    return m.group(1).strip()


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


def _account(session, *, number=None, status=AccountStatus.EXPIRED, expires=None):
    number = number or generate_account_number()
    a = Account(
        account_number_hash=hash_account_number(number, get_settings().account_number_pepper),
        status=status,
        expires_at=expires,
        device_limit=5,
    )
    session.add(a)
    session.commit()
    return a, number


def _plan(session, *, code="1m", days=30, limit=5):
    p = PricePlan(code=code, name=f"{days}d", duration_days=days, price_fiat_cents=500,
                  fiat_currency="USD", device_limit=limit, is_active=True, sort_order=1)
    session.add(p)
    session.commit()
    return p


def test_search_finds_the_account_by_its_number(client, session):
    _, number = _account(session)

    r = client.post("/admin/accounts/search", data={"account_number": number})

    assert r.status_code == 200
    assert "Действует до" in r.text  # the account card rendered
    assert "не найден" not in r.text


def test_search_reports_not_found_and_never_echoes_the_number(client, session):
    r = client.post("/admin/accounts/search", data={"account_number": "12345678901234567890"})

    assert r.status_code == 200
    assert "не найден" in r.text
    assert "12345678901234567890" not in r.text, "a customer's number must not be echoed back"


def test_extend_by_plan_activates_and_credits_days(client, session):
    acc, _ = _account(session)
    plan = _plan(session, days=30)

    r = client.post(f"/admin/accounts/{acc.id}/extend", data={"plan_id": str(plan.id), "custom_days": ""})

    assert r.status_code == 200
    session.refresh(acc)
    assert acc.status == AccountStatus.ACTIVE
    exp = as_utc(acc.expires_at)
    assert 29 <= (exp - datetime.now(UTC)).days <= 30


def test_custom_days_override_the_plan(client, session):
    acc, _ = _account(session)
    plan = _plan(session, days=30)

    client.post(f"/admin/accounts/{acc.id}/extend", data={"plan_id": str(plan.id), "custom_days": "7"})

    session.refresh(acc)
    exp = as_utc(acc.expires_at)
    assert 6 <= (exp - datetime.now(UTC)).days <= 7, "custom days must win over the plan"


def test_extend_stacks_on_the_existing_term(client, session):
    future = datetime.now(UTC) + timedelta(days=10)
    acc, _ = _account(session, status=AccountStatus.ACTIVE, expires=future)
    _plan(session, days=30)
    plan = session.query(PricePlan).first()

    client.post(f"/admin/accounts/{acc.id}/extend", data={"plan_id": str(plan.id), "custom_days": ""})

    session.refresh(acc)
    exp = as_utc(acc.expires_at)
    assert 39 <= (exp - datetime.now(UTC)).days <= 40, "days stack on the remaining term, not from now"


def test_extend_with_nothing_selected_is_rejected(client, session):
    acc, _ = _account(session)

    r = client.post(f"/admin/accounts/{acc.id}/extend", data={"plan_id": "", "custom_days": ""})

    assert r.status_code == 200
    session.refresh(acc)
    assert acc.status == AccountStatus.EXPIRED, "no plan + no days must not activate"
    assert acc.expires_at is None


def test_accounts_page_requires_admin_auth(session):
    """Without a login the page must not be reachable."""
    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    anon = TestClient(app)
    r = anon.get("/admin/accounts", follow_redirects=False)
    app.dependency_overrides.clear()

    assert r.status_code == 303
    assert r.headers["location"].endswith("/login")


# --- linking a grant to the invoice it pays off -------------------------------------------------

def _late_invoice(session, acc, *, days, seen=True):
    """An invoice the automation may still credit on its own."""
    from decimal import Decimal

    from app.models import Invoice
    from app.models.enums import InvoiceStatus, PaymentMethod

    inv = Invoice(
        account_id=acc.id, method=PaymentMethod.LITECOIN, status=InvoiceStatus.LATE_PAYMENT,
        days_granted=days, amount_fiat_cents=500,
        crypto_amount=Decimal("1.0"), crypto_currency="LTC",
        crypto_amount_paid=Decimal("1.0") if seen else None,
        payment_address="addr-x", external_id="addr-x",
        expires_at=datetime.now(UTC) - timedelta(minutes=90),
    )
    session.add(inv)
    session.commit()
    return inv


def test_console_shows_days_and_whether_coins_were_seen(client, session):
    """Without the day count the operator cannot tell a year-long purchase from a $5 cart."""
    acc, number = _account(session)
    _late_invoice(session, acc, days=365)
    r = client.post("/admin/accounts/search", data={"account_number": number})
    assert "365 дн." in r.text
    assert "монеты видели" in r.text


def test_a_shorter_linked_grant_is_refused(client, session):
    """Linking 7 days to a 365-day purchase caps the customer at 7 FOREVER — refuse it."""
    acc, _n = _account(session)
    inv = _late_invoice(session, acc, days=365)
    before = as_utc(acc.expires_at)

    r = client.post(
        f"/admin/accounts/{acc.id}/extend",
        data={"plan_id": "", "custom_days": "7", "settle_invoice_id": str(inv.id)},
    )

    assert "навсегда обрезать" in r.text
    session.refresh(acc)
    assert as_utc(acc.expires_at) == before, "nothing must be credited on a refusal"


def test_a_grant_for_an_already_credited_invoice_says_so(client, session):
    """apply_credit dedupes; the banner must not claim days were added when none were."""
    from app.services.billing import apply_credit

    acc, _n = _account(session)
    inv = _late_invoice(session, acc, days=30)
    apply_credit(session, acc, days=30, invoice_id=inv.id)  # the automation got there first
    session.commit()
    session.refresh(acc)
    before = as_utc(acc.expires_at)

    r = client.post(
        f"/admin/accounts/{acc.id}/extend",
        data={"plan_id": "", "custom_days": "30", "settle_invoice_id": str(inv.id)},
    )

    assert "уже зачтён" in r.text
    assert "Продлено на" not in r.text, "must not report a grant that did not happen"
    session.refresh(acc)
    assert as_utc(acc.expires_at) == before


# --- the listing: filters, pagination, and what it must never print ------------------------------
#
# Everything below leans on one thing being true: the SQL filter and the badge on the row are the
# SAME rule (billing.evaluate_status). They are written twice — a WHERE clause in
# admin_service._account_status_clause and Python for display — because pagination forces the filter
# into SQL. Two expressions of one rule is exactly the shape that rots silently, hence the boundary
# tests.

def _at(days: float):
    return datetime.now(UTC) + timedelta(days=days)


def test_listing_shows_accounts_with_status_term_devices_and_created(client, session):
    acc, _ = _account(session, status=AccountStatus.ACTIVE, expires=_at(10))

    body = client.get("/admin/accounts").text

    assert admin_service.mask_account_id(acc.id) in body
    assert "активен" in body
    assert "0 / 5" in body, "devices / limit"


def test_listing_never_prints_an_account_number(client, session):
    """The server cannot print one — it holds only the HMAC. Guard against a future "helpful" field."""
    acc, number = _account(session, status=AccountStatus.ACTIVE, expires=_at(10))

    body = client.get("/admin/accounts").text

    # non-vacuous: the row IS on the page — it is identified by the masked id and nothing else
    assert admin_service.mask_account_id(acc.id) in body
    assert number not in body
    assert number.replace(" ", "") not in body
    assert "невозможно" in body, "the page must say why, not stay silent about it"


def test_filters_match_evaluate_status_on_the_boundaries(client, session):
    """The SQL WHERE must agree with the Python badge — including the exact grace edge."""
    grace = get_settings().grace_period_days
    active, _ = _account(session, expires=_at(5))
    just_lapsed, _ = _account(session, expires=_at(-0.001))       # inside grace
    edge, _ = _account(session, expires=_at(-grace + 0.01))       # last moment of grace
    over, _ = _account(session, expires=_at(-grace - 0.01))       # grace ran out
    never_paid, _ = _account(session, expires=None)

    def ids(status):
        rows = admin_service.list_account_rows(session, status=status, limit=100)
        return {r["id"] for r in rows}, {r["status"] for r in rows}

    active_ids, active_badges = ids("active")
    grace_ids, grace_badges = ids("grace")
    expired_ids, expired_badges = ids("expired")

    assert active_ids == {active.id}
    assert grace_ids == {just_lapsed.id, edge.id}
    assert expired_ids == {over.id, never_paid.id}
    # the badge each row carries agrees with the filter that selected it
    assert active_badges == {"active"}
    assert grace_badges == {"grace"}
    assert expired_badges == {"expired"}
    # and every account lands in exactly one bucket
    assert active_ids | grace_ids | expired_ids == {
        active.id, just_lapsed.id, edge.id, over.id, never_paid.id
    }


def test_filter_links_render_and_narrow_the_table(client, session):
    a, _ = _account(session, expires=_at(5))
    e, _ = _account(session, expires=None)

    body = client.get("/admin/accounts?status=active").text

    assert admin_service.mask_account_id(a.id) in body
    assert admin_service.mask_account_id(e.id) not in body
    assert "/admin/accounts?status=grace" in body, "filters are plain links (no JS)"


def test_unknown_filter_value_falls_back_to_all(client, session):
    """Seeded across all three buckets on purpose: with only an active account, a silent fallback
    to status="active" — the very regression this guards — would satisfy the assertion too."""
    active, _ = _account(session, expires=_at(5))
    grace, _ = _account(session, expires=_at(-0.001))
    expired, _ = _account(session, expires=None)

    for url in ("/admin/accounts?status=../../etc/passwd", "/admin/accounts"):
        body = client.get(url).text
        for acc in (active, grace, expired):
            assert admin_service.mask_account_id(acc.id) in body, url


def test_pagination_pages_the_listing_and_does_not_load_everything(client, session, monkeypatch):
    monkeypatch.setattr(admin_service, "ACCOUNT_PAGE_SIZE", 2)
    made = [_account(session, expires=_at(5))[0] for _ in range(5)]

    first = client.get("/admin/accounts").text
    second = client.get("/admin/accounts?page=2").text

    assert first.count('class="cellcode">••••') == 2, "one page only"
    assert "страница 1 из 3" in first
    assert "страница 2 из 3" in second
    shown = {m for m in (admin_service.mask_account_id(a.id) for a in made) if m in first}
    also = {m for m in (admin_service.mask_account_id(a.id) for a in made) if m in second}
    assert not (shown & also), "pages must not repeat rows"


def test_pagination_keeps_the_filter_and_survives_a_junk_page(client, session, monkeypatch):
    monkeypatch.setattr(admin_service, "ACCOUNT_PAGE_SIZE", 2)
    for _ in range(3):
        _account(session, expires=_at(5))

    body = client.get("/admin/accounts?status=active").text
    # &amp; — Jinja escapes the ampersand, which is the correct HTML for a query string in an href
    assert "?status=active&amp;page=2" in body, "the filter must survive the page link"

    # out-of-range / non-numeric pages must not 500 or show an empty table
    assert client.get("/admin/accounts?page=999").status_code == 200
    assert "страница 2 из 2" in client.get("/admin/accounts?page=999").text
    assert client.get("/admin/accounts?page=abc").status_code == 200


def test_listing_does_not_query_peers_once_per_row(client, session, monkeypatch):
    """Device counts come from one grouped query — at thousands of accounts N+1 is the whole cost."""
    from sqlalchemy import event

    for _ in range(4):
        _account(session, expires=_at(5))

    seen = []

    def _spy(conn, cur, stmt, *a):
        seen.append(stmt)

    event.listen(session.get_bind(), "before_cursor_execute", _spy)
    try:
        admin_service.list_account_rows(session, limit=50)
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", _spy)

    peer_queries = [s for s in seen if "FROM peer" in s]
    assert len(peer_queries) <= 1, f"expected one grouped peer query, got {len(peer_queries)}"


# --- creating an account from the console --------------------------------------------------------

def test_create_account_credits_days_and_shows_the_number_once(client, session, caplog):
    plan = _plan(session, days=30, limit=7)

    with caplog.at_level(logging.WARNING):
        r = client.post("/admin/accounts/new", data={"plan_id": str(plan.id), "custom_days": ""})

    assert r.status_code == 200, "no redirect — a 303 would park the number in the URL"
    assert "показывается один раз" in r.text

    acc = session.execute(select(Account)).scalars().one()
    assert acc.status == AccountStatus.ACTIVE
    assert 29 <= (as_utc(acc.expires_at) - datetime.now(UTC)).days <= 30
    assert acc.device_limit == 7, "the plan's device limit applies"

    number = _shown_number(r.text)
    assert hash_account_number(number, get_settings().account_number_pepper) == acc.account_number_hash
    # money-equivalent action is logged as a fact — but never with the credential
    logged = [rec.getMessage() for rec in caplog.records]
    assert any("admin grant: NEW" in line for line in logged)
    assert not any(number in line for line in logged)
    assert not any(number.replace(" ", "") in line for line in logged)


def test_the_created_number_is_never_shown_again(client, session):
    plan = _plan(session, days=30)
    number = _shown_number(
        client.post("/admin/accounts/new", data={"plan_id": str(plan.id)}).text
    )

    later = client.get("/admin/accounts").text

    assert number not in later
    assert "показывается один раз" not in later


def test_create_with_custom_days_beats_the_plan(client, session):
    plan = _plan(session, days=30)

    r = client.post("/admin/accounts/new", data={"plan_id": str(plan.id), "custom_days": "3"})

    acc = session.execute(select(Account)).scalars().one()
    assert 2 <= (as_utc(acc.expires_at) - datetime.now(UTC)).days <= 3
    assert _shown_number(r.text)


def test_create_with_days_and_no_plan_uses_the_operator_grant_defaults(client, session):
    """The plainest operator action — "give this person 30 days", no tariff. Nothing else in the
    suite reaches the plan=None branch, so its defaults were untested."""
    from app.models import Subscription

    r = client.post("/admin/accounts/new", data={"plan_id": "", "custom_days": "30"})

    assert _shown_number(r.text)
    acc = session.execute(select(Account)).scalars().one()
    assert acc.device_limit == get_settings().default_device_limit
    sub = session.execute(select(Subscription)).scalars().one()
    assert sub.price_plan_code == admin_service.OPERATOR_GRANT_CODE
    assert sub.invoice_id is None, "a goodwill grant settles no invoice"


@pytest.mark.parametrize("with_plan", [False, True], ids=["без тарифа", "с тарифом"])
def test_a_day_count_that_only_looks_numeric_is_refused_not_a_500(client, session, with_plan):
    """`"²".isdigit()` is True but `int("²")` raises — that used to be a bare 500 on a money form.

    Parametrised over the plan because the two branches fail differently: with no plan the bad value
    just lands on 0, but WITH a plan an "unparseable → fall back to the plan" rule would silently
    grant the tariff's length instead — a money form answering a question the operator did not ask.
    """
    acc, _ = _account(session)
    plan_id = str(_plan(session, days=30).id) if with_plan else ""

    for bad in ("²", "③", "٣٠", "9" * 5000, "abc"):
        created = client.post(
            "/admin/accounts/new", data={"plan_id": plan_id, "custom_days": bad}
        )
        extended = client.post(
            f"/admin/accounts/{acc.id}/extend", data={"plan_id": plan_id, "custom_days": bad}
        )

        assert created.status_code == 200, bad
        assert "Аккаунт не создан" in created.text, bad
        assert extended.status_code == 200, bad
        assert "Не продлено" in extended.text, bad
        session.refresh(acc)
        assert acc.expires_at is None, f"{bad} must not credit anything"

    assert session.execute(select(Account)).scalars().all() == [acc]


def test_an_empty_day_field_still_means_the_plan(client, session):
    """The refusal above must not swallow the normal case: blank field = «use the tariff»."""
    plan = _plan(session, days=30)

    r = client.post("/admin/accounts/new", data={"plan_id": str(plan.id), "custom_days": "  "})

    assert _shown_number(r.text)
    acc = session.execute(select(Account)).scalars().one()
    assert 29 <= (as_utc(acc.expires_at) - datetime.now(UTC)).days <= 30


def test_create_with_nothing_selected_creates_nothing(client, session):
    r = client.post("/admin/accounts/new", data={"plan_id": "", "custom_days": ""})

    assert r.status_code == 200
    assert "Аккаунт не создан" in r.text
    assert session.execute(select(Account)).scalars().all() == []


def test_create_refuses_an_absurd_day_count_instead_of_crashing(client, session):
    """apply_credit does expires_at + timedelta(days=N); datetime overflows around 2.7M days."""
    r = client.post("/admin/accounts/new", data={"plan_id": "", "custom_days": "99999999"})

    assert r.status_code == 200, "must not be a 500 on a money form"
    assert "Аккаунт не создан" in r.text
    assert session.execute(select(Account)).scalars().all() == []


def test_extend_also_refuses_an_absurd_day_count(client, session):
    acc, _ = _account(session)

    r = client.post(f"/admin/accounts/{acc.id}/extend", data={"custom_days": "99999999"})

    assert r.status_code == 200
    session.refresh(acc)
    assert acc.expires_at is None


def test_create_account_requires_admin_auth(session):
    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    anon = TestClient(app)
    r = anon.post("/admin/accounts/new", data={"custom_days": "30"}, follow_redirects=False)
    app.dependency_overrides.clear()

    assert r.status_code == 303
    assert session.execute(select(Account)).scalars().all() == [], "anonymous POST must grant nothing"


def test_grant_time_new_mints_and_credits_like_the_console(session, monkeypatch):
    """grant_time.py --new and the console must not drift on what a new account gets.

    Asserted by RUNNING the script, not by grepping its source for "create_account": the diff that
    extracted the shared helper also added a comment naming it, so a source match is satisfied by
    the comment alone and survives gutting the branch entirely.
    """
    import contextlib
    import io
    import sys

    import scripts.grant_time as gt

    @contextlib.contextmanager
    def _fake_session():
        yield session

    monkeypatch.setattr(gt, "SessionLocal", _fake_session)
    monkeypatch.setattr(sys, "argv", ["grant_time.py", "--new", "--days", "30"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert gt.main() == 0

    printed = out.getvalue()
    m = re.search(r"ACCOUNT_NUMBER=([\d ]+)", printed)
    assert m, "--new must print the minted number (it exists nowhere else)"
    number = m.group(1).strip()
    acc = session.execute(
        select(Account).where(
            Account.account_number_hash
            == hash_account_number(number, get_settings().account_number_pepper)
        )
    ).scalars().one()
    assert acc.status == AccountStatus.ACTIVE
    assert 29 <= (as_utc(acc.expires_at) - datetime.now(UTC)).days <= 30


# --- an unanswered "which invoice?" is not a goodwill grant --------------------------------------
#
# The console has always WARNED that granting days without linking them lets reconcile credit the
# same coins again later. But "against no invoice" was pre-selected, so "forgot to answer" and "decided to
# gift" arrived identically — the double-payout the settle_invoice machinery exists to prevent, one
# step short of being closed.

def test_extending_an_account_with_rescuable_invoices_demands_a_choice(client, session):
    acc, _ = _account(session)
    _late_invoice(session, acc, days=30)

    r = client.post(f"/admin/accounts/{acc.id}/extend", data={"custom_days": "30"})

    assert r.status_code == 200
    assert "не указано, за какой счёт" in r.text
    session.refresh(acc)
    assert acc.expires_at is None, "nothing may be credited while the question is unanswered"


def test_an_explicit_goodwill_grant_still_works(client, session):
    """Refusing the blank must not remove the legitimate «this pays off nothing» path."""
    from app.api.admin_ui import GOODWILL_GRANT

    acc, _ = _account(session)
    inv = _late_invoice(session, acc, days=30)

    r = client.post(
        f"/admin/accounts/{acc.id}/extend",
        data={"custom_days": "7", "settle_invoice_id": GOODWILL_GRANT},
    )

    assert "Продлено на 7 дн." in r.text
    session.refresh(acc)
    assert 6 <= (as_utc(acc.expires_at) - datetime.now(UTC)).days <= 7
    session.refresh(inv)
    assert inv.status.value == "late_payment", "a goodwill grant must leave the invoice rescuable"


def test_the_goodwill_sentinel_is_not_pre_selected_in_the_form(client, session):
    """The whole point: both answers cost a click. A pre-checked default recreates the bug."""
    acc, number = _account(session)
    _late_invoice(session, acc, days=30)

    body = client.post("/admin/accounts/search", data={"account_number": number}).text

    radios = re.findall(r'<input type="radio" name="settle_invoice_id"[^>]*>', body)
    assert len(radios) == 2, f"one invoice + the goodwill option, got: {radios}"
    assert not any("checked" in r for r in radios), "nothing may be pre-selected"
    assert all("required" in r for r in radios), "browsers should catch it before the POST too"


def test_an_account_without_rescuable_invoices_is_unaffected(client, session):
    """The demand applies only where there is something to double-credit."""
    acc, _ = _account(session)

    r = client.post(f"/admin/accounts/{acc.id}/extend", data={"custom_days": "30"})

    assert "Продлено на 30 дн." in r.text
    session.refresh(acc)
    assert acc.status == AccountStatus.ACTIVE
