"""Gift tokens: prepaid access codes sold on someone else's counter.

The invariants that cost money or privacy if they break:
  1. the plaintext token exists in exactly one place — the operator's screen at issue time. Not in
     the database, not in a log, not in a URL.
  2. one token = one credit, including when two requests arrive at once.
  3. redeeming while logged in EXTENDS that account instead of minting a second one (a second
     account is the worst outcome for the returning buyer: their devices are bound to the first).
  4. nothing on the row records WHICH account took it — that linkage is the reason gift tokens
     exist instead of selling account numbers directly.

The race is covered twice on purpose: once against a live Postgres (two connections, real
FOR UPDATE — set CP_TEST_DATABASE_URL to run it; CI does) and once on SQLite by simulating the
committed writer the way test_promo does, because SQLite ignores FOR UPDATE entirely and would
report a green test for code with no lock in it at all.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session as OrmSession

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Account, Base, GiftToken, Invoice, PricePlan, PromoCode, Subscription
from app.models.base import as_utc
from app.security import sessions
from app.services import gift
from app.services.account_number import hash_account_number
from app.webui import redeem_limiter

# The number as the page prints it: 5 groups of 4 digits. Used to prove a number IS shown when a new
# account is minted, and — more importantly — that none is shown on the extension path.
_NUMBER = re.compile(r"\b\d{4} \d{4} \d{4} \d{4} \d{4}\b")


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False
    # module-global and shared between test files — reset both ends so this file can neither
    # poison another nor be poisoned by one
    redeem_limiter.reset("redeem")

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    redeem_limiter.reset("redeem")
    app.dependency_overrides.clear()


def _issue(session, *, days=30, count=1, **kw) -> list[str]:
    tokens = gift.issue_batch(session, count=count, days=days, **kw)
    session.commit()
    return tokens


def _one(session, **kw) -> str:
    return _issue(session, count=1, **kw)[0]


def _account(session, *, days_left: int | None = 10) -> Account:
    from app.models.enums import AccountStatus
    from app.services.account_number import generate_account_number

    number = generate_account_number()
    acc = Account(
        account_number_hash=hash_account_number(number, get_settings().account_number_pepper),
        status=AccountStatus.ACTIVE if days_left else AccountStatus.EXPIRED,
        expires_at=(datetime.now(UTC) + timedelta(days=days_left)) if days_left else None,
        device_limit=5,
    )
    session.add(acc)
    session.commit()
    return acc


def _login(client, account) -> None:
    client.cookies.set(
        "session", sessions.issue(account.id, scope="cabinet", binding=str(account.session_epoch))
    )


def _redeem(client, token: str):
    return client.post(
        "/redeem", data={"token": token, "action": "redeem"}, follow_redirects=False
    )


# --- what is stored ---------------------------------------------------------------------------
def test_the_token_itself_is_never_stored(session):
    """The whole reason this table holds a hash and promo_code holds plaintext.

    A promo code is deliberately not a secret; a gift token IS the goods, so a database dump must
    not be a stack of free subscriptions.
    """
    token = _one(session)
    row = session.query(GiftToken).one()

    body = gift.normalize(token)
    stored = " ".join(str(v) for v in row.__dict__.values())
    assert body not in stored and token not in stored
    assert row.token_hash == gift.hash_token(body)
    assert len(row.token_hash) == 64


def test_the_hash_is_peppered_and_domain_separated(session):
    """Two properties in one place, both load-bearing.

    Peppered: the same token under a different pepper is a different hash, so a stolen database
    alone does not let you recognise a token you already hold.
    Domain-separated: the gift HMAC shares the account-number pepper, and the prefix is what keeps
    the two hash spaces from ever colliding.
    """
    token = _one(session)
    body = gift.normalize(token)
    ours = gift.hash_token(body)

    settings = get_settings()
    original = settings.account_number_pepper
    try:
        settings.account_number_pepper = "a-different-pepper"
        assert gift.hash_token(body) != ours
    finally:
        settings.account_number_pepper = original

    # the same string hashed as an account number must land somewhere else entirely
    assert ours != hash_account_number("1" * 20, original)


def test_the_row_never_learns_which_account_redeemed_it(session):
    """Structural, not behavioural: an `account_id` here would rebuild exactly the linkage the
    feature exists to avoid, and it would be added by someone who thought it was useful bookkeeping.
    """
    assert not any(
        "account" in column.name for column in GiftToken.__table__.columns
    ), "gift_token must hold no link to an account"

    token = _one(session)
    gift.redeem(session, token)
    session.commit()

    row = session.query(GiftToken).one()
    account = session.query(Account).one()
    assert str(account.id) not in " ".join(str(v) for v in row.__dict__.values())


def test_the_redemption_stamp_is_coarsened_to_the_day(session):
    """Including `updated_at` — TimestampMixin would otherwise stamp this very UPDATE with the exact
    instant of redemption, i.e. the row would record to the microsecond the thing we are avoiding.
    Same trap scrub_invoice_linkage documents.

    Break check: drop `row.updated_at = day` in gift.redeem and this goes red.
    """
    token = _one(session)
    gift.redeem(session, token)
    session.commit()

    row = session.query(GiftToken).one()
    for stamp in (as_utc(row.redeemed_at), as_utc(row.updated_at)):
        assert (stamp.hour, stamp.minute, stamp.second, stamp.microsecond) == (0, 0, 0, 0)


# --- redemption: new account ------------------------------------------------------------------
def test_redeeming_without_a_session_mints_an_account_with_the_time_on_it(client, session):
    token = _one(session, days=30)

    resp = _redeem(client, token)

    assert resp.status_code == 200
    account = session.query(Account).one()
    shown = _NUMBER.search(resp.text)
    assert shown, "the account number must be printed in the response body"
    # the printed number is the REAL credential for that row, not a decorative string
    assert account.account_number_hash == hash_account_number(
        shown.group(0), get_settings().account_number_pepper
    )
    left = as_utc(account.expires_at) - datetime.now(UTC)
    assert timedelta(days=29, hours=23) < left <= timedelta(days=30)


def test_the_grant_is_exactly_what_was_issued(client, session):
    """Days and device limit are frozen at issue: the token promises them on someone else's listing,
    and a later plan edit must not change what an already-sold token delivers."""
    token = _one(session, days=7, device_limit=2, price_plan_code="1w")

    assert _redeem(client, token).status_code == 200

    account = session.query(Account).one()
    assert account.device_limit == 2
    sub = session.query(Subscription).one()
    assert (sub.days, sub.price_plan_code) == (7, "1w")
    left = as_utc(account.expires_at) - datetime.now(UTC)
    assert timedelta(days=6, hours=23) < left <= timedelta(days=7)


def test_closed_sales_do_not_block_a_redemption(client, session):
    """CP_PURCHASES_OPEN gates /buy, and deliberately not this.

    The money for a gift token changed hands somewhere else, possibly weeks ago. Refusing to honour
    it because the shop is temporarily shut would strand a customer who has already paid, with no
    way to tell the difference from a stolen code.
    """
    settings = get_settings()
    was_open = settings.purchases_open
    settings.purchases_open = False
    try:
        token = _one(session)
        assert _redeem(client, token).status_code == 200
        assert session.query(Account).count() == 1
    finally:
        settings.purchases_open = was_open


def test_a_redeemed_token_logs_the_buyer_in_on_this_device(client, session):
    """Convenience with a hard boundary: the cookie saves retyping the number into /login one screen
    later, and the page still says the number is the only way back."""
    token = _one(session)

    resp = _redeem(client, token)

    assert resp.status_code == 200
    assert client.cookies.get("session") is not None
    assert client.get("/dashboard", follow_redirects=False).status_code == 200


# --- redemption: extension --------------------------------------------------------------------
def test_redeeming_while_logged_in_extends_and_shows_no_number(client, session):
    """The case that makes gift tokens usable twice by the same person.

    Without it a second purchase means a second account: new number, new keys, every device
    re-imported. The account comes from the SESSION — never from a form field, which is what keeps
    this page from answering "does this account number exist?".
    """
    account = _account(session, days_left=10)
    before = as_utc(account.expires_at)
    _login(client, account)
    token = _one(session, days=30)

    resp = _redeem(client, token)

    assert resp.status_code == 200
    session.refresh(account)
    assert session.query(Account).count() == 1, "a second account was minted"
    assert not _NUMBER.search(resp.text), "an account number must not appear on the extension path"
    # stacked from the existing expiry, not from now — apply_credit's rule, not a reimplementation
    assert as_utc(account.expires_at) - before == timedelta(days=30)


def test_an_expired_account_in_grace_is_extended_from_now(client, session):
    """The other half of the stacking rule: dead time is not credited back."""
    account = _account(session, days_left=None)
    account.expires_at = datetime.now(UTC) - timedelta(days=2)
    session.commit()
    _login(client, account)

    assert _redeem(client, _one(session, days=30)).status_code == 200

    session.refresh(account)
    left = as_utc(account.expires_at) - datetime.now(UTC)
    assert timedelta(days=29, hours=23) < left <= timedelta(days=30)


def test_both_steps_work_without_javascript(client, session):
    """Tor Browser on "Safest" runs no JS, and this page is where money already spent turns into
    access. Both submits therefore have to be ordinary forms in the delivered HTML."""
    token = _one(session)

    form = client.get("/redeem").text
    assert re.search(r'<form[^>]+action="/redeem"', form) and 'name="token"' in form

    preview = client.post("/redeem", data={"token": token, "action": "check"}).text
    assert re.search(r'<form[^>]+action="/redeem"', preview)
    assert 'value="redeem"' in preview and 'name="token"' in preview


def test_the_cabinet_offers_redemption_next_to_renewal(client, session):
    """The door for the returning customer, where IVPN and Mullvad put it too.

    Without it the only way in is to remember that /redeem exists — and this is the one page a
    customer with time already on the clock actually visits.
    """
    account = _account(session, days_left=10)
    _login(client, account)

    page = client.get("/dashboard")

    assert page.status_code == 200
    assert 'href="/redeem"' in page.text


def test_the_page_never_asks_for_an_account_number(client, session):
    """The oracle rule. A "renew my account" field here would let anyone probe which account
    numbers are live by the difference between "not found" and "done". The session cannot be
    probed: numbers only travel through /login, which is rate-limited per number.
    """
    page = client.get("/redeem").text
    assert "account_number" not in page
    assert 'name="token"' in page


# --- one shot ---------------------------------------------------------------------------------
def test_a_token_is_refused_the_second_time(client, session):
    token = _one(session, days=30)
    assert _redeem(client, token).status_code == 200
    expires_after_first = as_utc(session.query(Account).one().expires_at)

    second = _redeem(client, token)

    assert second.status_code == 400
    assert session.query(Subscription).count() == 1
    assert as_utc(session.query(Account).one().expires_at) == expires_after_first


def test_a_committed_redemption_is_seen_through_the_identity_map(session):
    """The SQLite half of the race: another container redeemed the token and committed.

    The concurrent writer is a bare UPDATE with synchronize_session=False, so our identity map is
    NOT told — which is exactly what a second process's commit looks like from here. Mutating
    through the same ORM session would sync the map and the test would pass on broken code.

    Break check: drop `.execution_options(populate_existing=True)` in gift.redeem and this goes red —
    SQLAlchemy answers the locked SELECT from memory with redeemed_at=None and hands out a second
    subscription for one token.
    """
    token = _one(session)
    row = gift.lookup(session, token)  # pull it into the identity map, as the preview step does
    session.execute(
        update(GiftToken)
        .where(GiftToken.id == row.id)
        .values(redeemed_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    session.commit()
    assert row.redeemed_at is None, "precondition: our in-memory copy is stale"

    with pytest.raises(gift.GiftError) as e:
        gift.redeem(session, token)
    assert e.value.reason == "redeemed"


@pytest.mark.skipif(
    not os.environ.get("CP_TEST_DATABASE_URL"),
    reason="needs a live Postgres (CP_TEST_DATABASE_URL); SQLite ignores FOR UPDATE",
)
def test_two_concurrent_redemptions_yield_one_subscription():
    """The real race, on real Postgres, with two connections.

    SQLite cannot test this at all: it ignores FOR UPDATE, so the version of this code with no lock
    passes there. Here the second transaction blocks on the first's row lock, re-reads a row that
    now has redeemed_at set, and is refused — one token, one subscription, one account.
    """
    engine = create_engine(os.environ["CP_TEST_DATABASE_URL"], future=True)
    # Only the tables this test touches, and rows-not-tables on the way out: in CI this database has
    # already been migrated to head by an earlier step, and a metadata-wide drop_all would take the
    # whole schema with it.
    tables = [
        Account.__table__, PricePlan.__table__, PromoCode.__table__,
        Invoice.__table__, Subscription.__table__, GiftToken.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    try:
        with OrmSession(engine) as setup:
            token = gift.issue_batch(setup, count=1, days=30)[0]
            setup.commit()

        results: list[str] = []
        barrier = threading.Barrier(2)

        def _worker():
            with OrmSession(engine) as s:
                barrier.wait(timeout=10)
                try:
                    gift.redeem(s, token)
                    s.commit()
                    results.append("ok")
                except gift.GiftError as e:
                    s.rollback()
                    results.append(e.reason)

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert sorted(results) == ["ok", "redeemed"], results
        with OrmSession(engine) as check:
            assert len(check.execute(select(Subscription)).scalars().all()) == 1
            assert len(check.execute(select(Account)).scalars().all()) == 1
    finally:
        with OrmSession(engine) as cleanup:
            cleanup.execute(delete(Subscription))
            cleanup.execute(delete(GiftToken))
            cleanup.execute(delete(Account))
            cleanup.commit()
        engine.dispose()


# --- refusals ---------------------------------------------------------------------------------
def test_a_switched_off_token_is_refused(client, session):
    token = _one(session)
    gift.set_active(session, session.query(GiftToken).one(), False)
    session.commit()

    resp = _redeem(client, token)

    assert resp.status_code == 400
    assert session.query(Account).count() == 0


def test_an_expired_token_is_refused(client, session):
    token = _one(session, expires_at=datetime.now(UTC) - timedelta(minutes=1))

    resp = _redeem(client, token)

    assert resp.status_code == 400
    assert session.query(Account).count() == 0


def test_the_four_refusals_are_told_apart(session):
    """Distinguishable on purpose, unlike promo codes: nobody reaches these branches by guessing an
    80-bit string, and a buyer who paid for a token deserves to know which of the four happened.
    """
    now = datetime.now(UTC)
    assert _reason(session, "TT-0000-0000-0000-0000") == "unknown"
    assert _reason(session, "not-a-token") == "unknown"

    off = _one(session)
    gift.set_active(session, session.query(GiftToken).one(), False)
    session.commit()
    assert _reason(session, off) == "disabled"

    session.query(GiftToken).delete()
    session.commit()
    stale = _one(session, expires_at=now - timedelta(seconds=1))
    assert _reason(session, stale) == "expired"

    session.query(GiftToken).delete()
    session.commit()
    used = _one(session)
    gift.redeem(session, used)
    session.commit()
    assert _reason(session, used) == "redeemed"


def _reason(session, token: str) -> str:
    try:
        gift.lookup(session, token)
    except gift.GiftError as e:
        return e.reason
    return "ok"


# --- what the buyer types ---------------------------------------------------------------------
def test_normalize_survives_a_human(session):
    """Everything that happens to a code between our page and the buyer's clipboard."""
    token = _one(session)
    body = gift.normalize(token)

    for typed in (
        token.lower(),
        token.replace("-", ""),
        token.replace("-", " "),
        f"  {token}  ",
        gift.format_token(body)[len(gift.PREFIX) + 1 :],  # pasted without the TT- prefix
    ):
        assert gift.normalize(typed) == body, typed


def test_confusable_characters_are_read_the_crockford_way():
    """1/I/l and 0/O are the mistakes that actually happen when a code is retyped off a screenshot.
    The alphabet has no I, L, O or U, so mapping them is unambiguous — and refusing them would look
    to the buyer like a token that simply does not work.
    """
    assert gift.normalize("TT-I000-0000-0000-000O") == "1000000000000000"
    assert gift.normalize("tt-l000-0000-0000-0000") == "1000000000000000"
    # Not a token at all → "", which the caller reports exactly like an unknown one: a wrong length,
    # and a character the alphabet does not contain (U is the fourth one Crockford drops).
    assert gift.normalize("TT-0000-0000-0000-000") == ""
    assert gift.normalize("TT-0000-0000-0000-00000") == ""
    assert gift.normalize("TT-U000-0000-0000-0000") == ""
    assert gift.normalize("") == ""


def test_a_body_starting_with_the_prefix_is_not_truncated():
    """The prefix comes off only when what remains is exactly a body."""
    body = "TT" + "0" * 14
    assert gift.normalize(gift.format_token(body)) == body
    assert gift.normalize(body) == body


# --- batches ----------------------------------------------------------------------------------
def test_a_batch_is_distinct_tokens_each_good_once(client, session):
    tokens = _issue(session, count=5, days=30)

    assert len(set(tokens)) == 5
    assert session.query(GiftToken).count() == 5
    for token in tokens:
        assert _redeem(client, token).status_code == 200
    assert session.query(Subscription).count() == 5
    for token in tokens:
        assert _redeem(client, token).status_code == 400


def test_the_batch_refuses_impossible_orders(session):
    for kwargs in (
        {"count": 0, "days": 30},
        {"count": gift.MAX_BATCH + 1, "days": 30},
        {"count": 1, "days": 0},
        {"count": 1, "days": gift.MAX_DAYS + 1},
        {"count": 1, "days": 30, "device_limit": 0},
    ):
        with pytest.raises(ValueError):
            gift.issue_batch(session, **kwargs)
    session.rollback()
    assert session.query(GiftToken).count() == 0


# --- leakage ----------------------------------------------------------------------------------
def test_the_token_reaches_no_log_and_no_redirect(client, session, caplog):
    """Same class as the account number: a 303 would park a live credential in the browser history
    and in every proxy log on the way, and a log line would keep it forever.
    """
    token = _one(session)
    body = gift.normalize(token)

    with caplog.at_level(logging.DEBUG):
        resp = _redeem(client, token)

    assert resp.status_code == 200
    assert "location" not in {k.lower() for k in resp.headers}
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert body not in logged and token not in logged


def test_the_preview_does_not_spend_the_token(client, session):
    """The buyer sees what a code gives before pressing the irreversible button — and pressing
    Check must be repeatable, so it may not mutate anything."""
    token = _one(session, days=90, device_limit=3)

    resp = client.post("/redeem", data={"token": token, "action": "check"})

    assert resp.status_code == 200
    assert "90" in resp.text and "3" in resp.text
    assert session.query(GiftToken).one().redeemed_at is None
    assert session.query(Account).count() == 0


def test_only_failures_are_throttled(client, session):
    """A buyer holding a real token is never turned away because somebody else was flooding.

    Flooding with REAL tokens is not a thing: holding one means you can simply redeem it.
    """
    token = _one(session)
    for _ in range(40):
        client.post("/redeem", data={"token": "TT-0000-0000-0000-0000", "action": "check"})

    assert client.post(
        "/redeem", data={"token": "TT-0000-0000-0000-0001", "action": "check"}
    ).status_code == 429
    assert _redeem(client, token).status_code == 200
    assert session.query(Account).count() == 1


def test_an_empty_field_is_not_a_failed_attempt(client, session):
    """Submitting nothing is a slip, not an attempt — it must not eat the budget a real buyer needs."""
    token = _one(session)
    for _ in range(40):
        assert client.post("/redeem", data={"token": "", "action": "check"}).status_code == 400

    assert _redeem(client, token).status_code == 200
