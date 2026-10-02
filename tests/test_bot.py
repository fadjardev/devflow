import dataclasses
import re

from devflow import bot, config


def test_every_command_is_in_the_help_and_the_menu():
    settings = dataclasses.replace(config.load(), telegram_token="123:abc", allowed_chat_id=1)
    app = bot.build(settings)
    commands = {c for group in app.handlers.values() for h in group if hasattr(h, "commands") for c in h.commands}
    assert commands - set(re.findall(r"/(\w+)", bot.HELP)) <= {"start", "help"}
    assert commands - {name for name, _ in bot.MENU} <= {"start"}


def test_help_is_valid_telegram_html():
    tags = re.findall(r"</?([a-z]+)>", bot.HELP)
    assert set(tags) <= {"b", "i"}  # the tags Telegram's HTML mode accepts that we use
    for t in set(tags):
        assert bot.HELP.count(f"<{t}>") == bot.HELP.count(f"</{t}>")
    assert "&" not in re.sub(r"&(amp|lt|gt);", "", bot.HELP)  # a bare & breaks parsing


def test_replies_retry_a_timeout_but_not_a_bad_request(monkeypatch):
    import asyncio

    import pytest
    from telegram.error import BadRequest, TimedOut

    real_sleep = asyncio.sleep
    monkeypatch.setattr(bot.asyncio, "sleep", lambda s: real_sleep(0))

    class Msg:
        def __init__(self, errors):
            self.errors, self.sent = list(errors), []

        async def reply_text(self, text, parse_mode=None):
            if self.errors:
                raise self.errors.pop(0)
            self.sent.append(text)
            return text

    class Update:
        def __init__(self, msg):
            self.effective_message = msg

    flaky = Msg([TimedOut("Timed out")])
    assert asyncio.run(bot._reply(Update(flaky), "hello")) == ["hello"]

    refused = Msg([BadRequest("Message to reply not found")] * 3)
    with pytest.raises(BadRequest):
        asyncio.run(bot._reply(Update(refused), "hello"))
    assert len(refused.errors) == 1  # tried as HTML, then plain text; never retried on top

    assert bot._transient(TimedOut("x")) and not bot._transient(BadRequest("x"))
