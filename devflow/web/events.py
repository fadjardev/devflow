"""Notifications for the dashboard: what Telegram would have been sent, kept in memory."""

from __future__ import annotations

import datetime as dt
import itertools
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .. import redact
from ..tasks import ID_RE

KEEP = 300


@dataclass(frozen=True)
class Event:
    n: int
    ts: dt.datetime
    text: str
    task: str = ""          # the task id the message is about, when it starts with one
    photo: str = ""         # a screenshot's file name in runtime/shots


def _task_of(text: str) -> str:
    for word in text.split()[:3]:
        word = word.strip("`*:,.")
        if ID_RE.fullmatch(word):
            return word
    return ""


class WebNotifier:
    """A Flow notifier that never fails: the last KEEP messages, newest last."""

    def __init__(self) -> None:
        self.events: deque[Event] = deque(maxlen=KEEP)
        self._n = itertools.count(1)

    async def text(self, message: str) -> None:
        text = redact.redact(message)
        self.events.append(Event(next(self._n), dt.datetime.now(), text, _task_of(text)))

    async def photo(self, path: Path, caption: str) -> None:
        text = redact.redact(caption)
        self.events.append(Event(next(self._n), dt.datetime.now(), text, _task_of(text), path.name))

    def recent(self, count: int = 30, task: str = "") -> list[Event]:
        found = [e for e in self.events if not task or e.task == task]
        return list(reversed(found[-count:]))
