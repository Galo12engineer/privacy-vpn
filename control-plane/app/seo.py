"""Crawler-facing surface: robots.txt, sitemap.xml, canonical/OpenGraph URLs.

Design and the verdicts behind it: docs/seo-ai-visibility.md. The two rules that are load-bearing
here and easy to undo by accident:

1. The sitemap path list is a HAND-WRITTEN constant. It must never be derived from `app.routes`,
   `_ROUTE_TABLE` or `_GUIDE_PAGES` — those carry `ru-vless` (which is required to 404 in prod,
   CLAUDE.md rule 2) and the secret `CP_ADMIN_PATH`. A generated sitemap would publish both.
2. Origins come from a whitelist, not from the Host header. Host is attacker-controlled; echoing it
   into `<link rel=canonical>` lets a third party declare their own URL canonical for our pages.
   An unknown Host degrades to the primary domain rather than being trusted.

On .onion we ALLOW crawling, which reverses one line of the SEO doc ("на `.onion` → `Disallow: /`").
That line was written from a clearnet-duplicate-content angle, and it is wrong for this project:
clearnet crawlers cannot resolve .onion at all, so there is no duplicate to suppress, while Ahmia —
the onion index we intend to submit to (docs/listing-plan-2026-07.md §5) — DOES respect robots.txt.
`Disallow: /` there would block the only crawler that can reach the address.
"""

from __future__ import annotations

from urllib.parse import quote

from app.config import get_settings

ONION_SUFFIX = ".onion"

# Public, indexable pages. Hand-written on purpose — see the module docstring.
# `/login`, `/dashboard`, `/devices/*`, `/invoice/*`, `/lang` are deliberately absent: they are
# either behind auth, capability-URLs, or redirects that a crawler would multiply into variants.
PUBLIC_PATHS: tuple[str, ...] = (
    "/",
    "/buy",
    "/redeem",
    "/guides",
    "/guides/windows",
    "/guides/macos",
    "/guides/ios",
    "/guides/android",
    "/guides/linux",
    "/faq",
    "/support",
    "/transparency",
    "/terms",
    "/aup",
    "/privacy",
)

# Paths kept out of the index. None of them is a secret — the admin path is deliberately NOT here:
# a `Disallow` line publishes the very prefix it pretends to protect, and robots.txt is the first
# file any scanner fetches (docs/seo-ai-visibility.md §5).
_DISALLOWED = ("/login", "/logout", "/dashboard", "/devices/", "/invoice/", "/lang", "/email")

# Crawlers that exist only to collect training corpora. Blocking these does NOT remove the site from
# AI answers — answering is done by separate tokens (OAI-SearchBot, Claude-User, PerplexityBot,
# DuckAssistBot …), which fall through to the `*` group below and stay allowed. Getting this
# backwards is a self-inflicted wound: `Claude-User` obeys robots.txt, so disallowing it would make
# the site invisible exactly when a Claude user asks about it.
# Amazon caches robots.txt for up to 30 days — ship this once, correctly, rather than iterating.
_TRAINING_ONLY = (
    "GPTBot",
    "ClaudeBot",
    "Google-Extended",
    "Applebot-Extended",
    "CCBot",
    "meta-externalagent",
    "Amazonbot",
    "Bytespider",
)


# Per-page `meta description`. Kept here rather than in each template so that adding one does not
# touch 14 files under web/templates — prod runs ahead of the repo on some of them (CLAUDE.md
# rule 2), and every extra template in a deploy is another hash to reconcile by hand.
# Written in the templates' source language (RU) and translated through the same en_by_ru.json
# dictionary as everything else; check_i18n.py enforces that.
#
# Deliberately free of numbers that live in the database — device limits, prices and the location
# list are data-driven, and a description that hardcodes "five devices, NL and RO" becomes a lie the
# day a plan or a node changes, in the one string search engines quote verbatim.
def _(text: str) -> str:
    """Translation MARKER, not a translation (the gettext `N_` idiom).

    These strings are collected at import time, long before a request exists, so the real
    per-request translator cannot run here — `_ctx` applies it later. The marker exists so that
    scripts/check_i18n.py sees an ordinary `_("…")` call: without it the strings are invisible to
    the checker in both directions — a missing EN translation would not be reported, and the
    dictionary entries would be flagged as orphans and "deleted" by a future cleanup.
    """
    return text


_DESCRIPTIONS: dict[str, str] = {
    "/": _("Анонимный VPN. No-KYC, no-logs. Оплата криптой. Вход по номеру аккаунта."),
    "/buy": _("Тарифы и оплата криптой: BTC, LTC, USDT. Без имени, email и KYC — доступ по номеру аккаунта."),
    "/redeem": _("Погашение подарочного кода privacyvpn VPN: код превращается в готовый доступ, номер аккаунта выдаётся сразу."),
    "/guides": _("Гайды по подключению WireGuard: Windows, macOS, iOS, Android, Linux. Написаны как man-страницы, командами."),
    "/guides/windows": _("Подключение к privacyvpn VPN на Windows: импорт туннеля WireGuard и настройка kill-switch."),
    "/guides/macos": _("Подключение к privacyvpn VPN на macOS: клиент WireGuard, импорт файла конфигурации или QR-кода."),
    "/guides/ios": _("Подключение к privacyvpn VPN на iOS и iPadOS: импорт конфигурации, QR-код и режим On-Demand."),
    "/guides/android": _("Подключение к privacyvpn VPN на Android: клиент WireGuard из Play или F-Droid, режим Always-on."),
    "/guides/linux": _("Подключение к privacyvpn VPN на Linux через wg-quick, плюс запасной способ сгенерировать ключи без браузера."),
    "/faq": _("Частые вопросы о privacyvpn VPN: что хранится и что нет, оплата криптой, устройства, локации, возвраты."),
    "/support": _("Поддержка privacyvpn VPN: бот в Telegram и канал с настоящими ссылками. Приватный ключ устройства не нужен никогда."),
    "/transparency": _("Прозрачность privacyvpn VPN: что именно хранится, почему мы не ведём warrant canary и чего у нас пока нет."),
    "/terms": _("Условия использования privacyvpn VPN: анонимный оператор, оплата криптой, KYC не запрашивается никогда."),
    "/aup": _("Политика допустимого использования privacyvpn VPN и порядок обработки abuse-жалоб."),
    "/privacy": _("Политика конфиденциальности privacyvpn VPN: какие данные собираются, как долго живут и что стирается после активации."),
}


def description_for(path: str) -> str:
    """Untranslated source string; the caller runs it through the request's translator."""
    return _DESCRIPTIONS.get(path.rstrip("/") or "/", _DESCRIPTIONS["/"])


def is_onion(host: str) -> bool:
    return host.lower().split(":")[0].endswith(ONION_SUFFIX)


def public_hosts() -> tuple[str, ...]:
    raw = get_settings().public_hosts or ""
    hosts = tuple(h.strip().lower() for h in raw.split(",") if h.strip())
    return hosts or ("privacyvpn.example.com",)


def origin_for(request) -> str:
    """Scheme+host to use in canonical/OpenGraph/sitemap URLs for this request.

    Whitelisted Host → that host (so .com, .si and .onion each stay self-referential and the mirror
    never points at the primary — a seized domain is usually turned into a confiscation banner, and
    a cross-canonical would hand the banner our identity). Anything else → the primary domain.
    """
    # `url.hostname`, not a hand-split of the Host header: it strips the port and unwraps IPv6
    # brackets for us. Same idiom the admin onion-gate already relies on (api/admin_ui.py).
    host = (request.url.hostname or "").lower()
    if host not in public_hosts():
        host = public_hosts()[0]
    scheme = "http" if is_onion(host) else "https"
    return f"{scheme}://{host}"


def canonical_url(request) -> str:
    """Self-canonical: same path on the whitelisted origin. Query strings are dropped on purpose —
    none of the public pages varies its content by query, and keeping them would split the page
    across as many canonicals as there are tracking parameters people paste."""
    path = request.url.path or "/"
    return origin_for(request) + quote(path, safe="/")


def robots_txt(origin: str) -> str:
    lines = [
        "# Answering/citing crawlers (OAI-SearchBot, ChatGPT-User, Claude-SearchBot, Claude-User,",
        "# PerplexityBot, Perplexity-User, Googlebot, bingbot, Applebot, DuckAssistBot, MistralAI-*)",
        "# are intentionally NOT listed: they fall through to the '*' group and stay allowed.",
        "# Only training-only crawlers are named, and only to refuse them.",
        "",
    ]
    for agent in _TRAINING_ONLY:
        lines += [f"User-agent: {agent}", "Disallow: /", ""]
    lines.append("User-agent: *")
    lines += [f"Disallow: {path}" for path in _DISALLOWED]
    lines += ["Allow: /", "", f"Sitemap: {origin}/sitemap.xml", ""]
    return "\n".join(lines)


def sitemap_xml(origin: str) -> str:
    """Per-host sitemap. The protocol requires the entries to share the sitemap's own host, so one
    static file could not serve .com, .si and .onion at once.

    No `lastmod`: /opt/privacyvpn is not a git checkout, so file mtime there is the time the deploy
    tarball was unpacked, not the time the page changed. A wrong lastmod is worse than none — it
    teaches crawlers to ignore the field.
    """
    urls = "".join(f"<url><loc>{origin}{quote(p, safe='/')}</loc></url>" for p in PUBLIC_PATHS)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )
