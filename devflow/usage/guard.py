"""Spending: records every agent run, enforces budgets and provider limits, and reports.

The pipeline calls `before` at each phase boundary (a phase is never stopped
midway) and `after` each agent run. Everything that reaches the human goes
through the notifier it was given; repeated warnings are deduplicated in
SQLite so a restart doesn't send them again.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from ..config import BudgetConfig
from . import Limit, Usage, budget
from .db import Row, UsageDB

log = logging.getLogger("devflow.usage")

Clock = Callable[[], dt.datetime]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Stopped(Exception):
    """A task must stop at this phase boundary: `reason` is "budget" or "limit"."""

    def __init__(self, reason: str, message: str, resume_after: dt.datetime | None = None):
        super().__init__(message)
        self.reason, self.resume_after = reason, resume_after


def money(cost: float | None) -> str:
    return "cost not reported" if cost is None else f"${cost:.2f}"


def tokens(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.0f}k" if n >= 1000 else str(n)


class Spending:
    def __init__(self, cfg: BudgetConfig, db: UsageDB, notify: Callable[[str], Awaitable[None]],
                 clock: Clock = utc_now):
        self.cfg, self.db, self.notify, self.now = cfg, db, notify, clock

    # -- provider limits --------------------------------------------------------------------

    def active_limit(self, backend: str) -> Limit | None:
        """A limit that still holds: hit, and its reset time not reached (or unknown)."""
        now = self.now()
        for lim in self.db.limits():
            if lim.backend == backend and lim.hit and (lim.resets_at is None or lim.resets_at > now):
                return lim
        return None

    def _limit_text(self, lim: Limit) -> str:
        return f"{lim.backend}: {lim.reason}, {budget.resets_text(self.cfg, lim.resets_at, self.now())}"

    # -- before and after a run -------------------------------------------------------------

    def before(self, backend: str, task: str | None = None, extra_rounds: int = 0) -> None:
        """Raise Stopped if this run must not start: a provider limit on its backend, the
        budget used up with on_exceed = stop, or the task's own cap reached."""
        lim = self.active_limit(backend)
        if lim:
            raise Stopped("limit", f"waiting for the {self._limit_text(lim)}", lim.resets_at)
        if task is None:
            return
        if self.cfg.on_exceed == "stop":
            over = budget.exceeded(self.cfg, self.db, self.now())
            if over:
                m = over[0]
                # PRD §5.7: on_exceed = stop parks running tasks as a limit, resumable at the reset.
                raise Stopped("limit", f"{m.label} used up ({m.amount()}), "
                                        f"{budget.resets_text(self.cfg, m.resets, self.now())}", m.resets)
        cap = budget.task_meter(self.cfg, self.db, task, extra_rounds)
        if cap and cap.fraction >= 1:
            raise Stopped("budget", f"this task's budget is used up ({cap.amount()}); "
                                    f"/retry {task} allows one more {money(self.cfg.caps.per_task_usd)}")

    async def after(self, task: str, role: str, backend: str, session: str, total: Usage, seconds: float,
                    ok: bool, limit: Limit | None) -> Usage:
        """Record the run (the session's growth, for a cumulative total), then react to a
        limit and to budget thresholds. Raises Stopped when a provider limit was hit."""
        run = self.db.delta(backend, session, total) if session else total
        outcome = "limit" if limit and limit.hit else "ok" if ok else "error"
        self.db.record(Row(self.now(), task, role, backend, run.model, run, seconds, outcome))
        log.info("usage %s %s %s: %s tokens (in %d, out %d, cache %d/%d), %s, %.0fs, %s", task, role, backend,
                 run.tokens, run.input, run.output, run.cache_read, run.cache_write, money(run.cost_usd),
                 seconds, outcome)
        for message in budget.crossed(self.cfg, self.db, self.now()):
            await self.notify(message)
        if limit and not limit.hit:
            self.db.set_limit(limit, self.now())  # shown by /limits
            await self._early_warning(limit)
        if limit and limit.hit:
            self.db.set_limit(limit, self.now())
            await self.notify(f"🛑 {self._limit_text(limit)}. {task} stopped"
                              + ("; it resumes on its own after the reset." if self.cfg.auto_resume and limit.resets_at
                                 else f"; /retry {task} once it has reset." if not task.startswith("ask:") else "."))
            raise Stopped("limit", self._limit_text(limit), limit.resets_at)
        return run

    async def _early_warning(self, lim: Limit) -> None:
        if lim.utilization is None:
            return
        passed = [t for t in self.cfg.warn_at if lim.utilization >= t]
        if not passed:
            return
        window = lim.resets_at.isoformat() if lim.resets_at else "unknown"
        if self.db.first_time(f"limit:{lim.backend}:{window}:{max(passed)}", self.now()):
            await self.notify(f"⚠️ {lim.backend} usage limit {lim.utilization:.0%} used · "
                              f"{budget.resets_text(self.cfg, lim.resets_at, self.now())}")

    # -- new work ---------------------------------------------------------------------------

    def refuse_new(self) -> str:
        """Why a new /task or /ask can't start now, or ""."""
        lim = self.active_limit("claude")
        if lim:
            return f"⏸ Not now: {self._limit_text(lim)}."
        if self.cfg.on_exceed in ("pause-new", "stop"):
            over = budget.exceeded(self.cfg, self.db, self.now())
            if over:
                m = over[0]
                return (f"⏸ Paused: the {m.label} is used up ({m.amount()}), "
                        f"{budget.resets_text(self.cfg, m.resets, self.now())}. See /limits.")
        return ""

    def due(self) -> bool:
        """Clear provider limits whose reset time has passed; True if any did."""
        now, cleared = self.now(), False
        for lim in self.db.limits():
            if lim.resets_at is not None and lim.resets_at <= now:
                self.db.clear_limit(lim.backend)
                cleared = True
        return cleared

    # -- reports ----------------------------------------------------------------------------

    def task_total(self, task: str) -> Usage:
        rows = self.db.rows(task=task)
        costs = [r.usage.cost_usd for r in rows if r.usage.cost_usd is not None]
        return Usage(sum(r.usage.input for r in rows), sum(r.usage.output for r in rows),
                     sum(r.usage.cache_read for r in rows), sum(r.usage.cache_write for r in rows),
                     sum(costs) if costs else None)

    def footer(self, task: str) -> str:
        total = self.task_total(task)
        if not total.tokens:
            return ""
        return f"\n\n💰 so far: {tokens(total.tokens)} tokens · {money(total.cost_usd)}"

    def usage_report(self, arg: str = "") -> str:
        arg = arg.strip().lower() or "today"
        now = self.now()
        if arg not in ("today", "week", "month"):
            rows = self.db.rows(task=arg)
            if not rows:
                return f"No usage recorded for {arg}."
            lines = [f"**Usage of {arg}**"]
            for r in rows:
                lines.append(f"- {r.ts.astimezone(budget.tz(self.cfg)):%m-%d %H:%M} {r.role} ({r.backend} "
                             f"{r.model or '?'}): {tokens(r.usage.tokens)} tokens, {money(r.usage.cost_usd)}"
                             + ("" if r.outcome == "ok" else f", {r.outcome}"))
            total = self.task_total(arg)
            lines.append(f"**Total**: {tokens(total.tokens)} tokens, {money(total.cost_usd)}")
            return "\n".join(lines)
        spans = budget.windows(self.cfg, now)
        since = {"today": spans["day"][0], "month": spans["month"][0],
                 "week": spans["day"][0] - dt.timedelta(days=6)}[arg]
        rows = self.db.rows(since=since)
        if not rows:
            return f"No usage recorded {'today' if arg == 'today' else 'this ' + arg}."
        lines = [f"**Usage {'today' if arg == 'today' else 'in the last 7 days' if arg == 'week' else 'this month'}**"]
        for title, key in (("By backend", "backend"), ("By role", "role"), ("By model", "model")):
            groups: dict[str, list[Row]] = defaultdict(list)
            for r in rows:
                groups[getattr(r, key) or "?"].append(r)
            lines.append(f"\n{title}:")
            for name, rs in sorted(groups.items(), key=lambda kv: -sum(x.usage.cost_usd or 0 for x in kv[1])):
                cost = [x.usage.cost_usd for x in rs if x.usage.cost_usd is not None]
                lines.append(f"- {name}: {tokens(sum(x.usage.tokens for x in rs))} tokens, "
                             f"{money(sum(cost) if cost else None)}, {len(rs)} runs")
        per_task: dict[str, float] = defaultdict(float)
        for r in rows:
            per_task[r.task] += r.usage.cost_usd or 0
        top = sorted(per_task.items(), key=lambda kv: -kv[1])[:5]
        lines.append("\nTop tasks:")
        lines += [f"- {task}: ${cost:.2f}" for task, cost in top]
        return "\n".join(lines)

    def limits_report(self) -> str:
        now = self.now()
        lines = ["**Budgets**"]
        ms = budget.meters(self.cfg, self.db, now)
        if not ms and self.cfg.caps.per_task_usd is None:
            lines.append("- none configured ([budget] in devflow.toml)")
        for m in ms:
            lines.append(f"- {m.label}: {m.amount()} ({m.fraction:.0%}) · "
                         f"{budget.resets_text(self.cfg, m.resets, now)}")
        if self.cfg.caps.per_task_usd is not None:
            lines.append(f"- per task: {money(self.cfg.caps.per_task_usd)}")
        lines.append(f"- when used up: {self.cfg.on_exceed}")
        lines.append("\n**Provider limits**")
        known = self.db.limits()
        if not known:
            lines.append("- none reported")
        for lim in known:
            state = "⛔ hit" if lim.hit and (lim.resets_at is None or lim.resets_at > now) else \
                "✅ reset" if lim.hit else f"⚠️ {lim.utilization:.0%} used" if lim.utilization is not None else "ok"
            lines.append(f"- {lim.backend}: {state} · {budget.resets_text(self.cfg, lim.resets_at, now)}")
        lines.append(f"- auto-resume after a limit: {'on' if self.cfg.auto_resume else 'off'}")
        return "\n".join(lines)
