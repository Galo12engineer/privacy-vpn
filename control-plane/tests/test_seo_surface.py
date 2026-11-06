"""robots.txt / sitemap.xml / canonical / OpenGraph.

Two classes of regression are guarded here, and they are not the same thing:

1. **Leaks.** The sitemap must never grow a route it was not meant to publish. The two that would
   hurt are the secret admin prefix and `/guides/ru-vless` (required to 404 in prod, CLAUDE.md
   rule 2). This is why app/seo.PUBLIC_PATHS is hand-written and not derived from app.routes — a
   generated list would have published both the day someone added a route.

2. **Host trust.** Canonical/og:url are absolute URLs, and the only thing that decides the host is
   the Host header, which the client controls. Echoing it lets a stranger publish pages that
   declare THEIR url canonical for our content. The whitelist in app/seo.py is that guard, and
   `test_unknown_host_falls_back_to_primary` is what keeps it.

The .onion cases are the product invariant, not a nicety: an og:image pointing at the clearnet
would be an external resource on the one surface where "zero external resources" is a promise.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app import seo
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.webui import translator

ONION = "privacyvpnexampleonionaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion"


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False

    def _ov():
        yield session

    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    app.dependency_overrides.clear()


def _meta(html: str, attr: str, name: str) -> str | None:
    m = re.search(
        rf'<meta {attr}="{re.escape(name)}" content="([^"]*)"', html
    )
    return m.group(1) if m else None


def _canonical(html: str) -> str | None:
    m = re.search(r'<link rel="canonical" href="([^"]*)"', html)
    return m.group(1) if m else None


# --- the sitemap must reach only pages that actually exist for an anonymous visitor ---


@pytest.mark.parametrize("path", seo.PUBLIC_PATHS)
def test_every_sitemap_url_is_200_anonymous(client, path):
    assert client.get(path).status_code == 200


def test_sitemap_leaks_neither_admin_path_nor_gated_guide(client):
    body = client.get("/sitemap.xml").text
    assert get_settings().admin_path not in body
    assert "ru-vless" not in body
    # the gate itself must still hold — a sitemap is no use if the page it omits is reachable
    assert client.get("/guides/ru-vless").status_code == 404


def test_sitemap_lists_every_public_path(client):
    body = client.get("/sitemap.xml").text
    for path in seo.PUBLIC_PATHS:
        assert f"<loc>https://privacyvpn.example.com{path}</loc>" in body


# --- robots.txt ---


def test_robots_allows_answering_bots_and_refuses_training_only(client):
    body = client.get("/robots.txt").text
    # training-only crawlers are named and refused
    assert "User-agent: GPTBot\nDisallow: /" in body
    assert "User-agent: ClaudeBot\nDisallow: /" in body
    # answering crawlers are NOT named — they fall through to `*`, which allows. Naming
    # Claude-User with a Disallow would make the site invisible to Claude users; that is the
    # single most expensive mistake available in this file.
    for token in ("Claude-User", "OAI-SearchBot", "PerplexityBot", "DuckAssistBot"):
        assert f"User-agent: {token}" not in body
    assert "Allow: /" in body


def test_robots_does_not_publish_the_admin_prefix(client):
    """A Disallow line publishes the very prefix it pretends to protect, and robots.txt is the
    first file any scanner fetches."""
    assert get_settings().admin_path not in client.get("/robots.txt").text


def test_robots_points_at_the_sitemap_on_the_same_host(client):
    body = client.get("/robots.txt", headers={"host": "privacyvpn.example.net"}).text
    assert "Sitemap: https://privacyvpn.example.net/sitemap.xml" in body


# --- canonical / OpenGraph, per host ---


def test_canonical_is_self_referential_per_host(client):
    assert _canonical(client.get("/faq").text) == "https://privacyvpn.example.com/faq"
    # the mirror points at ITSELF, never at the primary: a seized domain is usually turned into a
    # confiscation banner, and a cross-canonical would hand that banner our identity.
    assert (
        _canonical(client.get("/faq", headers={"host": "privacyvpn.example.net"}).text)
        == "https://privacyvpn.example.net/faq"
    )


def test_unknown_host_falls_back_to_primary(client):
    """Host is client-controlled: without the whitelist this would emit
    `https://evil.example/faq` as our own canonical."""
    html = client.get("/faq", headers={"host": "evil.example"}).text
    assert _canonical(html) == "https://privacyvpn.example.com/faq"
    assert "evil.example" not in html


def test_onion_stays_same_origin(client):
    """On .onion every absolute URL we emit must point back into the onion — an og:image on the
    clearnet would be an external resource on the one surface where there must be none."""
    html = client.get("/faq", headers={"host": ONION}).text
    assert _canonical(html) == f"http://{ONION}/faq"
    assert _meta(html, "property", "og:image") == f"http://{ONION}/static/og.png"
    assert "privacyvpn.example.com" not in _meta(html, "property", "og:url")


def test_onion_sitemap_and_robots_are_onion_local(client):
    sitemap = client.get("/sitemap.xml", headers={"host": ONION}).text
    assert f"<loc>http://{ONION}/</loc>" in sitemap
    assert "https://" not in sitemap.replace("https://www.sitemaps.org", "")
    # Ahmia respects robots.txt — a Disallow here would block the only crawler that can reach us
    assert "User-agent: *\nDisallow: /\n" not in client.get("/robots.txt", headers={"host": ONION}).text


# --- meta description on every public page (it used to exist only on the landing) ---


@pytest.mark.parametrize("path", seo.PUBLIC_PATHS)
def test_every_public_page_has_a_description(client, path):
    desc = _meta(client.get(path).text, "name", "description")
    assert desc and len(desc) >= 40


@pytest.mark.parametrize("path", seo.PUBLIC_PATHS)
def test_descriptions_are_within_the_snippet_budget(path):
    """Over ~160 characters search engines truncate mid-sentence — in both languages."""
    source = seo.description_for(path)
    assert len(source) <= 160
    assert len(translator("en")(source)) <= 160


@pytest.mark.parametrize("path", seo.PUBLIC_PATHS)
def test_every_description_is_translated(path):
    """EN is the default language, and a missing key makes tr() return the Russian source — which
    is how `рекомендуем · любая сумма` shipped on the English /buy page and sat there unnoticed.
    A description is worse: it is the line search engines and AI assistants quote verbatim."""
    source = seo.description_for(path)
    assert translator("en")(source) != source, f"no EN translation for {path}"


def test_english_pages_carry_no_cyrillic_description(client):
    for path in seo.PUBLIC_PATHS:
        desc = _meta(client.get(path).text, "name", "description")
        assert not re.search(r"[А-Яа-яЁё]", desc), f"RU description on the EN page {path}"


def test_descriptions_are_page_specific(client):
    """A single site-wide description is the same as none — engines dedupe it away."""
    seen = {_meta(client.get(p).text, "name", "description") for p in seo.PUBLIC_PATHS}
    assert len(seen) >= 10


def test_og_title_mirrors_the_page_title(client):
    html = client.get("/faq").text
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()
    assert _meta(html, "property", "og:title") == title


def test_landing_carries_the_preview_card(client):
    html = client.get("/").text
    assert _meta(html, "name", "twitter:card") == "summary_large_image"
    assert _meta(html, "property", "og:image") == "https://privacyvpn.example.com/static/og.png"
    assert _meta(html, "property", "og:title").startswith("privacyvpn VPN")
