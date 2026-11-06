"""No-logs policy: strip client-IP and email from CP logs. Enforced in CI.

The filter is deliberately conservative — better to over-redact than to leave PII.
"""

import logging
import re

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# IPv6: full form (>=4 groups, so that HH:MM:SS is not caught) OR any token containing "::"
_IPV6 = re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){4,7}[A-Fa-f0-9]{1,4}\b|[A-Fa-f0-9:]*::[A-Fa-f0-9:]*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Secrets in query strings (e.g. the BlockBee GET-callback and its upstream URL with apikey), which
# would otherwise end up in uvicorn.access/error. We cut the value up to &/space/quote/end.
# Kept as a BLACKLIST on top of the whitelist below, because it also fires on a bare `apikey=…`
# that is not part of a `?`-query (e.g. inside an exception message or a config dump).
_SECRET_QS = re.compile(
    r"(?i)\b(apikey|api_key|token|nonce|secret|signature|sig|password|passwd|pwd)=[^&\s\"'#]+"
)

# Whole query strings are redacted by WHITELIST, not by blacklist. A blacklist is the wrong shape
# here: uvicorn.access logs the request line with the query verbatim, and the BlockBee GET-callback
# carries `address_in`/`txid_in`/`order_id`/`value_coin` — i.e. exactly the on-chain fingerprint that
# services.billing.scrub_invoice_linkage is built to destroy in the DB. Left in the access log those
# survive the scrub on the same disk and re-link a public transaction to an account (via the log
# timestamp ≈ Subscription.created_at). Any NEW provider param is redacted by default now.
_QS_KEEP = frozenset({"lang", "page", "status", "warn", "v"})
_QUERY_STRING = re.compile(r"\?(?=[^\s\"'#]*=)([^\s\"'#]+)")


# SQLAlchemy appends the bound values of a failing statement as `[parameters: (...)]` / `[parameters:
# [{...}]`. Those values ARE the client row — device name, WireGuard public key, account id, the
# encrypted email blob — and none of them look like an IP or an address, so no pattern rule above
# would ever catch them. The `[SQL: ...]` statement next to it is a template with no data in it and
# stays, so a traceback remains diagnosable: you see which INSERT failed, just not with what.
_SQL_PARAMS = re.compile(r"\[parameters:.*?\](?=\s|$)", re.S)


def _redact_query(m: re.Match) -> str:
    out = []
    for pair in m.group(1).split("&"):
        key, sep, _value = pair.partition("=")
        if not sep or key.lower() in _QS_KEEP:
            out.append(pair)
        else:
            out.append(f"{key}=[redacted]")
    return "?" + "&".join(out)


def scrub(text: str) -> str:
    text = _SQL_PARAMS.sub("[parameters: [redacted]]", text)
    text = _QUERY_STRING.sub(_redact_query, text)
    text = _SECRET_QS.sub(r"\1=[redacted]", text)
    text = _EMAIL.sub("[redacted-email]", text)
    text = _IPV4.sub("[redacted-ip]", text)
    text = _IPV6.sub("[redacted-ip]", text)
    return text


def _scrub_arg(a: object) -> object:
    """Scrub str and exception arguments. Exceptions are the main PII carrier: an httpx error
    carries an IP/URL and is logged as `log.warning("...: %s", e)`; a str-only filter missed it.
    Numbers/other types are returned as is (otherwise %d/%.2f would break on coercion to str)."""
    if isinstance(a, str):
        return scrub(a)
    if isinstance(a, BaseException):
        return scrub(str(a))
    return a


_EXC_FORMATTER = logging.Formatter()


def _scrub_traceback(record: logging.LogRecord) -> None:
    """Render, scrub and freeze the traceback.

    A Filter only ever saw `msg`/`args`, but the Formatter renders the traceback separately out of
    `record.exc_info` — so every unhandled 500 wrote an UNSCRUBBED trace. That is the fattest PII
    carrier in the codebase: uvicorn logs `logger.error(..., exc_info=exc)`, and a SQLAlchemy error
    carries `[SQL: INSERT INTO peer …] [parameters: (account_id, node_id, public_key, '<device
    name>')]` — real client data on disk, which /transparency says cannot happen.

    We render it here, scrub the text, park it in `exc_text` (which Formatter appends as is) and
    clear `exc_info` so nothing downstream can re-render the raw trace.
    """
    if record.exc_info:
        if not record.exc_text:
            record.exc_text = _EXC_FORMATTER.formatException(record.exc_info)
        record.exc_info = None
    if record.exc_text:
        record.exc_text = scrub(record.exc_text)
    if record.stack_info:
        record.stack_info = scrub(record.stack_info)


class NoPIIFilter(logging.Filter):
    """Scrubs the message, the string args and the traceback of every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_scrub_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _scrub_arg(v) for k, v in record.args.items()}
        _scrub_traceback(record)
        return True


def install_scrubbing() -> None:
    """Install NoPIIFilter so that ALL records get scrubbed, including ones propagated from
    child loggers (cabinet/admin_ui/uvicorn.access).

    A LOGGER filter is applied only to records originated by that logger — therefore the
    root filter does NOT cover records from child loggers. The solution:
      - attach the filter to root + uvicorn* (mutate-before-propagate for their own records);
      - attach it to the HANDLERS and to logging.lastResort — a handler filters ANY record
        passing through it, including ones coming from child loggers without their own handlers
        (in prod the app loggers go to root without handlers → logging.lastResort).
    Idempotent in effect (scrubbing twice is harmless)."""
    f = NoPIIFilter()
    root = logging.getLogger()
    root.addFilter(f)
    handlers = set(root.handlers)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.addFilter(f)
        handlers.update(lg.handlers)
    for h in handlers:
        h.addFilter(f)
    if logging.lastResort is not None:
        logging.lastResort.addFilter(f)
