"""Behavioural tests for the support relay.

The relay's failure mode is silence: every path here is one where the operator believes he
answered a paying customer while the customer received nothing. Run:

    control-plane/.venv/bin/python -m pytest support-bot -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib

import pytest

OPERATOR_ID = 123456789
USER_ID = 111222333
CHANNEL_URL = "https://t.me/privacyvpn_vpn"


class FakeTelegram:
    """Stands in for _api: records every call, hands back monotonic message ids."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._next_id = 1000

    def __call__(self, method: str, **params) -> dict:
        self.calls.append((method, params))
        if method in ("sendMessage", "copyMessage"):
            self._next_id += 1
            return {"ok": True, "result": {"message_id": self._next_id}}
        return {"ok": True}

    def to(self, chat_id: int) -> list[tuple[str, dict]]:
        return [(m, p) for m, p in self.calls if p.get("chat_id") == chat_id]

    def texts_to(self, chat_id: int) -> str:
        # caption too: what the user actually read does not depend on whether we shipped it as a
        # message or as a photo caption, and /start became a photo card.
        return "\n".join(p.get("text", "") or p.get("caption", "") for _, p in self.to(chat_id))


@pytest.fixture
def bot(tmp_path, monkeypatch):
    # TOKEN/OPERATOR are read at import time → the env must be set before loading the module.
    monkeypatch.setenv("SUPPORT_BOT_TOKEN", "test-token")
    monkeypatch.setenv("SUPPORT_BOT_OPERATOR_CHAT_ID", str(OPERATOR_ID))
    monkeypatch.setenv("SUPPORT_BOT_STATE_FILE", str(tmp_path / "relay-state.json"))
    monkeypatch.setenv("SUPPORT_BOT_CHANNEL_URL", CHANNEL_URL)
    for stale in ("SUPPORT_BOT_WELCOME", "SUPPORT_BOT_ACK", "SUPPORT_BOT_LINKS", "SUPPORT_BOT_SUPPORT"):
        os.environ.pop(stale, None)

    spec = importlib.util.spec_from_file_location("relaybot", pathlib.Path(__file__).with_name("bot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.fake = FakeTelegram()
    monkeypatch.setattr(mod, "_api", mod.fake)
    return mod


def user_msg(text: str, msg_id: int = 1) -> dict:
    return {"message_id": msg_id, "chat": {"id": USER_ID, "first_name": "Alice"}, "text": text}


def op_msg(text: str, msg_id: int, reply_to: int | None = None, reply_from_bot: bool = True) -> dict:
    msg = {"message_id": msg_id, "chat": {"id": OPERATOR_ID, "first_name": "op"}, "text": text}
    if reply_to is not None:
        msg["reply_to_message"] = {"message_id": reply_to, "from": {"is_bot": reply_from_bot}}
    return msg


def relay_one(bot, state, text: str = "my config won't connect") -> tuple[int, int]:
    """A user writes in; returns the two ids the operator can reply to: (header, copy)."""
    bot._dispatch(user_msg(text), USER_ID, *state)
    header_id, copy_id = sorted(state[0])  # header is sent first → lower message_id
    return header_id, copy_id


@pytest.fixture
def state() -> tuple[dict, set, dict]:
    return {}, set(), {}


def test_first_contact_reaches_operator_tagged_and_acks_the_user(bot, state):
    bot._dispatch(user_msg("hello"), USER_ID, *state)
    relay, acked, open_q = state

    assert "🆕" in bot.fake.texts_to(OPERATOR_ID)
    assert f"id {USER_ID}" in bot.fake.texts_to(OPERATOR_ID)
    assert any(m == "copyMessage" for m, _ in bot.fake.to(OPERATOR_ID))
    assert "Got it" in bot.fake.texts_to(USER_ID)
    assert set(relay.values()) == {USER_ID}
    assert USER_ID in acked and USER_ID in open_q


def test_second_message_from_known_user_is_not_tagged_new(bot, state):
    bot._dispatch(user_msg("hello", 1), USER_ID, *state)
    bot.fake.calls.clear()
    bot._dispatch(user_msg("still there?", 2), USER_ID, *state)
    assert "🆕" not in bot.fake.texts_to(OPERATOR_ID)


def test_operator_reply_to_the_copy_reaches_the_user(bot, state):
    _, copy_id = relay_one(bot, state)
    bot.fake.calls.clear()

    bot._dispatch(op_msg("try re-adding the device", 50, reply_to=copy_id), OPERATOR_ID, *state)

    to_user = bot.fake.to(USER_ID)
    assert to_user and to_user[0][0] == "copyMessage"
    assert to_user[0][1]["message_id"] == 50
    assert USER_ID not in state[2]  # answered → out of the queue


def test_operator_reply_to_the_header_reaches_the_user(bot, state):
    """Regression: the header sits above the copy, so replying to it is the natural slip.
    It used to map to nothing and the answer vanished without a word to the operator."""
    header_id, _ = relay_one(bot, state)
    bot.fake.calls.clear()

    bot._dispatch(op_msg("try re-adding the device", 51, reply_to=header_id), OPERATOR_ID, *state)

    to_user = bot.fake.to(USER_ID)
    assert to_user and to_user[0][0] == "copyMessage"
    assert USER_ID not in state[2]


def test_operator_can_answer_a_user_with_slash_links(bot, state):
    """Regression: /links is the canned anti-phishing answer. Commands used to be handled
    before the operator branch, so replying "/links" bounced the list back to the operator
    and the customer got nothing."""
    _, copy_id = relay_one(bot, state, "is privacyvpn-alt.example.net yours?")
    bot.fake.calls.clear()

    bot._dispatch(op_msg("/links", 52, reply_to=copy_id), OPERATOR_ID, *state)

    to_user = bot.fake.to(USER_ID)
    assert to_user and to_user[0][0] == "copyMessage"
    assert to_user[0][1]["message_id"] == 52
    assert "Official privacyvpn links" not in bot.fake.texts_to(OPERATOR_ID)


def test_operator_slash_links_without_a_reply_still_answers_the_operator(bot, state):
    bot._dispatch(op_msg("/links", 60), OPERATOR_ID, *state)

    text = bot.fake.texts_to(OPERATOR_ID)
    assert "Official privacyvpn links" in text
    assert CHANNEL_URL in text  # channel comes from the env, as on prod
    assert "privacyvpnexampleonionaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion" in text
    assert not bot.fake.to(USER_ID)


def test_reply_to_an_unroutable_message_warns_instead_of_dropping(bot, state):
    """Regression: after a restart that lost state (or past MAP_MAX), the mapping is gone.
    Silence let the operator believe the customer had been answered."""
    bot._dispatch(op_msg("here you go", 70, reply_to=999999), OPERATOR_ID, *state)

    assert "Не могу определить получателя" in bot.fake.texts_to(OPERATOR_ID)
    assert not bot.fake.to(USER_ID)


def test_operator_reply_to_his_own_message_is_not_treated_as_an_answer(bot, state):
    bot._dispatch(op_msg("note to self", 71, reply_to=999999, reply_from_bot=False), OPERATOR_ID, *state)

    assert not bot.fake.to(USER_ID)
    assert "Не могу определить получателя" not in bot.fake.texts_to(OPERATOR_ID)


def test_undelivered_answer_tells_the_operator(bot, state):
    _, copy_id = relay_one(bot, state)

    def refuse_to_user(method, **params):
        bot.fake.calls.append((method, params))
        if method == "copyMessage" and params.get("chat_id") == USER_ID:
            return {"ok": False}  # user blocked the bot
        return {"ok": True, "result": {"message_id": 9999}}

    bot._api = refuse_to_user
    bot.fake.calls.clear()
    bot._dispatch(op_msg("answer", 53, reply_to=copy_id), OPERATOR_ID, *state)

    assert "не доставлено" in "\n".join(p.get("text", "") for m, p in bot.fake.calls if m == "sendMessage")
    assert USER_ID in state[2]  # unanswered → stays in the queue


def test_queue_is_operator_only_and_a_users_slash_queue_is_relayed(bot, state):
    bot._dispatch(user_msg("/queue"), USER_ID, *state)

    assert any(m == "copyMessage" for m, _ in bot.fake.to(OPERATOR_ID))
    assert "Ждут ответа" not in bot.fake.texts_to(OPERATOR_ID)
    assert "Очередь пуста" not in bot.fake.texts_to(USER_ID)


def test_queue_lists_the_waiting_user_then_empties_after_an_answer(bot, state):
    _, copy_id = relay_one(bot, state)
    bot.fake.calls.clear()

    bot._dispatch(op_msg("/queue", 80), OPERATOR_ID, *state)
    listing = bot.fake.texts_to(OPERATOR_ID)
    assert "Ждут ответа" in listing and "Alice" in listing and f"id {USER_ID}" in listing

    bot._dispatch(op_msg("answered", 81, reply_to=copy_id), OPERATOR_ID, *state)
    bot.fake.calls.clear()
    bot._dispatch(op_msg("/queue", 82), OPERATOR_ID, *state)
    assert "Очередь пуста" in bot.fake.texts_to(OPERATOR_ID)


def test_start_warns_against_sending_the_private_key(bot, state):
    bot._dispatch(user_msg("/start"), USER_ID, *state)

    text = bot.fake.texts_to(USER_ID)
    assert "private key" in text
    assert not bot.fake.to(OPERATOR_ID)  # a command is not relayed


def test_state_round_trips_through_disk(bot, state):
    relay_one(bot, state)
    relay, acked, open_q = state

    bot._save(relay, acked, open_q)
    loaded_relay, loaded_acked, loaded_open = bot._load()

    assert loaded_relay == relay
    assert loaded_acked == acked
    # the queue entry survives (the operator still sees who is waiting and for how long)...
    assert USER_ID in loaded_open
    assert loaded_open[USER_ID][1] == open_q[USER_ID][1]
    # ...but the display name does NOT: chat_id + name on a durable volume is a customer roster.
    assert loaded_open[USER_ID][0] == ""
    assert "Alice" not in bot.STATE_FILE.read_text(encoding="utf-8")
    # in RAM the name is still there for this process, so /queue stays readable until a restart
    assert open_q[USER_ID][0] == "Alice"


def test_legacy_state_without_the_queue_key_still_loads(bot):
    """The queue was added later; a state file written before it must not brick the bot."""
    bot.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    bot.STATE_FILE.write_text('{"relay": {"5": 42}, "acked": [42]}', encoding="utf-8")

    relay, acked, open_q = bot._load()

    assert relay == {5: 42} and acked == {42} and open_q == {}


def test_start_sends_the_card_with_link_buttons(bot, state):
    bot._dispatch(user_msg("/start"), USER_ID, *state)

    methods = [m for m, _ in bot.fake.to(USER_ID)]
    assert methods == ["sendPhoto"]
    _, params = bot.fake.to(USER_ID)[0]
    assert params["photo"].endswith("/static/og.png")
    assert len(params["caption"]) <= 1024  # Telegram truncates a longer caption

    buttons = [b for row in json.loads(params["reply_markup"])["inline_keyboard"] for b in row]
    # every button is an ordinary link to the site: selling inside Telegram is a locked non-goal
    # (the private key is generated in the customer's BROWSER, so a chat-app checkout has nowhere
    # to put key handling). callback_data would be the first step towards one.
    assert all(b["url"].startswith("https://privacyvpn.example.com/") for b in buttons)
    assert not any("callback_data" in b for b in buttons)
    # .onion is not a button on purpose: Telegram's in-app browser cannot resolve it, so the
    # button would be dead. It stays copyable text in /links.
    assert not any(".onion" in b["url"] for b in buttons)


def test_start_falls_back_to_text_when_the_photo_cannot_be_fetched(bot, state):
    """Telegram pulls the image from our own domain. If that fetch fails, first contact must not
    be answered with silence — the relay's defining failure mode."""
    real = bot.fake

    def flaky(method: str, **params):
        if method == "sendPhoto":
            real.calls.append((method, params))
            return {"ok": False, "description": "wrong file identifier"}
        return real(method, **params)

    bot._api = flaky
    bot._dispatch(user_msg("/start"), USER_ID, *state)

    methods = [m for m, _ in bot.fake.to(USER_ID)]
    assert methods == ["sendPhoto", "sendMessage"]
    _, fallback = bot.fake.to(USER_ID)[-1]
    assert "private key" in fallback["text"]
    assert json.loads(fallback["reply_markup"])["inline_keyboard"]  # buttons survive the fallback


def test_start_does_not_promise_what_we_cannot_deliver(bot, state):
    """The competitor card this was modelled on sells 'YouTube, Instagram, blocked sites' and
    Russian cards. Every one of those is off-limits here: no live RU test (posts.md stop-list),
    no RU servers, and cards mean KYC. A regression in this copy is a public overclaim."""
    bot._dispatch(user_msg("/start"), USER_ID, *state)
    text = bot.fake.texts_to(USER_ID).lower()

    for forbidden in ("youtube", "instagram", "blocked", "россия", "russian card", "card"):
        assert forbidden not in text, f"welcome claims {forbidden!r}"


def test_gift_answers_the_holder_and_names_the_expensive_mistake(bot, state):
    """/gift is the answer the operator would otherwise retype for every marketplace buyer.

    The one thing it must not omit: log in BEFORE redeeming. Redeeming while logged out mints a
    second account with a new number, and the customer then re-imports the config on every device.
    """
    bot._dispatch(user_msg("/gift"), USER_ID, *state)

    text = bot.fake.texts_to(USER_ID)
    assert "https://privacyvpn.example.com/redeem" in text
    assert "TT-" in text
    assert "Log in FIRST" in text
    assert "second account" in text
    # a code cannot be restored — saying otherwise would promise support something it cannot do
    assert "cannot restore" in text
    assert not bot.fake.to(OPERATOR_ID), "a hint command must not be relayed to the operator"


def test_start_card_offers_the_redeem_page(bot, state):
    """A gift-code holder who lands in the bot instead of the site must find the door here too."""
    bot._dispatch(user_msg("/start"), USER_ID, *state)

    _, params = bot.fake.to(USER_ID)[0]
    urls = [b["url"] for row in json.loads(params["reply_markup"])["inline_keyboard"] for b in row]
    assert "https://privacyvpn.example.com/redeem" in urls


def test_the_command_menu_is_published_and_lists_gift(bot, state):
    """Without setMyCommands the menu next to the input field stays whatever was set by hand at
    BotFather years ago — /gift would work only for someone who already knows it exists."""
    bot._register_commands()

    calls = [(m, p) for m, p in bot.fake.calls if m == "setMyCommands"]
    assert calls, "the command menu was never published"
    public = json.loads(calls[0][1]["commands"])
    names = [c["command"] for c in public]
    assert "gift" in names and "start" in names
    # an operator tool must not be advertised in every customer's menu
    assert "queue" not in names
    assert "scope" not in calls[0][1], "the public menu must be the default scope"


def test_the_operator_gets_the_queue_command_scoped_to_their_chat(bot, state):
    bot._register_commands()

    scoped = [p for m, p in bot.fake.calls if m == "setMyCommands" and "scope" in p]
    assert len(scoped) == 1
    assert json.loads(scoped[0]["scope"]) == {"type": "chat", "chat_id": OPERATOR_ID}
    assert "queue" in [c["command"] for c in json.loads(scoped[0]["commands"])]
