"""BlockBee — non-custodial «address per order» + GET-callback (no HMAC).

A hybrid of monero.py (unique address per invoice) and gateway.py (push webhook):

- create_invoice: we quote fiat→crypto via `/{ticker}/convert/`, then generate a
  unique `address_in` via `/{ticker}/create/`, passing OUR OWN operator payout address.
  BlockBee forwards the incoming funds to payout and pokes our callback.
- callback: by default BlockBee sends a GET to the callback URL, appending its own params
  (address_in / txid_in / confirmations / value_coin / pending / …). BlockBee has neither
  HMAC nor headers, so we confirm authenticity with a DERIVED per-invoice token
  HMAC(secret, "order_id:expected") baked into the callback URL (the master secret itself
  never goes into the URL). The expected amount (`expected`) also goes into the callback URL →
  parse_webhook decides SEEN / SETTLED / UNDERPAID on its own, staying stateless.
- ack: our route returns `*ok*` / HTTP 200 → BlockBee stops retrying (see app/api/payments.py).

Non-custodial: the CP never becomes a custodian — funds go directly to the operator's payout address.
Tickers: BTC=`btc`, LTC=`ltc`, USDT-TRON=`trc20_usdt` (in the URL `_`→`/` = `trc20/usdt`).
Docs: https://docs.blockbee.io/get-started/custom-payment-flow
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx

from app.payments.base import InvoiceRequest, PaymentProvider, ProviderInvoice, ProviderStatus
from app.services.billing_fsm import PaymentEvent

# coin string (purchase._COIN) → internal BlockBee ticker (in the URL `_`→`/`).
_TICKER = {
    "BTC": "btc",
    "LTC": "ltc",
    "USDT-TRC20": "trc20_usdt",
}


def _dec(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes")


class BlockBeeProvider(PaymentProvider):
    name = "blockbee"

    def __init__(
        self,
        *,
        api_key: str,
        callback_secret: str,
        payout_addresses: dict[str, str],
        callback_base_url: str,
        confirmations: dict[str, int] | None = None,
        underpaid_tolerance: str | float = "0.01",
        base_url: str = "https://api.blockbee.io",
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key
        self.callback_secret = callback_secret
        self.payout_addresses = payout_addresses
        self.callback_base_url = callback_base_url.rstrip("/")
        self.confirmations = confirmations or {}
        self.tolerance = Decimal(str(underpaid_tolerance))
        self.base_url = base_url.rstrip("/")
        self._client = client

    # --- HTTP ---------------------------------------------------------------
    def _get(self, ticker: str, endpoint: str, params: dict, *, timeout: float = 15) -> dict:
        # apikey goes as a query param → we MUST NOT let an httpx exception leak: the text of
        # HTTPStatusError/RequestError contains the full URL with the apikey and would land in the logs.
        url = f"{self.base_url}/{ticker.replace('_', '/')}/{endpoint}/"
        client = self._client or httpx.Client(timeout=timeout)
        try:
            resp = client.get(
                url,
                params={**params, "apikey": self.api_key},
                headers={"Host": "api.blockbee.io"},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise ValueError(f"blockbee {endpoint} http {e.response.status_code}") from None
        except httpx.HTTPError as e:
            raise ValueError(f"blockbee {endpoint} request failed: {type(e).__name__}") from None
        finally:
            if self._client is None:
                client.close()
        if data.get("status") == "error":
            raise ValueError(f"blockbee {endpoint} error: {data.get('error')}")
        return data

    @staticmethod
    def _amount_key(expected: Decimal) -> str:
        """Canonical string for `expected` — the SAME one at creation and at reconcile time.

        The callback URL is our only authentication channel, and its token is HMAC over this
        string. create_invoice signs the freshly quoted Decimal (e.g. 0.00021062); reconcile signs
        invoice.crypto_amount read back from Numeric(30,12), which Postgres pads to
        0.000210620000. Numerically equal, textually not — so plain format(expected, "f") produced
        a DIFFERENT token, i.e. reconcile asked BlockBee's /logs/ for a callback URL that never
        existed and always found nothing. The lost-payment safety net looked wired and did nothing.
        normalize() strips the padding; "f" keeps 1E+2 / 1E-8 out of scientific notation.
        The webhook is unaffected: it re-signs whatever string BlockBee echoes back to us.
        """
        return format(expected.normalize(), "f")

    def _callback_url(self, *, order_id: str, expected: Decimal) -> str:
        # BlockBee provides neither an HMAC nor any headers of its own on the callback — the only
        # authentication channel is the callback URL itself. We put in a DERIVED per-invoice token
        # HMAC(secret, "order_id:expected"), NOT the master secret: a leak of a single log line
        # neither reveals the secret nor allows forging other invoices; tampering with expected breaks the token.
        expected_s = self._amount_key(expected)
        token = self._token(order_id, expected_s)
        params = {"token": token, "order_id": order_id, "expected": expected_s}
        return f"{self.callback_base_url}/v1/payments/webhook/blockbee?{urlencode(params)}"

    def _token(self, order_id: str, expected_s: str) -> str:
        msg = f"{order_id}:{expected_s}".encode()
        return hmac.new(self.callback_secret.encode(), msg, hashlib.sha256).hexdigest()

    # --- PaymentProvider ----------------------------------------------------
    def create_invoice(self, req: InvoiceRequest) -> ProviderInvoice:
        ticker = _TICKER.get(req.coin)
        if ticker is None:
            raise ValueError(f"blockbee: unsupported coin {req.coin}")
        payout = self.payout_addresses.get(req.coin)
        if not payout:
            raise ValueError(f"blockbee: no payout address for {req.coin}")
        conf = self.confirmations.get(req.coin, 1)

        # 1) fiat→crypto quote (rate-lock on our side; we show the amount to the client)
        conv = self._get(
            ticker,
            "convert",
            {"from": req.fiat_currency.lower(), "value": req.amount_fiat_cents / 100},
        )
        value_coin = _dec(conv.get("value_coin"))
        rate = _dec(conv.get("exchange_rate"))
        # a positive quote is mandatory: otherwise expected≤0 and any callback → SETTLED
        # (free activation). We refuse to create an invoice on a broken quote.
        if value_coin is None or value_coin <= 0:
            raise ValueError(f"blockbee: bad convert quote value_coin={conv.get('value_coin')!r}")

        # 2) unique address_in for this invoice; forwarding to the operator's payout
        data = self._get(
            ticker,
            "create",
            {
                "callback": self._callback_url(order_id=req.order_id, expected=value_coin),
                "address": payout,
                "pending": 1,          # we also want the mempool callback (0-conf → SEEN)
                "confirmations": conf,  # the «confirmed» threshold → SETTLED callback
            },
        )
        address_in = data.get("address_in")
        if not address_in:
            raise ValueError(f"blockbee: no address_in (status={data.get('status')})")

        return ProviderInvoice(
            external_id=address_in,       # the matching key in the webhook
            payment_address=address_in,
            crypto_amount=value_coin,
            crypto_currency=req.coin,
            exchange_rate=rate,
            rate_expires_at=None,
            confirmations_required=conf,
        )

    def min_transaction_usd(self, coin: str) -> Decimal | None:
        """BlockBee's CURRENT forwarding threshold for this coin, converted to USD. None if unknown.

        `GET /{ticker}/info/` reports `minimum_transaction_coin` (fixed in the coin) plus a `prices`
        map. The threshold matters in USD because that is what our plans are priced in, and the
        conversion is exactly what drifts as the rate moves. Consumed by services/min_tx, which
        never lets this LOWER the gate — so a wrong or stale answer here cannot strand money.

        Short timeout on purpose: this sits on the /buy render path, and a slow gateway must cost a
        page a moment, not a quarter of a minute. Raises ValueError (from _get) on failure — the
        caller catches and falls back to the static floor.
        """
        ticker = _TICKER.get(coin)
        if ticker is None:
            return None
        data = self._get(ticker, "info", {"prices": 1}, timeout=5)
        min_coin = _dec(data.get("minimum_transaction_coin"))
        prices = data.get("prices")
        price_usd = _dec(prices.get("USD")) if isinstance(prices, dict) else None
        if not min_coin or not price_usd or min_coin <= 0 or price_usd <= 0:
            return None
        return min_coin * price_usd

    def get_status(self, external_id: str) -> ProviderStatus:
        """BlockBee is webhook-first: the API has no status by address_in (logs are keyed by the
        callback URL, not by the address). We return a safe SEEN placeholder — the source of truth is the callback.
        Neither SEEN nor missing data activates or cancels an invoice."""
        return ProviderStatus(external_id=external_id, event=PaymentEvent.SEEN)

    # --- #6 reconcile (lost callback) ----------------------------------------
    def reconcile(self, *, order_id: str, coin: str, expected: Decimal) -> ProviderStatus | None:
        """Polls `GET /{ticker}/logs/` for an invoice whose webhook may not have arrived (network/CP restart).

        We reconstruct the callback URL DETERMINISTICALLY with the same `_callback_url` as at
        creation time (order_id+expected+secret) — BlockBee matches the logs request by it too.
        BlockBee's response per the docs (docs.blockbee.io/api/get-payment-logs):
        `{"address_in", "status", "callbacks": [{"txid_in","value_coin","confirmations",
        "result": "pending"|"sent"|"done", ...}]}`. The format is nowhere officially versioned
        (unofficial contract) → parsing is MAXIMALLY defensive: any unexpected shape (not a
        dict/list/unknown fields) → None, we never raise — reconciling ONE invoice
        must not bring down the job.
        """
        ticker = _TICKER.get(coin)
        if ticker is None:
            return None
        callback_url = self._callback_url(order_id=order_id, expected=expected)
        try:
            data = self._get(ticker, "logs", {"callback": callback_url})
        except ValueError:
            return None  # the invoice is unknown to BlockBee (not our provider/404/network failure) — not our case
        callbacks = data.get("callbacks")
        if not isinstance(callbacks, list) or not callbacks:
            return None

        # SUM the confirmed value over UNIQUE transactions. BlockBee reports value per transaction
        # and repeats a callback for the same txid as it advances pending→sent→done, so we keep the
        # most advanced entry per txid and add those up. Taking only the single most-advanced
        # callback (as this did) judged a split payment by ONE of its parts, so an invoice the
        # customer had fully paid in two transactions could never be rescued.
        _RANK = {"done": 3, "sent": 2, "pending": 1}
        # Does ANY entry identify its transaction? This decides how we treat the ones that do not.
        any_txid = any(
            str(e.get("txid_in") or "").strip() for e in callbacks if isinstance(e, dict)
        )
        per_tx: dict[str, tuple[int, Decimal | None, int | None]] = {}
        for entry in callbacks:
            if not isinstance(entry, dict):
                continue
            rank = _RANK.get(str(entry.get("result", "")).lower(), 0)
            txid = str(entry.get("txid_in") or "").strip().lower()
            # An unidentified entry alongside identified ones is almost certainly ANOTHER report of
            # a transaction we already counted — bucketing it separately would sum one payment
            # twice and settle a half-paid invoice (a free subscription, granted unattended, with
            # nothing downstream comparing paid vs expected afterwards). Drop it: under-counting
            # only leaves the invoice rescuable, over-counting hands out money. When NO entry has a
            # txid we cannot dedup at all, so they all collapse into one bucket and the most
            # advanced survives — the conservative pre-accumulation behaviour.
            if any_txid and not txid:
                continue
            key = txid
            prev = per_tx.get(key)
            if prev is not None and rank <= prev[0]:
                continue
            raw_conf = entry.get("confirmations")
            conf_i = int(raw_conf) if str(raw_conf).strip().isdigit() else None
            per_tx[key] = (rank, _dec(entry.get("value_coin")), conf_i)
        if not per_tx:
            return None

        confirmed = [(v, c) for (r, v, c) in per_tx.values() if r >= _RANK["sent"] and v is not None]
        total = sum((v for v, _c in confirmed), Decimal(0)) if confirmed else None
        # the weakest confirmation count among the parts describes the payment as a whole
        conf = min((c for _v, c in confirmed if c is not None), default=None)

        if total is None:
            event = PaymentEvent.SEEN
        elif expected > 0 and total < expected * (Decimal(1) - self.tolerance):
            event = PaymentEvent.UNDERPAID
        else:
            event = PaymentEvent.SETTLED

        address_in = str(data.get("address_in") or "")
        return ProviderStatus(external_id=address_in, event=event, amount_paid=total, confirmations=conf)

    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        # For the GET callback the route synthesizes a body from the query params (see api/payments.py).
        # Authentication: the per-invoice token HMAC(secret, "order_id:expected"). The token
        # covers expected as well → tampering with the amount (e.g. expected=0) gives a mismatch → refusal.
        try:
            params = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return False
        token = str(params.get("token", ""))
        order_id = str(params.get("order_id", ""))
        expected_s = str(params.get("expected", ""))
        if not self.callback_secret or not token or not order_id or not expected_s:
            return False
        return hmac.compare_digest(token, self._token(order_id, expected_s))

    def parse_webhook(self, headers: dict, body: bytes) -> ProviderStatus:
        params = json.loads(body or b"{}")
        value = _dec(params.get("value_coin"))
        expected = _dec(params.get("expected"))
        raw_conf = params.get("confirmations")
        conf = int(raw_conf) if str(raw_conf).strip().isdigit() else None
        pending = _truthy(params.get("pending", "0"))

        # value_coin is PER TRANSACTION, not cumulative, so this verdict is provisional: the caller
        # accumulates by txid against the invoice and re-decides on the TOTAL (see api/payments.py).
        # Judging a split payment by its last part is what left fully-paid invoices UNDERPAID.
        # expected>0 is guaranteed by create_invoice + the token (see verify_webhook).
        if pending or value is None:
            event = PaymentEvent.SEEN          # mempool / not yet confirmed
        elif expected is not None and expected > 0 and value < expected * (Decimal(1) - self.tolerance):
            event = PaymentEvent.UNDERPAID     # confirmed, but this tx alone is short
        else:
            event = PaymentEvent.SETTLED       # confirmed and this tx alone covers it

        return ProviderStatus(
            external_id=str(params.get("address_in", "")),
            event=event,
            amount_paid=value,
            confirmations=conf,
            # Normalized exactly like the poll's bucketing (see reconcile). A whitespace-only
            # txid_in must read as ABSENT, not as an id: left raw it passed the route's guard,
            # normalized to "" inside record_tx, and became a bucket separate from the real txid —
            # summing ONE payment twice and settling a half-paid invoice on the primary path.
            txid=str(params.get("txid_in") or "").strip().lower() or None,
        )
