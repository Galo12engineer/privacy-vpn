"""Percent-off promo codes on the money path.

The invariants that cost real money if they break:
  1. the discounted amount reaches BOTH the Invoice row and the provider quote (one number);
  2. the min-USD coin gate is judged on the DISCOUNTED amount and BEFORE an invoice exists —
     otherwise a $13.50 plan at −15% = $11.48 slips under the $12 USDT threshold and BlockBee
     never forwards the customer's coins;
  3. max_uses holds under concurrency;
  4. a purchase that fails after the reservation gives the use back.

Every test here was checked by reverting its fix and confirming it goes red — the notes on the
race tests say what to break.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Account, Invoice, PricePlan, PromoCode
from app.models.enums import PaymentMethod
from app.payments import registry
from app.services import promo
from app.services.billing_fsm import PaymentEvent
from app.services.purchase import start_purchase
from app.webui import buy_limiter, preview_limiter, promo_limiter
from tests._fakes import FakeProvider


@pytest.fixture()
def client(session):
    get_settings().cookie_secure = False
    get_settings().purchases_open = True
    promo_limiter.reset("promo")

    def _ov():
        yield session

    # All three limiters are module-global and survive between tests. Reset them here so this file
    # cannot poison another (or be poisoned): the ordering dependency would be invisible locally and
    # would surface as a flake in CI.
    buy_limiter.reset("buy")
    preview_limiter.reset("preview")
    app.dependency_overrides[get_session] = _ov
    yield TestClient(app)
    buy_limiter.reset("buy")
    preview_limiter.reset("preview")
    promo_limiter.reset("promo")
    app.dependency_overrides.clear()
    registry.clear()


@pytest.fixture()
def ladder(session):
    """Prod's real ladder — the $13.50 plan is the one that straddles the USDT threshold."""
    session.add_all([
        PricePlan(code="1m", name="1 месяц", duration_days=30, price_fiat_cents=500,
                  fiat_currency="USD", device_limit=5, is_active=True, sort_order=1),
        PricePlan(code="3m", name="3 месяца", duration_days=90, price_fiat_cents=1350,
                  fiat_currency="USD", device_limit=5, is_active=True, sort_order=2),
        PricePlan(code="12m", name="12 месяцев", duration_days=365, price_fiat_cents=4800,
                  fiat_currency="USD", device_limit=5, is_active=True, sort_order=4),
    ])
    session.commit()
    return session.query(PricePlan).order_by(PricePlan.sort_order).all()


def _code(session, code="LAUNCH20", percent=20, **kw):
    kw.setdefault("is_active", True)
    row = PromoCode(code=code, percent_off=percent, **kw)
    session.add(row)
    session.commit()
    return row


def _buy(client, **extra):
    data = {"plan_code": "3m", "method": "litecoin", "account_number": ""}
    data.update(extra)
    return client.post("/buy", data=data)


# --- arithmetic ------------------------------------------------------------------------------
def test_rounding_is_half_up_on_the_final_amount():
    # the brief's own worked example
    assert promo.discounted_cents(1350, 15) == 1148  # $13.50 -> $11.48
    assert promo.discounted_cents(500, 20) == 400
    assert promo.discounted_cents(4800, 33) == 3216
    # exactly .5 of a cent rounds up, not to even and not down
    assert promo.discounted_cents(1000, 15) == 850
    assert promo.discounted_cents(101, 50) == 51  # 50.5 -> 51


def test_zero_and_negative_percent_are_a_no_op():
    assert promo.discounted_cents(1350, 0) == 1350
    assert promo.discounted_cents(1350, -5) == 1350


def test_discount_never_produces_a_free_invoice():
    """A zero-amount invoice quotes as 0 coins and then reads as fully paid on any callback."""
    assert promo.discounted_cents(1, promo.MAX_PERCENT) >= 1
    # even a percent that slipped past validation cannot zero the price
    assert promo.discounted_cents(100, 100) >= 1


def test_normalize_collapses_the_ways_a_customer_retypes_a_code():
    assert promo.normalize(" launch-20 ") == "LAUNCH20"
    assert promo.normalize("LAUNCH 20") == "LAUNCH20"
    assert promo.normalize("launch20") == "LAUNCH20"
    assert promo.normalize("") == ""


# --- the two places the amount must reach ------------------------------------------------------
def test_discount_reaches_both_the_invoice_row_and_the_provider(session, ladder):
    """start_purchase reads the plan price twice. Both reads must be the discounted number.

    Break check: pass `amount_fiat_cents=price_plan.price_fiat_cents` to InvoiceRequest again and
    this goes red on the provider assertion while the row still looks right — which is exactly the
    shape of the bug (invoice says $11.48, the customer is asked for $13.50 worth of coins).
    """
    provider = FakeProvider(name="blockbee", event=PaymentEvent.SEEN)
    plan = next(p for p in ladder if p.code == "3m")
    res = start_purchase(
        session, price_plan=plan, method=PaymentMethod.LITECOIN,
        provider=provider, discount_percent=15,
    )
    assert res.invoice.amount_fiat_cents == 1148
    assert res.invoice.discount_percent == 15
    assert provider.created[0].amount_fiat_cents == 1148, "provider quoted the UNDISCOUNTED price"
    # and they are the same integer, not two independently-rounded ones
    assert provider.created[0].amount_fiat_cents == res.invoice.amount_fiat_cents


def test_purchase_without_a_code_is_unchanged(session, ladder):
    provider = FakeProvider(name="blockbee", event=PaymentEvent.SEEN)
    plan = next(p for p in ladder if p.code == "3m")
    res = start_purchase(
        session, price_plan=plan, method=PaymentMethod.LITECOIN, provider=provider
    )
    assert res.invoice.amount_fiat_cents == 1350
    assert res.invoice.discount_percent == 0
    assert provider.created[0].amount_fiat_cents == 1350


# --- the min-USD gate ---------------------------------------------------------------------------
def test_discount_under_the_coin_threshold_is_refused_before_any_invoice_exists(
    client, session, ladder
):
    """$13.50 −15% = $11.48 < the $12 USDT threshold. BlockBee would not forward it and the coins
    would be stuck at the deposit address, unrecoverable.

    Break check: gate on `plan.price_fiat_cents` instead of the discounted amount → the POST
    succeeds and an unpayable invoice is created.
    """
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "SAVE15X", 15)

    resp = _buy(client, method="usdt_trc20", promo_code="SAVE15X")

    assert resp.status_code == 400
    assert session.query(Invoice).count() == 0, "an invoice was minted below the coin threshold"
    assert session.query(Account).count() == 0
    # the reservation is given back too — a blocked coin choice must not cost a redemption
    assert session.query(PromoCode).one().used_count == 0
    assert "$12" in resp.text and "$11.48" in resp.text, "the error must name both numbers"


def test_the_same_pair_without_the_code_still_goes_through(client, session, ladder):
    """The control: $13.50 clears $12 at list price, so the gate above is about the discount."""
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    resp = _buy(client, method="usdt_trc20")
    assert resp.status_code == 200
    assert session.query(Invoice).one().amount_fiat_cents == 1350


def test_plan_cards_drop_coins_the_discount_pushed_out_of_range(client, session, ladder):
    """The card lists payable coins; with a code applied that list must shrink accordingly."""
    _code(session, "SAVE15X", 15)
    resp = client.post(
        "/buy",
        data={"plan_code": "3m", "method": "litecoin", "promo_code": "SAVE15X", "action": "apply"},
    )
    assert resp.status_code == 200
    # $11.48 can no longer pay with USDT ($12) but still clears BTC ($8) and LTC ($1)
    assert "11.48" in resp.text
    assert "13.50" in resp.text, "the real list price must remain visible next to the new one"


# --- preview (no JS) ----------------------------------------------------------------------------
def test_apply_shows_the_recalculated_price_and_creates_nothing(client, session, ladder):
    _code(session, "LAUNCH20", 20)
    resp = client.post(
        "/buy",
        data={"plan_code": "3m", "method": "litecoin", "promo_code": "launch-20", "action": "apply"},
    )
    assert resp.status_code == 200
    assert session.query(Invoice).count() == 0
    assert session.query(PromoCode).one().used_count == 0, "the preview burned a use"
    assert "10.80" in resp.text          # 1350 -20% = 1080
    assert 'value="LAUNCH20"' in resp.text, "the normalized code must be echoed back into the form"


def test_apply_is_repeatable(client, session, ladder):
    """A customer will press Apply, change the plan and press it again."""
    _code(session, "LAUNCH20", 20, max_uses=1)
    for _ in range(3):
        resp = client.post(
            "/buy",
            data={"plan_code": "3m", "method": "litecoin", "promo_code": "LAUNCH20",
                  "action": "apply"},
        )
        assert resp.status_code == 200
    assert session.query(PromoCode).one().used_count == 0


def test_a_typed_renewal_number_survives_a_re_render(client, session, ladder):
    """Apply is a re-render, and so is every error path. Wiping the field there means a
    renewing customer retypes a 20-digit credential out of their password manager — and ENTER
    inside the promo input lands on Apply, so it is not a rare corner."""
    _code(session, "LAUNCH20", 20)
    number = "1234 5678 9012 3456 7890"
    resp = client.post(
        "/buy",
        data={"plan_code": "3m", "method": "litecoin", "promo_code": "LAUNCH20",
              "account_number": number, "action": "apply"},
    )
    assert resp.status_code == 200
    assert f'value="{number}"' in resp.text, "the typed account number was wiped"


def test_buy_still_works_when_the_form_posts_no_action_field(client, session, ladder):
    """The action field defaults to «create», so anything written against the old contract buys."""
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    resp = client.post("/buy", data={"plan_code": "3m", "method": "litecoin"})
    assert resp.status_code == 200
    assert session.query(Invoice).count() == 1


# --- the resubmit guard must not eat a discount --------------------------------------------------
def test_applying_a_code_after_a_full_price_invoice_does_not_silently_charge_the_full_price(
    client, session, ladder
):
    """The overcharge the guard used to cause, verbatim.

    Buy at list price → obtain a code → Apply (the page quotes $10.80) → Create invoice.
    The guard matched on "this account has a live invoice" and nothing else, so it 303'd the
    customer to the OLD $13.50 invoice: no error, no banner, no promo badge, and buy.html had just
    promised in as many words that the discount reaches the invoice. Crypto is non-reversible, so
    sending coins to that invoice is final.

    Break check: drop the `same` comparison and always redirect → the customer lands on a 1350-cent
    invoice after being quoted 1080.
    """
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "LAUNCH20", 20)

    assert _buy(client).status_code == 200          # invoice #1, list price, sets the cookie
    first = session.query(Invoice).one()
    assert (first.amount_fiat_cents, first.discount_percent) == (1350, 0)

    preview = client.post("/buy", data={"plan_code": "3m", "method": "litecoin",
                                        "promo_code": "LAUNCH20", "action": "apply"})
    assert "10.80" in preview.text, "the customer was quoted the discounted price"

    resp = client.post("/buy", data={"plan_code": "3m", "method": "litecoin",
                                     "promo_code": "LAUNCH20"}, follow_redirects=False)

    assert resp.status_code != 303, "silently redirected to the undiscounted invoice"
    assert resp.status_code == 409
    # says what they would actually be paying, and links the invoice they already have
    assert "13.50" in resp.text
    assert f"/invoice/{first.id}" in resp.text
    # nothing was minted behind their back, and no use was spent
    assert session.query(Invoice).count() == 1
    assert session.query(PromoCode).one().used_count == 0


def test_two_plans_of_equal_length_but_different_price_are_not_confused(client, session, ladder):
    """A resubmit is the SAME plan; the guard compares the plan key the invoice already carries.

    Two active plans sharing duration_days ("90 days cheaper, 2 devices" next to "90 days, 5
    devices" — the normal shape of a promo tier) looked identical to an earlier guard that
    reconstructed plan identity from (price, duration). The customer who picked the cheap one was
    redirected to the expensive invoice.

    Break check: drop `pending.price_plan_id == plan.id` from `same` → 303 to the $13.50 invoice
    after the customer selected the $10 plan (the price DOES differ here, so the amount comparison
    also catches this one — see the device_limit test for the case only price_plan_id catches).
    """
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    session.add(PricePlan(code="3m-lite", name="3 месяца lite", duration_days=90,
                          price_fiat_cents=1000, fiat_currency="USD", device_limit=2,
                          is_active=True, sort_order=3))
    session.commit()

    assert _buy(client, plan_code="3m").status_code == 200      # $13.50 invoice, cookie set
    assert session.query(Invoice).one().amount_fiat_cents == 1350

    resp = client.post("/buy", data={"plan_code": "3m-lite", "method": "litecoin",
                                     "account_number": ""}, follow_redirects=False)

    assert resp.status_code != 303, "sent to the $13.50 invoice after selecting the $10.00 plan"
    assert resp.status_code == 409
    assert session.query(Invoice).count() == 1


def test_two_plans_of_equal_price_but_different_device_limit_are_not_confused(client, session, ladder):
    """The dimension only price_plan_id catches: same price, same duration, MORE devices.

    Reproduced against the price-only guard: the customer selects the 10-device tier at $13.50, is
    redirected to the 5-device invoice at the same $13.50 — and settlement, which reads device_limit
    back through invoice.price_plan_id, grants 5. The amount matches, so nothing downstream notices;
    the invoice page shows neither plan name nor device count, so the customer cannot see the swap.
    It is entitlement quietly stolen, with no order record to dispute, paid in non-reversible crypto.

    Break check: drop `pending.price_plan_id == plan.id` from `same` → 303 to the 5-device invoice
    and, after settle, account.device_limit == 5 for a customer who bought 10.
    """
    from app.models.enums import AccountStatus
    from app.services.purchase import on_payment_settled

    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    session.add(PricePlan(code="3m-pro", name="3 месяца pro", duration_days=90,
                          price_fiat_cents=1350, fiat_currency="USD", device_limit=10,
                          is_active=True, sort_order=3))
    session.commit()

    assert _buy(client, plan_code="3m").status_code == 200          # 5-device invoice
    resp = client.post("/buy", data={"plan_code": "3m-pro", "method": "litecoin",
                                     "account_number": ""}, follow_redirects=False)
    assert resp.status_code == 409, "redirected to the 5-device invoice after selecting 10 devices"
    assert session.query(Invoice).count() == 1

    # and had it NOT been blocked, the 5-device grant would have been the silent theft — assert the
    # positive path grants what was chosen: buy the pro plan cleanly on a fresh session.
    client.cookies.clear()
    r = client.post("/buy", data={"plan_code": "3m-pro", "method": "litecoin", "account_number": ""})
    assert r.status_code == 200
    inv = session.query(Invoice).order_by(Invoice.created_at.desc()).first()
    account_id = inv.account_id
    on_payment_settled(session, inv)
    session.commit()
    got = session.get(Account, account_id)
    assert got.device_limit == 10, "the 10-device plan must grant 10 devices"
    assert got.status == AccountStatus.ACTIVE


def test_a_mid_window_duration_edit_on_the_same_plan_is_not_treated_as_a_resubmit(
    client, session, ladder
):
    """The term dimension: same plan, same price, but the operator changed duration_days.

    device_limit is read LIVE at settlement (so price_plan_id already protects it), but days_granted
    is FROZEN on the invoice at creation — so a resubmit that redirected would settle the stale
    term. Customer sees "180 days for $13.50" on the resubmit page and would be credited the frozen
    90. amount and price_plan_id both still match a pure duration edit; only days_granted moves.

    The edit goes through admin_service.update_plan, whose `allowed` set includes duration_days.
    The admin ROUTE does not expose that field today, so this is currently latent — but the guard
    must not depend on a route's field list for correctness. Comparing days_granted closes it.

    Break check: drop `pending.days_granted == plan.duration_days` from `same` → 303 to the stale
    90-day invoice after the plan became 180 days.
    """
    from app.services import admin_service

    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))

    assert _buy(client, plan_code="3m").status_code == 200
    inv = session.query(Invoice).one()
    assert inv.days_granted == 90 and inv.amount_fiat_cents == 1350

    plan = session.execute(select(PricePlan).where(PricePlan.code == "3m")).scalar_one()
    admin_service.update_plan(session, plan, duration_days=180)   # price unchanged
    session.commit()

    resp = client.post("/buy", data={"plan_code": "3m", "method": "litecoin",
                                     "account_number": ""}, follow_redirects=False)

    assert resp.status_code == 409, "held to the stale 90-day invoice after the plan became 180 days"
    assert session.query(Invoice).count() == 1


def test_a_mid_window_price_drop_on_the_same_plan_is_not_treated_as_a_resubmit(
    client, session, ladder
):
    """price_plan_id matches but the amount does not — the case the amount clause exists for.

    The customer creates a $13.50 invoice; the operator then lowers that same plan to $10.00 while
    the invoice is still live; the customer resubmits. Same plan, same coin, same (zero) percent, so
    price_plan_id + method + discount_percent all match — but redirecting him to the stale $13.50
    invoice would silently hold him above the price the plan now advertises. The amount comparison
    catches it and returns 409.

    Break check: drop `pending.amount_fiat_cents == payable_cents` from `same` → 303 to the $13.50
    invoice after the plan dropped to $10.00.
    """
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))

    assert _buy(client, plan_code="3m").status_code == 200
    assert session.query(Invoice).one().amount_fiat_cents == 1350

    plan = session.execute(
        select(PricePlan).where(PricePlan.code == "3m")
    ).scalar_one()
    plan.price_fiat_cents = 1000        # operator edits the SAME plan's price mid-window
    session.commit()

    resp = client.post("/buy", data={"plan_code": "3m", "method": "litecoin",
                                     "account_number": ""}, follow_redirects=False)

    assert resp.status_code == 409, "held to the stale $13.50 invoice after the plan dropped to $10"
    assert session.query(Invoice).count() == 1


def test_a_true_resubmit_still_returns_the_same_invoice(client, session, ladder):
    """F5 on the result page: identical plan, method and percent → still one invoice, not two.
    This is the bug the guard was written for (a second ACCOUNT, with the cookie swapped to it)."""
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "LAUNCH20", 20)

    assert _buy(client, promo_code="LAUNCH20").status_code == 200
    inv = session.query(Invoice).one()

    again = _buy(client, promo_code="LAUNCH20")
    assert again.status_code == 200  # followed the redirect to the same invoice
    assert session.query(Invoice).count() == 1
    assert session.query(Account).count() == 1
    assert session.query(PromoCode).one().used_count == 1, "the resubmit spent a second use"
    assert session.query(Invoice).one().id == inv.id


def test_renewal_with_a_code_extends_the_existing_account_at_the_discounted_price(
    client, session, ladder
):
    """The less-travelled path: an account number is supplied, so no account is minted.

    Worth its own test because the promo work threads through start_purchase's OTHER branch, and
    because the 409 added to the resubmit guard must never be reachable here.
    """
    from app.models.enums import AccountStatus
    from app.services.account_number import generate_account_number, hash_account_number
    from app.services.purchase import on_payment_settled

    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "LAUNCH20", 20, max_uses=5)
    number = generate_account_number()
    acct = Account(
        account_number_hash=hash_account_number(number, get_settings().account_number_pepper),
        status=AccountStatus.EXPIRED, device_limit=5,
    )
    session.add(acct)
    session.commit()

    resp = _buy(client, account_number=number, promo_code="LAUNCH20")

    assert resp.status_code == 200
    inv = session.query(Invoice).one()
    assert (inv.amount_fiat_cents, inv.discount_percent) == (1080, 20)
    assert inv.account_id == acct.id, "renewal must extend the existing account"
    assert session.query(Account).count() == 1, "renewal minted a second account"

    on_payment_settled(session, inv)
    session.commit()
    session.refresh(acct)
    assert acct.status == AccountStatus.ACTIVE
    assert session.query(PromoCode).one().used_count == 1, "a paid renewal must keep its use"


def test_a_renewer_is_never_blocked_by_the_pending_invoice_409(client, session, ladder):
    """The resubmit guard only runs when account_number is EMPTY — a renewer supplies one, so the
    409 added for the discount mismatch must not reach them."""
    from app.models.enums import AccountStatus
    from app.services.account_number import generate_account_number, hash_account_number

    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "LAUNCH20", 20)
    number = generate_account_number()
    session.add(Account(
        account_number_hash=hash_account_number(number, get_settings().account_number_pepper),
        status=AccountStatus.EXPIRED, device_limit=5,
    ))
    session.commit()

    for _ in range(2):
        resp = _buy(client, account_number=number, promo_code="LAUNCH20")
        assert resp.status_code == 200, f"renewer blocked with {resp.status_code}"


# --- keyboard / no-JS ----------------------------------------------------------------------------
def test_enter_with_an_empty_promo_field_buys_instead_of_dead_ending(client, session, ladder):
    """Apply is the form's default button, so with JS off ENTER anywhere submits action=apply.

    With no code entered that re-rendered a byte-identical page — a keyboard user on Tor pressed
    ENTER, waited out a circuit round trip, saw the same form and concluded checkout was broken.
    Nothing is created, so the abandonment is invisible in every metric we have.
    """
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    resp = client.post("/buy", data={"plan_code": "3m", "method": "litecoin",
                                     "promo_code": "", "action": "apply"})
    assert resp.status_code == 200
    assert session.query(Invoice).count() == 1, "ENTER on an empty promo field bought nothing"


def test_applying_a_code_never_leaves_an_unpayable_coin_selected(client, session, ladder):
    """Apply a discount that pushes the price under the selected coin's threshold: the form must
    come back with a coin that CAN pay, not with a guaranteed 400 on the next click.

    _render_buy's `sel_method or default` can never reach its discount-aware fallback on a POST —
    `method` is a required field, so it is always truthy — which made the discount-aware argument
    added to _default_method_for dead code on exactly the path it was written for.
    """
    _code(session, "SAVE20X", 20)
    resp = client.post("/buy", data={"plan_code": "3m", "method": "usdt_trc20",
                                     "promo_code": "SAVE20X", "action": "apply"})
    assert resp.status_code == 200
    checked = [ln for ln in resp.text.splitlines() if 'name="method"' in ln and "checked" in ln]
    assert checked, "no method preselected"
    assert not any('value="usdt_trc20"' in ln for ln in checked), (
        "$10.80 cannot pay with USDT ($12) — the next click is a guaranteed 400"
    )


def test_the_preview_path_is_rate_limited(client, session, ladder):
    """It returns before buy_limiter and creates nothing, so neither other bucket covers it: a DB
    lookup plus a full Jinja render, unauthenticated, over .onion where there is no IP to key on."""
    preview_limiter.reset("preview")
    _code(session, "GOODCODE", 10)
    seen_429 = False
    for _ in range(200):
        r = client.post("/buy", data={"plan_code": "3m", "method": "litecoin",
                                      "promo_code": "GOODCODE", "action": "apply"})
        if r.status_code == 429:
            seen_429 = True
            break
    preview_limiter.reset("preview")
    assert seen_429, "the preview path renders without limit"


# --- rejection reasons ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "setup,fragment",
    [
        (lambda s: None, "not found"),
        (lambda s: _code(s, "GONECODE", 20, is_active=False), "not found"),
        (
            lambda s: _code(s, "GONECODE", 20,
                            expires_at=datetime.now(UTC) - timedelta(days=1)),
            "expired",
        ),
        (lambda s: _code(s, "GONECODE", 20, max_uses=1), "usage limit"),
    ],
    ids=["unknown", "disabled", "expired", "exhausted"],
)
def test_a_bad_code_blocks_the_purchase_and_says_why(client, session, ladder, setup, fragment):
    """A rejected code must NOT quietly fall through to a full-price invoice: the customer is
    trying not to pay that price, and charging it anyway is the money-losing failure here."""
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    row = setup(session)
    if row is not None and row.max_uses:
        row.used_count = row.max_uses
        session.commit()

    resp = _buy(client, promo_code="GONECODE")

    assert resp.status_code == 400
    assert session.query(Invoice).count() == 0, "a full-price invoice was minted anyway"
    assert fragment in resp.text


def test_a_disabled_code_is_indistinguishable_from_an_unknown_one(client, session, ladder):
    """Otherwise the page is a free oracle for whoever is walking a code list."""
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "SWITCHEDOFF", 20, is_active=False)
    off = _buy(client, promo_code="SWITCHEDOFF").text
    unknown = _buy(client, promo_code="NEVEREXISTED").text
    assert "not found" in off and "not found" in unknown


def test_failed_attempts_are_rate_limited(client, session, ladder):
    """buy_limiter does not cover this: a preview creates no invoice, so guessing never touches it."""
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    seen_throttle = False
    for i in range(40):
        resp = client.post(
            "/buy",
            data={"plan_code": "3m", "method": "litecoin", "promo_code": f"GUESS{i:04d}",
                  "action": "apply"},
        )
        if "Too many promo-code attempts" in resp.text:
            seen_throttle = True
            break
    assert seen_throttle, "unlimited promo guesses"


def test_a_valid_code_does_not_spend_the_guess_budget(client, session, ladder):
    _code(session, "GOODCODE", 10)
    for _ in range(40):
        resp = client.post(
            "/buy",
            data={"plan_code": "3m", "method": "litecoin", "promo_code": "GOODCODE", "action": "apply"},
        )
        assert resp.status_code == 200
        assert "Too many" not in resp.text


# --- capacity + races -----------------------------------------------------------------------------
def test_the_invoice_page_shows_the_discounted_fiat_amount(client, session, ladder):
    """The customer's only proof the discount actually landed.

    The crypto figure alone tells them nothing — they cannot check 0.001 LTC against a percentage.
    So the invoice prints the fiat amount it was quoted from, plus the promo badge.
    """
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "LAUNCH20", 20)

    resp = _buy(client, promo_code="LAUNCH20")

    assert resp.status_code == 200
    assert "10.80" in resp.text, "the discounted fiat amount is not on the invoice page"
    assert "20" in resp.text
    assert session.query(Invoice).one().amount_fiat_cents == 1080


def test_the_use_is_counted_when_the_invoice_is_created(client, session, ladder):
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "LAUNCH20", 20, max_uses=2)
    assert _buy(client, promo_code="LAUNCH20").status_code == 200
    assert session.query(PromoCode).one().used_count == 1
    assert session.query(Invoice).one().amount_fiat_cents == 1080


def test_max_uses_holds_against_a_concurrent_redemption(session, ladder):
    """The race the brief names: two simultaneous POST /buy must not both take the last use.

    The concurrent writer is simulated with a bare UPDATE and synchronize_session=False, so the
    identity map is NOT told — which is what a second container's commit looks like from here.
    Mutating through the same ORM session instead would sync the identity map and the test would
    pass on the broken code.

    Break check: drop `.execution_options(populate_existing=True)` in promo.reserve and this goes
    red — SQLAlchemy hands back the stale in-memory used_count=0 and the guard waves it through.
    """
    row = _code(session, "LASTONECODE", 20, max_uses=1)

    # the other request got there first and committed
    session.execute(
        update(PromoCode)
        .where(PromoCode.id == row.id)
        .values(used_count=1)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    assert row.used_count == 0, "precondition: our in-memory copy is stale"

    with pytest.raises(promo.PromoError) as e:
        promo.reserve(session, row)
    assert e.value.reason == "exhausted"


def test_reserve_rechecks_the_switch_not_just_the_counter(session, ladder):
    """The operator can switch a code off between the customer's preview and their submit."""
    row = _code(session, "PULLEDCODE", 20)
    session.execute(
        update(PromoCode)
        .where(PromoCode.id == row.id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    session.commit()

    with pytest.raises(promo.PromoError):
        promo.reserve(session, row)


def test_a_failed_purchase_gives_the_use_back(client, session, ladder):
    """A BlockBee failure after the reservation must not eat a redemption.

    Break check: remove the `session.rollback()` from the provider-failure branch in POST /buy →
    used_count stays at 1 with no invoice to show for it.
    """
    class Broken(FakeProvider):
        def create_invoice(self, req):
            raise httpx.ConnectError("blockbee down")

    registry.register(Broken(name="blockbee"))
    _code(session, "LAUNCH20", 20, max_uses=1)

    resp = _buy(client, promo_code="LAUNCH20")

    assert resp.status_code == 400
    assert session.query(Invoice).count() == 0
    assert session.query(PromoCode).one().used_count == 0, "the failed purchase burned a use"


def test_an_unknown_account_number_gives_the_use_back(client, session, ladder):
    """Renewal with a typo'd account number: one typo must not cost one redemption."""
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "LAUNCH20", 20, max_uses=1)

    resp = _buy(client, promo_code="LAUNCH20", account_number="1234 5678 9012 3456 7890")

    assert resp.status_code == 404
    assert session.query(PromoCode).one().used_count == 0


# --- returning a use the customer never paid for -------------------------------------------------
def test_a_griefer_cannot_keep_a_code_dead(client, session, ladder):
    """The end-to-end griefing scenario, on invoices shaped exactly like production's.

    Reserving at creation makes max_uses free to destroy unless expiry returns the use: ten unpaid
    POSTs (cookies cleared, so the resubmit guard never fires) zero a max_uses=10 code and every
    real customer then sees "limit reached". The attacker gains nothing, which is what makes it
    attractive — one HTTP request burns one of the operator's discounts.

    THIS TEST TOUCHES NOTHING BUT expires_at. An earlier version also set payment_address=None to
    make the invoice land in EXPIRED, and that one line is what made it worthless: BlockBee is
    address-per-order, so every real invoice HAS an address and expiry routes it to LATE_PAYMENT
    instead. The release keyed on EXPIRED, so it never fired on a single production invoice while
    the test reported the defence working. Mutation testing did not catch it either — the mutant
    was caught, because the test really did exercise the code; the code just could not be reached
    from reality. Hence: drive it through the real POST /buy and the real expiry job, and change
    only the clock.

    Break check: revert the discriminator in invoice_expiry to `status == InvoiceStatus.EXPIRED`
    → used_count stays at 10.
    """
    from app.scheduler.invoice_expiry import expire_stale_invoices

    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "LAUNCH20", 20, max_uses=10)

    for _ in range(10):
        client.cookies.clear()  # no cookie → the resubmit guard cannot fire
        assert _buy(client, promo_code="LAUNCH20").status_code == 200
    assert session.query(PromoCode).one().used_count == 10, "precondition: the code is burned out"
    assert session.query(Invoice).filter(Invoice.payment_address.is_not(None)).count() == 10, (
        "precondition: these are production-shaped invoices, every one with a deposit address"
    )

    # the attacker walks away; only the clock moves
    session.query(Invoice).update({Invoice.expires_at: datetime.now(UTC) - timedelta(minutes=1)})
    session.commit()
    expire_stale_invoices(session)
    session.commit()

    assert session.query(PromoCode).one().used_count == 0, "the burned uses were never returned"
    assert session.query(Invoice).filter(Invoice.promo_code_id.is_not(None)).count() == 0
    # and a real customer can use the code again
    resp = client.post("/buy", data={"plan_code": "3m", "method": "litecoin",
                                     "promo_code": "LAUNCH20", "action": "apply"})
    assert resp.status_code == 200 and "10.80" in resp.text


def test_releasing_is_idempotent(client, session, ladder):
    """Expiry re-running, or a scrub racing it, must not credit the same use back twice — a counter
    drifting below reality lets the NEXT buyer past a limit that is genuinely spent.

    Two reservations, one released three times: the counter must land on 1, not 0. Starting from a
    single reservation cannot test this at all — `release` floors at max(0, ...), so a
    non-idempotent implementation produces the same 0 and the test passes on broken code. (It did.
    That is the exact failure mode this repo has hit before, so: the harness re-checks it.)
    """
    row = _code(session, "LAUNCH20", 20, max_uses=5)
    inv_a = Invoice(method=PaymentMethod.LITECOIN, days_granted=90, amount_fiat_cents=1080,
                    fiat_currency="USD", discount_percent=20, promo_code_id=row.id)
    session.add(inv_a)
    promo.reserve(session, row)   # invoice A
    promo.reserve(session, row)   # invoice B, which nobody releases
    session.commit()
    assert row.used_count == 2

    for _ in range(3):
        promo.release(session, inv_a)
        session.commit()

    assert session.query(PromoCode).one().used_count == 1, (
        "releasing one invoice repeatedly gave back the OTHER invoice's use too"
    )


def test_a_paid_invoice_keeps_its_use(client, session, ladder):
    """The customer got the discount — the allowance really was spent. Releasing here would let one
    payment redeem the code twice."""
    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "LAUNCH20", 20, max_uses=5)
    _buy(client, promo_code="LAUNCH20")
    inv = session.query(Invoice).one()

    from app.services.purchase import on_payment_settled

    on_payment_settled(session, inv)
    session.commit()

    assert session.query(PromoCode).one().used_count == 1
    # the linkage is scrubbed like account_id, but the counter is untouched
    assert inv.promo_code_id is None
    assert inv.account_id is None


def test_an_invoice_with_coins_seen_keeps_its_use(client, session, ladder):
    """Money actually arrived — the customer is redeeming it, reconcile just has not settled yet.

    This is what separates a real late payer from an abandoned cart, and it is the ONLY thing that
    separates them: both sit in late_payment with a deposit address (see the griefer test for why
    the status cannot be the discriminator). Returning the use here would let one payment redeem
    the code twice.

    Break check: drop the `not received` condition in invoice_expiry → used_count falls to 0.
    """
    from app.scheduler.invoice_expiry import expire_stale_invoices

    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "LAUNCH20", 20, max_uses=5)
    _buy(client, promo_code="LAUNCH20")
    inv = session.query(Invoice).one()
    inv.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    inv.crypto_amount_paid = Decimal("0.5")  # the gateway saw coins land on the address
    session.commit()

    expire_stale_invoices(session)
    session.commit()

    assert session.query(Invoice).one().status.value == "late_payment"
    assert session.query(PromoCode).one().used_count == 1, "a funded invoice returned its use"


def test_the_scrub_backstops_a_terminal_status_expiry_never_sees(client, session, ladder):
    """CANCELLED comes from a provider webhook, not from the expiry job.

    So expiry — the fast refund path — never sees it, and without a backstop the use would sit held
    for the whole 30-day retention window and then vanish silently when scrub_invoice_linkage
    nulled promo_code_id. The scrub is where the linkage dies, so it is where the last chance to
    credit the allowance back is.

    Break check: remove the promo.release_if_unfunded call from invoice_scrub → used_count stays 1
    while the invoice's promo_code_id is wiped, i.e. the use is lost with no way to recover it.
    """
    from app.models.enums import InvoiceStatus
    from app.scheduler.invoice_scrub import scrub_due_invoices

    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "LAUNCH20", 20, max_uses=5)
    _buy(client, promo_code="LAUNCH20")
    inv = session.query(Invoice).one()
    inv.status = InvoiceStatus.CANCELLED          # as a failed/refunded gateway webhook leaves it
    inv.expires_at = datetime.now(UTC) - timedelta(days=60)
    session.commit()
    assert session.query(PromoCode).one().used_count == 1

    scrub_due_invoices(session)
    session.commit()

    assert session.query(PromoCode).one().used_count == 0, "the cancelled invoice kept its use"
    assert session.query(Invoice).one().promo_code_id is None


def test_the_scrub_does_not_refund_an_invoice_that_was_funded(client, session, ladder):
    """Same backstop, opposite case: coins arrived, so the allowance really was spent."""
    from app.models.enums import InvoiceStatus
    from app.scheduler.invoice_scrub import scrub_due_invoices

    registry.register(FakeProvider(name="blockbee", event=PaymentEvent.SEEN))
    _code(session, "LAUNCH20", 20, max_uses=5)
    _buy(client, promo_code="LAUNCH20")
    inv = session.query(Invoice).one()
    inv.status = InvoiceStatus.LATE_PAYMENT
    inv.crypto_amount_paid = Decimal("0.5")
    inv.expires_at = datetime.now(UTC) - timedelta(days=60)
    session.commit()

    scrub_due_invoices(session)
    session.commit()

    assert session.query(PromoCode).one().used_count == 1


def test_release_survives_the_operator_deleting_the_code(session, ladder):
    """A dangling FK must not 500 the whole expiry job for every other invoice in the batch."""
    row = _code(session, "GONESOON", 20, max_uses=5)
    inv = Invoice(
        method=PaymentMethod.LITECOIN, days_granted=90, amount_fiat_cents=1080,
        fiat_currency="USD", discount_percent=20, promo_code_id=row.id,
    )
    session.add(inv)
    session.commit()
    session.delete(row)
    session.commit()

    assert promo.release(session, inv) is False
    assert inv.promo_code_id is None


def test_an_expired_code_cannot_be_reserved(session):
    row = _code(session, "OLDCODE", 20, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(promo.PromoError) as e:
        promo.reserve(session, row)
    assert e.value.reason == "expired"


def test_a_code_expiring_later_today_still_works(session):
    row = _code(session, "TODAYCODE", 20, expires_at=datetime.now(UTC) + timedelta(hours=1))
    promo.reserve(session, row)
    assert row.used_count == 1
