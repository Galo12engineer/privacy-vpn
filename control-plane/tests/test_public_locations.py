"""Exit locations must be readable on the PUBLIC pages, and must come from the DB.

Until now the country list lived only in the add-device picker, behind the login, so a visitor —
and anything reading the page on their behalf — could not learn where the exits are. "Which
countries do you have servers in" is one of the first things anyone asks about a VPN.

The list is data-driven (cabinet._sellable_locations) for the same reason the landing prices are:
a hardcoded list of countries becomes a lie the first time a node is added or pulled from sale.
These tests pin that property, not just the presence of some text.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Node

PUBLIC_PAGES_WITH_LOCATIONS = ["/", "/buy", "/faq"]


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def sellable_nodes(session):
    """Two on sale, one deliberately not — the third must never reach a public page."""
    for name, code, location, subnet, for_sale in (
        ("nl-1", "nl", "Netherlands", "10.71.0.0/24", True),
        ("ro-1", "ro", "Romania", "10.72.0.0/24", True),
        ("xx-1", "xx", "Atlantis", "10.73.0.0/24", False),
    ):
        session.add(
            Node(
                name=name,
                endpoint_host=f"{name}.example.net",
                location_code=code,
                location_name=location,
                subnet_v4=subnet,
                dns_ip=subnet.replace("0/24", "1"),
                available_for_sale=for_sale,
            )
        )
    session.commit()


@pytest.mark.parametrize("path", PUBLIC_PAGES_WITH_LOCATIONS)
def test_public_page_names_the_live_locations(client, sellable_nodes, path):
    body = client.get(path).text
    assert "Netherlands" in body
    assert "Romania" in body


@pytest.mark.parametrize("path", PUBLIC_PAGES_WITH_LOCATIONS)
def test_node_not_for_sale_is_never_advertised(client, sellable_nodes, path):
    """available_for_sale=False must not leak: advertising an exit we do not sell is a false claim."""
    assert "Atlantis" not in client.get(path).text


@pytest.mark.parametrize("path", PUBLIC_PAGES_WITH_LOCATIONS)
def test_pages_render_with_no_sellable_nodes(client, path):
    """No nodes on sale → the pages must still render, without an empty "Locations: ." stub."""
    r = client.get(path)
    assert r.status_code == 200
    assert "Locations available now:" not in r.text
    assert "Locations included in every plan:" not in r.text


def test_locations_are_deduplicated(client, session):
    """Two nodes in one country must not render as "Netherlands, Netherlands".

    Asserted on the joined list rather than on a page-wide count: /faq legitimately prints the
    list in two separate answers, so counting occurrences across the page proves nothing.
    """
    for name, subnet in (("nl-1", "10.71.0.0/24"), ("nl-2", "10.74.0.0/24")):
        session.add(
            Node(
                name=name,
                endpoint_host=f"{name}.example.net",
                location_code="nl",
                location_name="Netherlands",
                subnet_v4=subnet,
                dns_ip=subnet.replace("0/24", "1"),
                available_for_sale=True,
            )
        )
    session.commit()
    body = client.get("/faq").text
    assert "Netherlands" in body
    assert "Netherlands, Netherlands" not in body


def test_faq_answers_the_literal_country_question(client, sellable_nodes):
    """The question an assistant's sub-query actually asks must exist verbatim on the page."""
    body = client.get("/faq").text
    assert "Which countries are your servers in?" in body


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
