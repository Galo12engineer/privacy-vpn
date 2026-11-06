"""The day count and its unit, in both languages.

This exists because of one visible bug: the English pages printed "1 days". Russian hid it — "дн."
is a correct abbreviation for every number, so nothing looked wrong in the source language, and
until gift codes nothing on the site could be one day long (the plan ladder starts at 30). The
dictionary is keyed by the Russian source, so one key cannot carry both English forms; the branch
lives in webui.days_phrase and these tests pin it there.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Account
from app.models.enums import AccountStatus
from app.security import sessions
from app.services import gift
from app.services.account_number import generate_account_number, hash_account_number
from app.webui import cabinet_templates, days_phrase
from app.webui import translator as make_translator


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    app.dependency_overrides.clear()


def _render(count, lang: str) -> str:
    """Call the helper the way Jinja does — through a template, with the request's translator."""
    template = cabinet_templates.env.from_string("{{ days_phrase(n) }}")
    return template.render(n=count, _=make_translator(lang))


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "1 day"), (2, "2 days"), (30, "30 days"), (365, "365 days"), (0, "0 days")],
)
def test_english_uses_the_singular_for_one(count, expected):
    assert _render(count, "en") == expected


@pytest.mark.parametrize(("count", "expected"), [(1, "1 день"), (30, "30 дн.")])
def test_russian_gets_the_singular_too(count, expected):
    """Not only an English fix: "1 дн." was wrong Russian, it was simply less visible."""
    assert _render(count, "ru") == expected


def test_a_non_number_does_not_break_the_render():
    """The helper is called from five templates on live pages; a bad value must degrade, not 500."""
    assert _render(None, "en") == "None days"
    assert _render("", "en") == " days"


def test_outside_a_request_it_degrades_to_the_source_language():
    """Rendered with no translator in the context (an admin template, or a render outside a
    request) it must still produce a sentence rather than raise."""
    assert cabinet_templates.env.from_string("{{ days_phrase(3) }}").render() == "3 дн."
    assert days_phrase({}, 1) == "1 день"


def test_a_one_day_gift_reads_correctly_on_the_english_page(client, session):
    """The bug as the customer met it: a one-day term on the gift preview said "1 days"."""
    token = gift.issue_batch(session, count=1, days=1)[0]
    session.commit()

    page = client.post("/redeem", data={"token": token, "action": "check"}).text

    assert "1 day" in page
    assert "1 days" not in page


def test_the_dashboard_counts_the_last_day_in_the_singular(client, session):
    from datetime import UTC, datetime, timedelta

    number = generate_account_number()
    account = Account(
        account_number_hash=hash_account_number(number, get_settings().account_number_pepper),
        status=AccountStatus.ACTIVE,
        # just under two days: the dashboard floors the remainder, so this renders as "1 day"
        expires_at=datetime.now(UTC) + timedelta(days=1, hours=12),
        device_limit=5,
    )
    session.add(account)
    session.commit()
    client.cookies.set(
        "session", sessions.issue(account.id, scope="cabinet", binding=str(account.session_epoch))
    )

    page = client.get("/dashboard").text

    assert "1 day" in page and "1 days" not in page
