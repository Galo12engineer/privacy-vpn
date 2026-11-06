"""reconcile must rebuild the SAME callback URL that create_invoice registered.

Regression, and the second half of the "reconcile is a no-op" story. Wiring the provider into the
scheduler was not enough: the callback URL carries token = HMAC(secret, "order_id:expected"), and
`expected` was stringified with format(expected, "f"). create_invoice signs the freshly quoted
Decimal ("0.00021062"); reconcile signs invoice.crypto_amount read back from Numeric(30, 12), which
Postgres pads to "0.000210620000". Same number, different string, different token — so reconcile
asked BlockBee's /logs/ for a callback URL that never existed and always found nothing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.payments.blockbee import BlockBeeProvider


@pytest.fixture()
def provider():
    return BlockBeeProvider(
        api_key="k",
        callback_secret="s3cret",
        payout_addresses={"BTC": "bc1qexample"},
        callback_base_url="https://example.test",
        confirmations={"BTC": 2},
    )


ORDER = "11111111-2222-3333-4444-555555555555"


def test_db_padding_does_not_change_the_callback_url(provider):
    at_creation = Decimal("0.00021062")        # what create_invoice quotes
    after_db_roundtrip = Decimal("0.000210620000")  # the same value out of Numeric(30, 12)

    assert at_creation == after_db_roundtrip, "premise: numerically identical"
    assert provider._callback_url(order_id=ORDER, expected=at_creation) == provider._callback_url(
        order_id=ORDER, expected=after_db_roundtrip
    ), "reconcile would query a URL BlockBee never saw → lost payments stay lost"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("0.5", "0.500000000000"),
        ("100", "100.000000000000"),
        ("12.345", "12.34500000"),
        ("0.00000001", "0.000000010000"),
    ],
)
def test_amount_key_is_stable_under_trailing_zeros(provider, a, b):
    assert provider._amount_key(Decimal(a)) == provider._amount_key(Decimal(b))


@pytest.mark.parametrize(
    ("value", "expected_text"),
    [("100", "100"), ("0.00000001", "0.00000001"), ("0.000000000000", "0")],
)
def test_amount_key_never_emits_scientific_notation(provider, value, expected_text):
    """normalize() yields 1E+2 / 1E-8; the URL must carry plain digits or BlockBee cannot match."""
    key = provider._amount_key(Decimal(value))
    assert key == expected_text
    assert "E" not in key and "e" not in key


def test_token_still_covers_the_amount(provider):
    """The token must keep binding order_id AND amount — normalising must not weaken that."""
    u1 = provider._callback_url(order_id=ORDER, expected=Decimal("0.00021062"))
    u2 = provider._callback_url(order_id=ORDER, expected=Decimal("0.00021063"))
    assert u1 != u2, "a different amount must produce a different token"


def test_webhook_accepts_the_token_from_the_url_we_registered(provider):
    """End to end: the string we sign at creation is the one BlockBee echoes back and we verify.

    The GET callback route synthesises a JSON body from the query params (see api/payments.py),
    so that is what verify_webhook receives.
    """
    import json
    from urllib.parse import parse_qs, urlparse

    url = provider._callback_url(order_id=ORDER, expected=Decimal("0.000210620000"))
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}

    assert provider.verify_webhook(headers={}, body=json.dumps(q).encode()) is True


def test_webhook_rejects_a_tampered_amount(provider):
    """Normalising the key must not let someone rewrite `expected` to 0 and settle for free."""
    import json
    from urllib.parse import parse_qs, urlparse

    url = provider._callback_url(order_id=ORDER, expected=Decimal("0.00021062"))
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    q["expected"] = "0"

    assert provider.verify_webhook(headers={}, body=json.dumps(q).encode()) is False
