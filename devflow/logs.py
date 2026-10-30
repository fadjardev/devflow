"""Logging for the bot and the MCP server.

Two files under runtime/: `<name>.log` holds everything, `<name>-errors.log`
only warnings and errors, so a failure is never buried under routine lines.
Every line carries the task it belongs to (`[261002-01]`, or `[-]` outside a
task) via a context variable, which asyncio jobs and `asyncio.to_thread`
inherit. Uncaught exceptions in the main thread, other threads and the event
loop are logged instead of vanishing.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import logging.handlers
import re
import sys
import threading
from pathlib import Path
from types import TracebackType
from typing import Any

from . import redact
from .config import RUNTIME_DIR

task_id: contextvars.ContextVar[str] = contextvars.ContextVar("devflow_task", default="-")

FORMAT = "%(asctime)s %(levelname)-7s [%(task)s] %(name)s: %(message)s"
_ENTRY = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
_NOISY = ("httpx", "httpcore", "telegram", "apscheduler", "mcp")

log = logging.getLogger("devflow")


class _TaskTag(logging.Filter):
    """On the handler, not the logger, so third-party records get the tag too."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.task = task_id.get()
        return True


def path(name: str = "devflow", errors: bool = False) -> Path:
    return RUNTIME_DIR / (f"{name}-errors.log" if errors else f"{name}.log")


def setup(name: str = "devflow", console: bool = True, rotate: bool = True) -> None:
    """Configure the root logger. `rotate=False` for processes that run several at once
    (the MCP server): Windows cannot rename a log another process holds open."""
    RUNTIME_DIR.mkdir(exist_ok=True)

    def file(p: Path, keep: int) -> logging.Handler:
        if rotate:
            return logging.handlers.RotatingFileHandler(p, maxBytes=5_000_000, backupCount=keep, encoding="utf-8")
        return logging.FileHandler(p, encoding="utf-8")

    errors = file(path(name, errors=True), 3)
    errors.setLevel(logging.WARNING)
    handlers = [file(path(name), 5), errors]
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    redact.add_env()
    fmt, tag = redact.Formatter(FORMAT), _TaskTag()
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(tag)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(logging.INFO)
    for noisy in _NOISY:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def uncaught(kind: type[BaseException], value: BaseException, tb: TracebackType | None) -> None:
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, value, tb)
            return
        log.critical("uncaught exception", exc_info=(kind, value, tb))

    def thread_uncaught(args: threading.ExceptHookArgs) -> None:
        exc = args.exc_value or args.exc_type()
        log.error("uncaught exception in thread %s", args.thread.name if args.thread else "?",
                  exc_info=(args.exc_type, exc, args.exc_traceback))

    sys.excepthook = uncaught
    threading.excepthook = thread_uncaught


def watch_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Log what asyncio would otherwise print once, at garbage collection, or not at all
    (e.g. "Task exception was never retrieved")."""

    def handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        log.error("asyncio: %s", context.get("message", "unhandled error"),
                  exc_info=(type(exc), exc, exc.__traceback__) if exc else None)

    loop.set_exception_handler(handler)


def entries(text: str) -> list[str]:
    """Split log text into entries: a timestamped line plus its traceback lines."""
    out: list[str] = []
    for line in text.splitlines():
        if _ENTRY.match(line) or not out:
            out.append(line)
        else:
            out[-1] += "\n" + line
    return out


def tail(p: Path, count: int = 5, task: str = "", max_bytes: int = 2_000_000) -> list[str]:
    """The last `count` entries of a log, optionally only those tagged with `task`."""
    if not p.is_file():
        return []
    with open(p, "rb") as f:
        f.seek(max(0, p.stat().st_size - max_bytes))
        text = f.read().decode("utf-8", "replace")
    found = entries(text)
    if task:
        found = [e for e in found if f"[{task}]" in e.split("\n", 1)[0]]
    return found[-count:]


def roll(name: str, max_bytes: int = 5_000_000, keep: int = 3) -> None:
    """Rotate a log by hand, for writers that can't rotate it themselves. Skipped while
    another process holds the file open (Windows refuses the rename)."""
    p = path(name)
    if not p.is_file() or p.stat().st_size < max_bytes:
        return
    try:
        for i in range(keep - 1, 0, -1):
            older = p.with_name(f"{p.name}.{i}")
            if older.exists():
                older.replace(p.with_name(f"{p.name}.{i + 1}"))
        p.replace(p.with_name(f"{p.name}.1"))
    except OSError as e:
        log.warning("could not rotate %s: %s", p.name, e)
