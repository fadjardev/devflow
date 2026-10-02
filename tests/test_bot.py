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
