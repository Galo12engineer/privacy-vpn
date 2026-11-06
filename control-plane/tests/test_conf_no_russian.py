"""The .conf the customer downloads must be in his language.

Regression: config_builder embedded a bare Russian comment into EVERY generated config — the
"the private key is generated in your browser" line — so an English customer opened his WireGuard
file and found Russian in it. It is not a cosmetic string either: it is the product's central privacy claim,
placed exactly where it matters (the file holding his key). It survived because
scripts/check_i18n.py only inspects tr('…') calls and error=/sub= kwargs; a plain literal in a list
matches neither rule.
"""

from __future__ import annotations

import re

from app.services.config_builder import PeerConfig, render_conf
from app.webui import translator

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _cfg() -> PeerConfig:
    return PeerConfig(
        address_v4="10.7.0.2/32",
        address_v6="fd00::2/128",
        dns="10.7.0.1",
        mtu=1420,
        server_public_key="c2VydmVycHVia2V5",
        endpoint="203.0.113.10:51820",
        endpoint_alt="203.0.113.10:443",
        allowed_ips=["0.0.0.0/0", "::/0"],
        persistent_keepalive=25,
        preshared_key=None,
        v6_null_routed=False,
    )


def test_english_config_has_no_cyrillic():
    conf = render_conf(_cfg(), tr=translator("en"))

    assert not CYRILLIC.search(conf), f"Russian leaked into an English customer's .conf:\n{conf}"
    assert "never reaches the server" in conf, "the privacy claim must survive translation"


def test_russian_config_keeps_russian():
    conf = render_conf(_cfg(), tr=translator("ru"))

    assert "генерируется в браузере" in conf


def test_config_is_still_a_valid_wireguard_file():
    """The comment must stay a comment — a stray line would break the client's import."""
    conf = render_conf(_cfg(), tr=translator("en"))

    for line in conf.splitlines():
        assert line == "" or line.startswith(("#", "[")) or "=" in line, f"bad line: {line!r}"
    assert conf.startswith("[Interface]")
    assert "PrivateKey = <INSERT_PRIVATE_KEY_IN_BROWSER>" in conf
