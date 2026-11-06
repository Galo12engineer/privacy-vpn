import io
import logging

from app.logging.no_pii_filter import NoPIIFilter, install_scrubbing, scrub


def test_scrub_ipv4():
    assert scrub("client 203.0.113.5 connected") == "client [redacted-ip] connected"


def test_scrub_ipv6():
    out = scrub("client 2001:db8::1 connected")
    assert "[redacted-ip]" in out
    assert "2001:db8" not in out


def test_scrub_email():
    assert scrub("notify a.b+x@example.com now") == "notify [redacted-email] now"


def test_scrub_keeps_timestamp():
    # HH:MM:SS must not be caught by the IPv6 rule
    assert scrub("event at 12:34:56 ok") == "event at 12:34:56 ok"


def test_scrub_redacts_secret_query_params():
    # the BlockBee GET-callback and upstream URLs with apikey must not settle in the logs verbatim
    line = "GET /v1/payments/webhook/blockbee?token=deadbeefcafe&order_id=o1&expected=0.5 HTTP/1.1"
    out = scrub(line)
    assert "deadbeefcafe" not in out
    assert "token=[redacted]" in out
    # order_id is the invoice UUID: NOT a secret, but it is the account side of the linkage that
    # scrub_invoice_linkage wipes from the DB — in a log line it hands that linkage straight back.
    assert "o1" not in out
    up = scrub("GET https://api.blockbee.io/btc/create/?callback=x&apikey=SUPERSECRET123")
    assert "SUPERSECRET123" not in up
    assert "apikey=[redacted]" in up


def test_scrub_redacts_whole_blockbee_callback():
    # regression: uvicorn.access logs the request line with the query verbatim. The callback carries
    # the on-chain fingerprint (address/txid/amount) + the invoice id; the DB-side scrub destroys
    # exactly these, so surviving in a log file they re-link a public tx to an account.
    line = (
        '172.18.0.4 - "GET /v1/payments/webhook/blockbee?token=abc&order_id=7f3a1c2e-9b44&'
        "expected=0.00021062&address_in=bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq&"
        "txid_in=4a5e1e4baab89f3a32518a88c31bc87f618f76673e2cc77ab2127b7afdeda33b&"
        'value_coin=0.00021062&confirmations=6&pending=0 HTTP/1.1" 200'
    )
    out = scrub(line)
    for leak in ("bc1qar0srrr", "4a5e1e4baab", "7f3a1c2e-9b44", "0.00021062"):
        assert leak not in out, f"{leak} survived the scrub"
    assert "/v1/payments/webhook/blockbee" in out  # the path itself stays useful for ops


def test_scrub_keeps_whitelisted_query_params():
    out = scrub("GET /admin/peers?page=3&status=active&v=ab12 HTTP/1.1")
    assert "page=3" in out and "status=active" in out and "v=ab12" in out


def test_scrub_redacts_sql_bound_parameters():
    # a failing INSERT carries the client row itself; none of the pattern rules (ip/email/query)
    # would recognise a device name or a WireGuard pubkey, so the whole block goes
    err = (
        "(psycopg.errors.StringDataRightTruncation) value too long for type character varying(64)\n"
        "[SQL: INSERT INTO peer (account_id, node_id, public_key, name) VALUES (%s, %s, %s, %s)]\n"
        "[parameters: ('9f1c', 'nl-1', 'kQ3xR8vB2n0pL5yT7wA1sD4fG6hJ9kM2nP5qR8tU0vY=', 'Ivan laptop')]"
    )
    out = scrub(err)
    assert "Ivan laptop" not in out
    assert "kQ3xR8vB2n0pL5yT7wA1sD4fG6hJ9kM2nP5qR8tU0vY=" not in out
    assert "9f1c" not in out
    assert "[parameters: [redacted]]" in out
    assert "INSERT INTO peer" in out  # the statement template stays — no data, still diagnosable


def test_scrub_redacts_attribution_click_id():
    # /buy?subid=… is an affiliate tag: a cross-site identifier we promise not to keep
    out = scrub("GET /buy?subid=partner-42&click_id=ffff HTTP/1.1")
    assert "partner-42" not in out and "ffff" not in out


def test_install_scrubbing_covers_child_logger_via_handler():
    # a child logger's record (cabinet/admin) goes to the root handler; the logger's filter does
    # NOT cover it → install_scrubbing attaches the filter to the HANDLER, and it scrubs the
    # record passing through.
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(h)
    try:
        install_scrubbing()  # among other things attaches NoPIIFilter to the root handlers (incl. h)
        logging.getLogger("cabinet.childtest").warning("peer 203.0.113.7 mail x@y.com")
        h.flush()
        out = buf.getvalue()
        assert "203.0.113.7" not in out
        assert "x@y.com" not in out
        assert "[redacted-ip]" in out
    finally:
        root.removeHandler(h)


def test_filter_redacts_message_and_args(caplog):
    logger = logging.getLogger("test.nopii")
    logger.addFilter(NoPIIFilter())
    with caplog.at_level(logging.INFO, logger="test.nopii"):
        logger.info("peer from %s mail %s", "198.51.100.7", "u@example.org")
    record = caplog.records[-1]
    rendered = record.getMessage()
    assert "198.51.100.7" not in rendered
    assert "u@example.org" not in rendered
    assert "[redacted-ip]" in rendered
    assert "[redacted-email]" in rendered


def test_filter_redacts_exception_arg(caplog):
    # regression: an exception object as a %s-arg (e.g. an httpx error with an IP) used to slip past
    # the scrubber (only str-args were scrubbed). Now we scrub BaseException too.
    logger = logging.getLogger("test.nopii.exc")
    logger.addFilter(NoPIIFilter())
    exc = ConnectionError("failed to connect to 203.0.113.9:8443")
    with caplog.at_level(logging.WARNING, logger="test.nopii.exc"):
        logger.warning("dispatch failed: %s", exc)
    rendered = caplog.records[-1].getMessage()
    assert "203.0.113.9" not in rendered
    assert "[redacted-ip]" in rendered


def test_filter_scrubs_traceback():
    # regression: the Formatter renders the traceback out of record.exc_info, which the filter never
    # touched — so every unhandled 500 wrote client data (SQLAlchemy dumps the INSERT parameters).
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.addFilter(NoPIIFilter())
    logger = logging.getLogger("test.nopii.tb")
    logger.propagate = False
    logger.addHandler(h)
    try:
        try:
            raise ConnectionError(
                "reminder to u@example.org failed: smtp 203.0.113.9 refused; "
                "[SQL: INSERT INTO peer ...] [parameters: ('acct', 'node', 'PUB==', 'laptop')]"
            )
        except ConnectionError as exc:
            logger.error("Exception in ASGI application", exc_info=exc)
        h.flush()
        out = buf.getvalue()
        assert "u@example.org" not in out
        assert "203.0.113.9" not in out
        assert "[redacted-email]" in out and "[redacted-ip]" in out
        # the bound values go as a block (see test_scrub_redacts_sql_bound_parameters)
        assert "laptop" not in out and "[parameters: [redacted]]" in out
        assert "Traceback" in out  # still a usable trace, just scrubbed
    finally:
        logger.removeHandler(h)


def test_filter_keeps_numeric_args(caplog):
    # numeric %d-args must not be cast to string (otherwise TypeError during formatting)
    logger = logging.getLogger("test.nopii.num")
    logger.addFilter(NoPIIFilter())
    with caplog.at_level(logging.INFO, logger="test.nopii.num"):
        logger.info("count=%d ratio=%.2f", 5, 1.5)
    assert caplog.records[-1].getMessage() == "count=5 ratio=1.50"
