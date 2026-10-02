import asyncio
import dataclasses
import re
from pathlib import Path

import pytest

from devflow import bot, claude, config, pipeline
from devflow.pipeline import Flow, FlowError
from devflow.vault import VaultError


class Notes:
    async def text(self, message):
        pass

    async def photo(self, path, caption):
        pass


@pytest.fixture
def flow(vault, tmp_path, monkeypatch):
    checkout = tmp_path / "projects" / "demo.app"
    checkout.mkdir(parents=True)
    note = vault / "01-Projects" / "demo.app.md"
    text = re.sub(r"(?m)^path: .*$", lambda _: f'path: "{checkout.as_posix()}"', note.read_text(encoding="utf-8"))
    note.write_text(text, encoding="utf-8")
    settings = dataclasses.replace(config.load(), vault=vault, projects=tmp_path / "projects",
                                   deny_read=[".env", "**/*-key.pem"])
    calls = []

    async def fake_claude(s, prompt, cwd, tools, resume="", add_dirs=(), timeout=1800):
        calls.append({"prompt": prompt, "cwd": Path(cwd), "tools": tools, "resume": resume, "add_dirs": add_dirs})
        if "explode" in prompt:
            return claude.Result("API error", "", False)
        return claude.Result(f"answer {len(calls)}", f"sess-{len(calls)}", True, 0.12)

    monkeypatch.setattr(pipeline.claude, "run", fake_claude)
    f = Flow(settings, Notes())
    f.calls, f.checkout = calls, checkout
    return f


def test_question_runs_read_only_in_the_project_then_follows_up(flow):
    target = flow.ask_target("demo")
    text, follow = asyncio.run(flow.ask(target, "where is login handled?"))
    first = flow.calls[0]
    assert first["cwd"] == flow.checkout and first["tools"] is claude.ASKER_TOOLS and first["resume"] == ""
    assert "where is login handled?" in first["prompt"] and "project `demo.app`" in first["prompt"]
    assert flow.s.vault in first["add_dirs"]
    assert text.startswith("answer 1") and "demo.app · $0.12" in text and follow.session == "sess-1"

    text, again = asyncio.run(flow.ask(follow, "and logout?"))
    second = flow.calls[1]
    assert second["prompt"] == "and logout?" and second["resume"] == "sess-1" and second["cwd"] == flow.checkout
    assert again.session == "sess-2"


def test_all_projects_and_bad_input(flow):
    for word in ("all", "*", "ALL"):
        assert flow.ask_target(word).cwd == flow.s.projects
    with pytest.raises(VaultError):
        flow.ask_target("nosuchproject")
    with pytest.raises(FlowError):
        asyncio.run(flow.ask(flow.ask_target("demo"), "  "))
    with pytest.raises(RuntimeError, match="couldn't answer"):
        asyncio.run(flow.ask(flow.ask_target("demo"), "explode please"))


def test_asker_writes_nothing_and_secrets_are_denied(flow):
    writes = [t for t in claude.ASKER_TOOLS if "append" in t or "update" in t or "add" in t or t in ("Write", "Edit")]
    assert writes == []
    assert claude.secret_rules(flow.s) == ["Read(**/.env)", "Read(**/*-key.pem)"]


class Msg:
    _next = 100

    def __init__(self, text="", reply_to=None):
        self.text, self.reply_to_message, self.sent = text, reply_to, []
        Msg._next += 1
        self.message_id = Msg._next

    async def reply_text(self, text):
        m = Msg(text)
        self.sent.append(m)
        return m


class Update:
    def __init__(self, msg):
        self.effective_message = msg


class Ctx:
    def __init__(self, flow):
        self.application = type("App", (), {"bot_data": {"flow": flow}})()


def test_replying_to_an_answer_follows_up(flow):
    ctx = Ctx(flow)

    async def scenario():
        question = Msg("/ask demo where is login?")
        await bot._answer(Update(question), ctx, flow.ask_target("demo"), "where is login?")
        answer = question.sent[0]
        assert answer.text.startswith("answer 1")

        reply = Msg("and logout?", reply_to=answer)
        await bot.on_reply(Update(reply), ctx)
        await asyncio.gather(*ctx.application.bot_data["ask_jobs"])
        assert flow.calls[-1]["resume"] == "sess-1" and flow.calls[-1]["prompt"] == "and logout?"
        assert reply.sent[-1].text.startswith("answer 2")

        stray = Msg("hello", reply_to=Msg("some other message"))
        await bot.on_reply(Update(stray), ctx)
        assert "reply to an /ask answer" in stray.sent[-1].text

    asyncio.run(scenario())
