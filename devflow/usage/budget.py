"""Budgets: how much of each cap is used, which thresholds were crossed, what to refuse.

Windows use the budget's time zone: a day starts at 00:00 and a month on day 1.
`now` is always passed in, so tests drive time with a fake clock.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo
from dataclasses import dataclass

from ..config import BudgetConfig, Caps
from .db import UsageDB

BACKENDS = ("claude", "opencode")


@dataclass(frozen=True)
class Meter:
    window: str           # day, month, task
    unit: str             # usd, tokens
    backend: str | None   # None: all backends together
    used: float
    cap: float
    starts: dt.datetime
    resets: dt.datetime | None  # None for a task's cap

    @property
    def fraction(self) -> float:
        return self.used / self.cap if self.cap else 0.0

    @property
    def label(self) -> str:
        who = f"{self.backend} " if self.backend else ""
        return f"{who}{'daily' if self.window == 'day' else 'monthly' if self.window == 'month' else 'task'} " \
               f"{'budget' if self.unit == 'usd' else 'token budget'}"

    def amount(self) -> str:
        if self.unit == "usd":
            return f"${self.used:.2f} / ${self.cap:.2f}"
        return f"{self.used:,.0f} / {self.cap:,.0f} tokens"


def tz(cfg: BudgetConfig) -> dt.tzinfo:
    if cfg.timezone:
        return zoneinfo.ZoneInfo(cfg.timezone)
    local = dt.datetime.now().astimezone().tzinfo
    return local or dt.UTC


def windows(cfg: BudgetConfig, now: dt.datetime) -> dict[str, tuple[dt.datetime, dt.datetime]]:
    local = now.astimezone(tz(cfg))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    next_month = (month + dt.timedelta(days=32)).replace(day=1)
    # Add a day on the calendar, not 24 hours, so a DST change doesn't move midnight.
    next_day = (day + dt.timedelta(days=1)).replace(hour=0)
    return {"day": (day, next_day), "month": (month, next_month)}


def _sum(db: UsageDB, since: dt.datetime, backend: str | None, task: str | None = None) -> tuple[float, int]:
    rows = [r for r in db.rows(since=since, task=task) if backend is None or r.backend == backend]
    return sum(r.usage.cost_usd or 0.0 for r in rows), sum(r.usage.tokens for r in rows)


def meters(cfg: BudgetConfig, db: UsageDB, now: dt.datetime) -> list[Meter]:
    """Every configured daily and monthly cap with its current use."""
    found = []
    spans = windows(cfg, now)
    scopes: list[tuple[str | None, Caps]] = [(None, cfg.caps)] + [(b, c) for b, c in cfg.backends.items()]
    for backend, caps in scopes:
        for window, usd, tokens in (("day", caps.daily_usd, caps.daily_tokens),
                                    ("month", caps.monthly_usd, caps.monthly_tokens)):
            if usd is None and tokens is None:
                continue
            starts, resets = spans[window]
            cost, toks = _sum(db, starts, backend)
            if usd is not None:
                found.append(Meter(window, "usd", backend, cost, usd, starts, resets))
            if tokens is not None:
                found.append(Meter(window, "tokens", backend, toks, tokens, starts, resets))
    return found


def task_meter(cfg: BudgetConfig, db: UsageDB, task: str, extra_rounds: int = 0) -> Meter | None:
    """The per-task cap. Each /retry after the cap was hit allows one more cap's worth."""
    cap = cfg.caps.per_task_usd
    if cap is None:
        return None
    cost, _ = _sum(db, dt.datetime.min.replace(tzinfo=dt.UTC), None, task)
    return Meter("task", "usd", None, cost, cap * (1 + extra_rounds), dt.datetime.min.replace(tzinfo=dt.UTC), None)


def exceeded(cfg: BudgetConfig, db: UsageDB, now: dt.datetime) -> list[Meter]:
    return [m for m in meters(cfg, db, now) if m.fraction >= 1]


def crossed(cfg: BudgetConfig, db: UsageDB, now: dt.datetime) -> list[str]:
    """Warnings for thresholds crossed since last time: one message per meter, threshold and
    window, ever (the dedup lives in the database, so a restart doesn't repeat one)."""
    out = []
    for m in meters(cfg, db, now):
        passed = [t for t in [*cfg.warn_at, 1.0] if m.fraction >= t]
        if not passed:
            continue
        top = max(passed)
        key = f"budget:{m.backend or 'all'}:{m.window}:{m.unit}:{m.starts.date().isoformat()}:{top}"
        lower = [f"budget:{m.backend or 'all'}:{m.window}:{m.unit}:{m.starts.date().isoformat()}:{t}" for t in passed]
        if not db.first_time(key, now):
            continue
        for k in lower:  # a jump straight past 50% and 80% reports only the highest
            db.first_time(k, now)
        out.append(threshold_message(cfg, m, top, now))
    return out


def resets_text(cfg: BudgetConfig, when: dt.datetime | None, now: dt.datetime) -> str:
    if when is None:
        return "reset time unknown"
    local = when.astimezone(tz(cfg))
    soon = dt.timedelta(0) <= when - now <= dt.timedelta(hours=24)  # within a day: the time says enough
    return f"resets {local:%H:%M}" if soon else f"resets {local:%d %b %H:%M}"


def threshold_message(cfg: BudgetConfig, m: Meter, top: float, now: dt.datetime) -> str:
    if top >= 1.0:
        action = {"warn": "nothing is paused (on_exceed = warn)",
                  "pause-new": "new /task and /ask are paused; running tasks finish",
                  "stop": "new work is paused and running tasks stop at their next step"}[cfg.on_exceed]
        return f"⛔ {m.label} used up ({m.amount()}): {action} · {resets_text(cfg, m.resets, now)}"
    return f"⚠️ {top:.0%} of the {m.label} used ({m.amount()}) · {resets_text(cfg, m.resets, now)}"
