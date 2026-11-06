#!/usr/bin/env python3
"""privacyvpn support relay bot — private two-way support over Telegram.

Flow: user writes to the bot → the message is COPIED to the operator (SUPPORT_BOT_OPERATOR_CHAT_ID).
The operator answers by REPLYING to the copied message → the bot forwards the answer to the user.

Privacy / no-logs:
- OUR OWN bot (not a third-party relay like @livegrambot) → no third party sees the conversation.
- long-polling (getUpdates) → no inbound ports/webhook, works behind NAT/firewall.
- user identity = only their Telegram chat_id (NOT linked to the VPN account, we don't know it).
- what we write to disk is chat_ids ONLY, and only as routing/bookkeeping state: {op_msg_id:
  user_chat_id} to route replies, the set of chat_ids already greeted (so the ACK is sent once),
  and the unanswered queue as {chat_id: ["", first_seen_ts]}. Display names are held in RAM and
  never persisted — chat_id + name on a durable volume would be a customer roster.
  The conversation content is NOT logged (stdout also carries no message text).
- copyMessage (not forward) → the operator sees no "forwarded from", works for text and media
  (an error screenshot from the user arrives as is).

Zero-deps: stdlib only (urllib). Run: SUPPORT_BOT_TOKEN + SUPPORT_BOT_OPERATOR_CHAT_ID in env.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("SUPPORT_BOT_TOKEN", "")
_OPERATOR_RAW = os.environ.get("SUPPORT_BOT_OPERATOR_CHAT_ID", "")
OPERATOR = int(_OPERATOR_RAW) if _OPERATOR_RAW.lstrip("-").isdigit() else 0
API = f"https://api.telegram.org/bot{TOKEN}"
STATE_FILE = pathlib.Path(os.environ.get("SUPPORT_BOT_STATE_FILE", "/data/relay-state.json"))
MAP_MAX = 3000  # mapping cap (message_id grows monotonically → we cut the old ones)
WELCOME = os.environ.get(
    "SUPPORT_BOT_WELCOME",
    "privacyvpn VPN — no signup, no email, no KYC.\n\n"
    "WireGuard. You get an account number, not an account. Keys are generated in your browser, so "
    "the private key never reaches us. Pay in BTC, LTC or USDT.\n\n"
    "This bot is support, not a shop — buying happens on the site, in your browser. Send your "
    "question here and the operator answers in this chat, 1:1.\n\n"
    "⚠️ Never send your device's private key. Your account number and a short description are enough.",
)

# /start card: photo + caption + link buttons.
#
# The buttons are URL buttons to the SITE, never an in-Telegram purchase flow. "Telegram as the
# product interface" is a locked non-goal (decisions.md, Phase-0: the only interface is the web
# cabinet + .onion) — and it is not arbitrary: the private key is generated in the customer's
# BROWSER, so a purchase flow living inside a chat app would have to move key handling somewhere
# it cannot go. A button that OPENS privacyvpn.example.com/buy is a link, not an interface.
#
# The .onion is deliberately NOT a button: Telegram only accepts resolvable http(s) targets and its
# in-app browser cannot reach Tor, so the button would be dead on arrival. It stays copyable text
# in /links, which is where the anti-phishing list lives anyway.
WELCOME_PHOTO = os.environ.get("SUPPORT_BOT_WELCOME_PHOTO", "https://privacyvpn.example.com/static/og.png")
_WELCOME_BUTTONS = [
    [{"text": "Buy access", "url": "https://privacyvpn.example.com/buy"}],
    [{"text": "Redeem a gift code", "url": "https://privacyvpn.example.com/redeem"}],
    [
        {"text": "How it works", "url": "https://privacyvpn.example.com/guides"},
        {"text": "What we store", "url": "https://privacyvpn.example.com/transparency"},
    ],
]
ACK = os.environ.get("SUPPORT_BOT_ACK", "Got it — the operator will reply here.")

# /support — a hint (the relay itself works on any message; the command only makes it explicit).
SUPPORT_PROMPT = os.environ.get(
    "SUPPORT_BOT_SUPPORT",
    "Just send your question here — text or a screenshot — and it reaches the operator "
    "directly, privately (1:1). Replies come back in this chat.\n"
    "⚠️ Never send your device's private key. Your invoice number and a short description are enough.",
)

# /links — the single trusted list of links (anti-phishing). Channel — optional, from env.
_CHANNEL = os.environ.get("SUPPORT_BOT_CHANNEL_URL", "").strip()
_LINKS_LINES = [
    "🔗 Official privacyvpn links — always verify here; beware of phishing copies.",
    "",
    "Website: https://privacyvpn.example.com",
    "Mirror: https://privacyvpn.example.net",
    "Tor (.onion): http://privacyvpnexampleonionaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion",
    "Support bot: https://t.me/privacyvpn_support_bot",
]
if _CHANNEL:
    _LINKS_LINES.append(f"Channel: {_CHANNEL}")
_LINKS_LINES += ["", "We never DM you first and never ask for your device's private key."]
LINKS = os.environ.get("SUPPORT_BOT_LINKS", "\n".join(_LINKS_LINES))

# /gift — the canned answer for a gift-code holder. It exists because this is the question the
# operator would otherwise retype: the code arrives from a marketplace with no instructions, and the
# one mistake in it is expensive for the CUSTOMER — redeeming while logged out mints a second
# account, and every device then has to be set up again against a new number. Everything here is a
# link to the site, not a flow inside the chat: redemption hands out an account number, and a
# credential does not belong in a Telegram window.
GIFT = os.environ.get(
    "SUPPORT_BOT_GIFT",
    "🎁 A gift code is access someone already paid for.\n\n"
    "Redeem it at https://privacyvpn.example.com/redeem — paste the whole code, including the TT- part. "
    "Case and dashes do not matter. Nothing to pay: the clock starts when you redeem, not when the "
    "code was bought.\n\n"
    "Already have an account? Log in FIRST, then redeem — the days go onto that account and your "
    "devices stay untouched. Redeeming while logged out creates a second account with a new number, "
    "and every device has to be set up again.\n\n"
    "We cannot restore or replace a code: only its hash is stored on our side, and it can be "
    "redeemed once. Until you redeem it, treat it like cash.",
)


# The command menu — the hint list Telegram shows next to the input field. It is NOT inferred from
# the code: an unregistered command still works when typed, it is simply invisible, which for /gift
# defeats the point (the person who needs it is a code holder who opened the bot instead of the
# site and has no reason to guess). Registered on every start rather than by hand at @BotFather:
# setMyCommands is idempotent, so a redeploy re-syncs the list with the code and the two cannot
# drift.
_PUBLIC_COMMANDS = [
    {"command": "start", "description": "What this is, and where to buy"},
    {"command": "gift", "description": "Redeem a gift code"},
    {"command": "support", "description": "Ask the operator"},
    {"command": "links", "description": "Official links (anti-phishing)"},
]
# /queue stays out of the public list: it is an operator tool, and a menu entry would advertise it
# to every customer. The operator gets it through a chat-scoped menu, which Telegram shows only to
# that chat.
_OPERATOR_COMMANDS = _PUBLIC_COMMANDS + [{"command": "queue", "description": "Unanswered queue"}]


def _api(method: str, **params) -> dict:
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    try:
        with urllib.request.urlopen(f"{API}/{method}", data=data, timeout=65) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False}
    except Exception as e:  # noqa: BLE001 — a network failure must not bring the bot down
        print(f"[api] {method}: {type(e).__name__}", flush=True)
        return {"ok": False}


def _load() -> tuple[dict[int, int], set[int], dict[int, list]]:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        relay = {int(k): int(v) for k, v in d.get("relay", {}).items()}
        acked = {int(x) for x in d.get("acked", [])}
        open_q = {int(k): [v[0], int(v[1])] for k, v in d.get("open", {}).items()}
        return relay, acked, open_q
    except Exception:
        return {}, set(), {}


def _save(relay: dict[int, int], acked: set[int], open_q: dict[int, list]) -> None:
    if len(relay) > MAP_MAX:  # keep the last MAP_MAX keys
        for k in sorted(relay)[: len(relay) - MAP_MAX]:
            relay.pop(k, None)
    if len(acked) > MAP_MAX:
        acked = set(sorted(acked)[-MAP_MAX:])
    if len(open_q) > MAP_MAX:  # unanswered ones are self-limiting, but let's play it safe (cut the oldest)
        for k in sorted(open_q, key=lambda x: open_q[x][1])[: len(open_q) - MAP_MAX]:
            open_q.pop(k, None)
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps({
                "relay": relay,
                "acked": sorted(acked),
                # The display name is deliberately NOT persisted. A chat_id is a routing address we
                # cannot avoid keeping (it is what a reply is sent to), but chat_id + first_name on a
                # durable volume is a "who our customers are" roster — a category /privacy does not
                # list. The name lives in RAM for the lifetime of the process; after a restart
                # /queue shows the id alone, which is enough to answer.
                "open": {k: ["", ts] for k, (_name, ts) in open_q.items()},
            }),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        print(f"[state] save: {type(e).__name__}", flush=True)


def _fmt_wait(seconds: int) -> str:
    m = max(0, seconds) // 60
    return f"{m} мин" if m < 60 else f"{m // 60} ч {m % 60} мин"


def _send_welcome(chat_id: int) -> None:
    """The /start card, with a text fallback that is not decoration.

    Telegram fetches WELCOME_PHOTO from our own domain by URL; if that fetch fails — a deploy
    window, a moved file, our own site briefly down — sendPhoto answers ok:false and nothing
    reaches the user. Silence on first contact is this relay's defining failure mode, so a failed
    photo degrades to the same text and the same buttons rather than to nothing.
    """
    markup = json.dumps({"inline_keyboard": _WELCOME_BUTTONS})
    if WELCOME_PHOTO:
        sent = _api(
            "sendPhoto", chat_id=chat_id, photo=WELCOME_PHOTO, caption=WELCOME, reply_markup=markup
        )
        if sent.get("ok"):
            return
        print("[welcome] sendPhoto failed → falling back to text", flush=True)
    _api(
        "sendMessage",
        chat_id=chat_id,
        text=WELCOME,
        reply_markup=markup,
        disable_web_page_preview="true",
    )


def _handle_command(msg: dict, chat_id: int, open_q: dict[int, list]) -> bool:
    """Hint commands (/start /support /links /gift) — for everyone; /queue — operator only.
    They reply to the sender and are NOT relayed. Returns True if the update is a handled command."""
    text = msg.get("text", "").strip()
    if not text.startswith("/"):
        return False
    # first token without the @botname suffix: "/links@privacyvpn_support_bot" → "/links"
    cmd = text.split()[0].split("@", 1)[0].lower()
    if cmd == "/queue":
        if chat_id != OPERATOR:
            return False  # for a user this is just text → it goes to the relay
        if not open_q:
            body = "✅ Очередь пуста — все ответили."
        else:
            now = int(time.time())
            rows = [f"⏳ Ждут ответа ({len(open_q)}):"]
            for uid, (nm, ts) in sorted(open_q.items(), key=lambda kv: kv[1][1]):
                # nm is empty for entries reloaded from disk (names are not persisted) — the id alone
                # is enough to answer, and the copied message right above carries the context.
                who = f"{nm} (id {uid})" if nm else f"id {uid}"
                rows.append(f"• {who} — {_fmt_wait(now - ts)}")
            body = "\n".join(rows)
        _api("sendMessage", chat_id=chat_id, text=body)
        return True
    if cmd == "/start":
        _send_welcome(chat_id)
        return True
    reply = {"/support": SUPPORT_PROMPT, "/links": LINKS, "/gift": GIFT}.get(cmd)
    if reply is None:
        return False
    _api("sendMessage", chat_id=chat_id, text=reply, disable_web_page_preview="true")
    return True


def _handle_operator(msg: dict, relay: dict[int, int], acked: set[int], open_q: dict[int, list]) -> bool:
    """The operator answered by replying to the copied message → send it to the user, drop it from the queue.

    Returns True if the update was consumed here (routed to a user, or reported as unroutable).
    A reply must never fall through silently: the operator would believe he had answered."""
    reply = msg.get("reply_to_message")
    if not reply:
        return False
    user = relay.get(reply["message_id"])
    if user is None:
        # Replying to one of our own messages we cannot route (state lost on restart, mapping
        # pruned past MAP_MAX). Silence would let the operator think the user got an answer.
        if reply.get("from", {}).get("is_bot"):
            _api(
                "sendMessage",
                chat_id=OPERATOR,
                text="⚠️ Не могу определить получателя этого ответа — реплай на слишком старое "
                "сообщение. Попроси юзера написать ещё раз и ответь на новое.",
            )
            return True
        return False
    r = _api("copyMessage", chat_id=user, from_chat_id=OPERATOR, message_id=msg["message_id"])
    if not r.get("ok"):
        _api("sendMessage", chat_id=OPERATOR, text="⚠️ не доставлено (юзер мог заблокировать бота)")
        return True
    if open_q.pop(user, None) is not None:  # answered → out of the queue
        _save(relay, acked, open_q)
    return True


def _handle_user(msg: dict, chat_id: int, relay: dict[int, int], acked: set[int], open_q: dict[int, list]) -> None:
    name = msg["chat"].get("first_name") or msg["chat"].get("username") or "user"
    tag = "" if chat_id in acked else "🆕 "  # first contact — a new user
    header = _api(
        "sendMessage", chat_id=OPERATOR, text=f"💬 {tag}{name} (id {chat_id}) — ответь РЕПЛАЕМ на сообщение ниже:"
    )
    copied = _api("copyMessage", chat_id=OPERATOR, from_chat_id=chat_id, message_id=msg["message_id"])
    if copied.get("ok"):
        relay[copied["result"]["message_id"]] = chat_id
        # The header sits directly above the copy, so replying to it instead is the natural
        # slip — route it to the same user rather than dropping the answer.
        if header.get("ok"):
            relay[header["result"]["message_id"]] = chat_id
        open_q.setdefault(chat_id, [name, int(time.time())])  # waiting since the first unanswered one
        open_q[chat_id][0] = name
        if chat_id not in acked:
            acked.add(chat_id)
            _api("sendMessage", chat_id=chat_id, text=ACK)
        _save(relay, acked, open_q)


def _dispatch(msg: dict, chat_id: int, relay: dict[int, int], acked: set[int], open_q: dict[int, list]) -> None:
    """Route one update. The order is the contract: for the operator a reply is always an answer
    to a user — even when its text starts with "/" — so it is checked before the commands."""
    if chat_id == OPERATOR:
        if not _handle_operator(msg, relay, acked, open_q):
            _handle_command(msg, chat_id, open_q)
        return
    if _handle_command(msg, chat_id, open_q):
        return
    _handle_user(msg, chat_id, relay, acked, open_q)


def _register_commands() -> None:
    """Publish the command menu. Failures are non-fatal — _api swallows them, and a bot with a stale
    menu still answers every command; refusing to start over a cosmetic call would be worse."""
    _api("setMyCommands", commands=json.dumps(_PUBLIC_COMMANDS))
    if OPERATOR:
        _api(
            "setMyCommands",
            commands=json.dumps(_OPERATOR_COMMANDS),
            scope=json.dumps({"type": "chat", "chat_id": OPERATOR}),
        )


def main() -> None:
    if not TOKEN or not OPERATOR:
        # deploy-safe: without a token/chat we don't crash, we idle (container up, idle).
        # The operator will put SUPPORT_BOT_* into .env + restart → the bot comes alive.
        print("[bot] SUPPORT_BOT_TOKEN / SUPPORT_BOT_OPERATOR_CHAT_ID unset — idle (disabled)", flush=True)
        while True:
            time.sleep(3600)
    relay, acked, open_q = _load()
    offset = None
    _register_commands()
    print("[bot] support relay started (long-polling)", flush=True)
    while True:
        resp = _api("getUpdates", offset=offset, timeout=30)
        if not resp.get("ok"):
            time.sleep(3)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg or "chat" not in msg:
                continue
            chat_id = msg["chat"]["id"]
            try:
                _dispatch(msg, chat_id, relay, acked, open_q)
            except Exception as e:  # noqa: BLE001 — one broken update must not kill the loop
                print(f"[loop] {type(e).__name__}", flush=True)


if __name__ == "__main__":
    main()
