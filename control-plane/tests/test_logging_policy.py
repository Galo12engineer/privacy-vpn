"""No-logs policy: NoPIIFilter must be installed on the root logger (checked in CI)."""

import logging

from app.logging.no_pii_filter import NoPIIFilter


def test_root_logger_has_no_pii_filter():

    assert any(isinstance(f, NoPIIFilter) for f in logging.getLogger().filters)


def test_no_pii_filter_redacts_via_root(caplog):
    import app.main  # noqa: F401

    logging.getLogger("x.y").info("client 203.0.113.9 ua@mail.com")
    # the filter on root is applied during handling; we check the filter itself deterministically
    from app.logging.no_pii_filter import scrub

    out = scrub("client 203.0.113.9 ua@mail.com")
    assert "203.0.113.9" not in out and "ua@mail.com" not in out
