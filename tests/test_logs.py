import asyncio
import logging

import httpx
import pytest
from telegram.error import NetworkError

from devflow import bot, config, logs, opencode

log = logging.getLogger("devflow.test")


def read(p):
    for h in logging.getLogger().handlers:
        h.flush()
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_lines_carry_their_task_and_errors_go_to_both_files(logdir):
    async def job(task):
        logs.task_id.set(task)
        await asyncio.sleep(0)
        log.info("working")
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("failed")

    async def main():
        await asyncio.gather(asyncio.create_task(job("261002-01")), asyncio.create_task(job("261002-02")))
        log.info("outside")

    asyncio.run(main())
    everything, errors = read(logdir / "devflow.log"), read(logdir / "devflow-errors.log")
    assert "[261002-01] devflow.test: working" in everything
    assert "[261002-02] devflow.test: working" in everything
    assert "[-] devflow.test: outside" in everything
    assert "working" not in errors and "outside" not in errors
    assert "[261002-01] devflow.test: failed" in errors and "ValueError: boom" in errors


def test_tail_keeps_tracebacks_with_their_entry_and_filters_by_task(logdir):
    log.warning("first")
    logs.task_id.set("261002-07")
    try:
        1 / 0
    except ZeroDivisionError:
        log.exception("division")
    logs.task_id.set("-")
    log.error("last")
    read(logdir / "devflow.log")

    found = logs.tail(logs.path(errors=True), 2)
    assert len(found) == 2
    assert found[0].splitlines()[0].endswith("division") and "ZeroDivisionError" in found[0]
    assert found[1].endswith("last")
    only = logs.tail(logs.path(), 10, task="261002-07")
    assert len(only) == 1 and "division" in only[0]
    assert logs.tail(logdir / "missing.log") == []


def test_uncaught_loop_and_thread_errors_are_logged(logdir):
    import threading

    t = threading.Thread(target=lambda: 1 / 0, name="worker-x")
    t.start()
    t.join()

    async def main():
        logs.watch_loop(asyncio.get_running_loop())
        asyncio.get_running_loop().call_exception_handler({"message": "lost task", "exception": KeyError("k")})

    asyncio.run(main())
    errors = read(logdir / "devflow-errors.log")
    assert "uncaught exception in thread worker-x" in errors and "ZeroDivisionError" in errors
    assert "asyncio: lost task" in errors and "KeyError" in errors


def test_roll_rotates_only_big_logs(logdir):
    p = logs.path("mcp")
    p.write_text("x" * 50, encoding="utf-8")
    logs.roll("mcp", max_bytes=100)
    assert p.exists()
    logs.roll("mcp", max_bytes=10)
    assert not p.exists() and p.with_name("mcp.log.1").read_text() == "x" * 50


def test_notifier_retries_once_then_drops_without_raising(logdir, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(bot.asyncio, "sleep", lambda s: real_sleep(0))
    sent = []

    class Bot:
        def __init__(self, fail):
            self.fail = fail

        async def send_message(self, chat, text):
            if self.fail:
                self.fail -= 1
                raise NetworkError("down")
            sent.append(text)

    class App:
        pass

    app = App()
    app.bot = Bot(fail=1)
    asyncio.run(bot.TelegramNotifier(app, 1).text("hello"))
    assert sent == ["hello"]

    app.bot = Bot(fail=5)
    asyncio.run(bot.TelegramNotifier(app, 1).text("lost"))  # must not raise
    assert sent == ["hello"]
    assert "Telegram send failed, message 'lost' dropped" in read(logdir / "devflow-errors.log")


def test_permission_watchdog_survives_a_failing_callback(logdir, tmp_path, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(opencode.asyncio, "sleep", lambda s: real_sleep(0))
    polls, replies = [], []

    def handle(request: httpx.Request):
        if request.method == "GET":
            polls.append(1)
            pending = [{"id": f"p{len(polls)}", "sessionID": "s1", "permission": "read", "patterns": [".env"]}]
            return httpx.Response(200, json=pending if len(polls) <= 2 else [])
        replies.append(request.url.path)
        return httpx.Response(200, json=True)

    async def denied(permission, patterns):
        raise RuntimeError("callback broke")

    async def main():
        client = opencode.Client(config.load())
        client.http = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handle))
        dog = asyncio.create_task(client._watch_permissions("s1", tmp_path, denied))
        while len(polls) < 4:
            await real_sleep(0)
        assert not dog.done()  # still watching after two callback failures
        dog.cancel()
        await client.http.aclose()

    asyncio.run(main())
    assert replies == ["/permission/p1/reply", "/permission/p2/reply"]
    assert read(logdir / "devflow-errors.log").count("could not reject permission request") == 2
