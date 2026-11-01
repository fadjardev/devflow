"""What every front end shares: one Flow, the notifiers it reports to, and the background loops.

The Telegram bot and the local dashboard (ADR 0011) both drive the same `Flow`. Either can
run alone, or both in one process and one event loop, each getting every notification.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .config import Settings
from .pipeline import Flow, Notifier

log = logging.getLogger("devflow")


class FanoutNotifier:
    """Sends to every notifier. One that fails is logged and does not stop the others."""

    def __init__(self, *targets: Notifier):
        self.targets: list[Notifier] = list(targets)

    def add(self, target: Notifier) -> None:
        self.targets.append(target)

    async def text(self, message: str) -> None:
        for t in self.targets:
            try:
                await t.text(message)
            except Exception:
                log.exception("notifier %s failed: %s", type(t).__name__, message[:200])

    async def photo(self, path: Path, caption: str) -> None:
        for t in self.targets:
            try:
                await t.photo(path, caption)
            except Exception:
                log.exception("notifier %s failed to send %s", type(t).__name__, path.name)


async def resume_loop(flow: Flow) -> None:
    """Every minute: resume tasks whose provider limit has reset (limits.auto_resume)."""
    while True:
        await asyncio.sleep(60)
        try:
            await flow.resume_due()
        except Exception:
            log.exception("auto-resume check failed")


class Runtime:
    """The Flow and its loops, started once whichever front ends are on."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.notifier = FanoutNotifier()
        self.flow = Flow(settings, self.notifier)
        self._loops: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        await self.flow.recover()
        if self.settings.budget.auto_resume:
            self._loops.append(asyncio.create_task(resume_loop(self.flow)))

    async def stop(self) -> None:
        for t in self._loops:
            t.cancel()
        await self.flow.close()
