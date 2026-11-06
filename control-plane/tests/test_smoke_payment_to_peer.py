"""Smoke: full flow payment → activation → issuing a working peer config (without the private key)."""

from datetime import UTC, datetime

from app.models.enums import AccountStatus, PaymentMethod
from app.services.config_builder import render_conf
from app.services.provisioning import create_peer
from app.services.purchase import on_payment_settled, start_purchase
from tests._fakes import FakeProvider

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def test_payment_to_working_peer(session, price_plan, node):
    node.public_key = "SERVER_PUBKEY="
    session.commit()

    # 1) an anonymous user pays (Lightning — the instant path), an account-number is minted
    fake = FakeProvider(external_id="ext-smoke")
    res = start_purchase(
        session, price_plan=price_plan, method=PaymentMethod.LIGHTNING, provider=fake, now=NOW
    )
    session.commit()
    assert res.account_number is not None

    # 2) payment confirmed → activation (idempotent, with scrubbing of the link)
    on_payment_settled(session, res.invoice, now=NOW)
    session.commit()
    assert res.account.status == AccountStatus.ACTIVE

    # 3) browser-side pubkey → peer + config WITHOUT the private key
    peer, cfg = create_peer(session, res.account, node, public_key="CLIENT_PUBKEY", now=NOW)
    session.commit()
    assert peer.id is not None
    assert cfg.server_public_key == "SERVER_PUBKEY="
    conf = render_conf(cfg)
    assert "PrivateKey = <INSERT_PRIVATE_KEY_IN_BROWSER>" in conf
    assert "AllowedIPs = 0.0.0.0/0, ::/0" in conf
    assert peer.ip_lease.address.startswith("10.7.0.")
