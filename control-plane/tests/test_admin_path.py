"""Secret admin path (CP_ADMIN_PATH): normalization + wiring into the router.

Rationale: hide /admin behind a high-entropy path (defense-in-depth against
brute-force/scanners). The default stays /admin (backward compatibility + tests).
The "custom path works, /admin returns 404" behavior is verified live (the router
prefix is resolved at import time, so within a single process only the default
changes)."""

import pytest

from app.config import Settings


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/admin", "/admin"),          # default
        ("/c-9f3a1b7e2d4c8", "/c-9f3a1b7e2d4c8"),
        ("/admin/", "/admin"),          # trailing slash is stripped
        ("admin", "/admin"),            # no leading slash → we add one
        ("console/secret", "/console/secret"),  # multi-segment secret
        ("  /c-abc  ", "/c-abc"),      # surrounding whitespace
        ("//x//", "/x"),                # collapse redundant slashes
        ("", "/admin"),                # empty → safe default
        ("/", "/admin"),                # slash only → do not mount at root
    ],
)
def test_admin_path_normalization(raw, expected):
    assert Settings(admin_path=raw).admin_path == expected


def test_admin_router_prefix_follows_settings():
    # the router and redirects must take the prefix from settings, not from a hardcoded "/admin"
    from app.api import admin_ui
    from app.config import get_settings

    assert admin_ui.ADMIN == get_settings().admin_path
    assert any(r.path.startswith(get_settings().admin_path) for r in admin_ui.router.routes)


def test_admin_templates_receive_admin_path():
    # _ctx passes admin_path into all templates (links/forms are built from the secret)
    from unittest.mock import MagicMock

    from app.api import admin_ui

    ctx = admin_ui._ctx(MagicMock())
    assert ctx["admin_path"] == admin_ui.ADMIN
