import asyncio

from telegram.error import BadRequest

from devflow import bot, fmt


def test_markdown_becomes_telegram_html():
    md = ("# Answer\n"
          "`Login` handles it: **see** `Login.php:47`.\n"
          "- first <tag> & more\n"
          "  * nested\n"
          "_asm · $0.30 · reply to follow up_\n"
          "```php\n$a = $b < 3 && **not bold**;\n```\n"
          "snake_case_name and a * b stay as they are")
    assert fmt.to_html(md) == (
        "<b>Answer</b>\n"
        "<code>Login</code> handles it: <b>see</b> <code>Login.php:47</code>.\n"
        "• first &lt;tag&gt; &amp; more\n"
        "  • nested\n"
        "<i>asm · $0.30 · reply to follow up</i>\n"
        "<pre>$a = $b &lt; 3 &amp;&amp; **not bold**;</pre>\n"
        "snake_case_name and a * b stay as they are")


def test_code_inside_backticks_is_not_bolded_or_unescaped():
    assert fmt.to_html("`**x** <y>`") == "<code>**x** &lt;y&gt;</code>"


def test_split_keeps_pieces_small_and_code_blocks_closed():
    md = "intro\n```\n" + "\n".join(f"line {i}" for i in range(400)) + "\n```\nafter"
    pieces = fmt.split(md, limit=500)
    assert len(pieces) > 3
    for p in pieces:
        assert len(p) <= 520
        assert p.count("```") % 2 == 0  # every piece's code block is closed
        assert fmt.to_html(p).count("<pre>") == fmt.to_html(p).count("</pre>")
    joined = "\n".join(pieces)
    assert "line 0" in joined and "line 399" in joined and joined.endswith("after")


def test_split_cuts_a_giant_line_and_drops_empty_pieces():
    assert [len(p) for p in fmt.split("x" * 1200, limit=500)] == [500, 500, 200]
    assert fmt.split("\n\n") == []


def test_rejected_formatting_falls_back_to_plain_text():
    sent = []

    async def send(text, mode):
        if mode:
            raise BadRequest("Can't parse entities")
        sent.append(text)
        return text

    assert asyncio.run(bot._deliver(send, "**odd** `text`")) == "**odd** `text`"
    assert sent == ["**odd** `text`"]


def test_log_entries_lose_the_long_timestamp():
    entry = "2026-10-02 15:06:10,800 WARNING [-] devflow: Telegram network error: Timed out"
    assert bot._compact(entry) == "10-02 15:06 WARNING [-] devflow: Telegram network error: Timed out"
