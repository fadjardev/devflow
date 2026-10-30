"""SQLite at <runtime>/devflow.db: one row per agent run, sent notifications, provider limits.

No prompt or response text is stored, only counts, costs and ids.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..config import runtime_dir
from . import Limit, Usage

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,            -- UTC, ISO 8601
    task TEXT NOT NULL,          -- task id, or ask:<project>
    role TEXT NOT NULL,          -- planner, worker, reviewer, closer, asker
    backend TEXT NOT NULL,       -- claude, opencode
    model TEXT NOT NULL,
    input INTEGER NOT NULL, output INTEGER NOT NULL,
    cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL,
    cost_usd REAL,               -- NULL when the backend reported none
    duration_s REAL NOT NULL,
    outcome TEXT NOT NULL        -- ok, error, limit
);
CREATE INDEX IF NOT EXISTS usage_ts ON usage (ts);
CREATE INDEX IF NOT EXISTS usage_task ON usage (task);
CREATE TABLE IF NOT EXISTS sessions (  -- a session's last reported totals, for resumed runs
    backend TEXT NOT NULL, session TEXT NOT NULL,
    input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER, cost_usd REAL,
    PRIMARY KEY (backend, session)
);
CREATE TABLE IF NOT EXISTS notifications (key TEXT PRIMARY KEY, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS limits (
    backend TEXT PRIMARY KEY, hit INTEGER NOT NULL, reason TEXT NOT NULL,
    resets_at TEXT, utilization REAL, ts TEXT NOT NULL
);
"""
VERSION = 1


@dataclass(frozen=True)
class Row:
    ts: dt.datetime
    task: str
    role: str
    backend: str
    model: str
    usage: Usage
    duration_s: float
    outcome: str


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat(timespec="seconds")


def _time(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s) if s else None


class UsageDB:
    def __init__(self, path: Path | None = None):
        self.path = path or runtime_dir() / "devflow.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)
            db.execute(f"PRAGMA user_version = {VERSION}")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    # -- runs ------------------------------------------------------------------------------

    def delta(self, backend: str, session: str, total: Usage) -> Usage:
        """What this run added to a session whose `total` the backend reports cumulatively."""
        if not session:
            return total
        with self._db() as db:
            row = db.execute("SELECT input, output, cache_read, cache_write, cost_usd FROM sessions "
                             "WHERE backend = ? AND session = ?", (backend, session)).fetchone()
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (backend, session, total.input, total.output, total.cache_read, total.cache_write,
                        total.cost_usd))
        if row is None:
            return total
        return total - Usage(row[0], row[1], row[2], row[3], row[4])

    def record(self, row: Row) -> None:
        u = row.usage
        with self._db() as db:
            db.execute("INSERT INTO usage (ts, task, role, backend, model, input, output, cache_read, cache_write,"
                       " cost_usd, duration_s, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (_iso(row.ts), row.task, row.role, row.backend, row.model or u.model, u.input, u.output,
                        u.cache_read, u.cache_write, u.cost_usd, round(row.duration_s, 1), row.outcome))

    def rows(self, since: dt.datetime | None = None, task: str | None = None) -> list[Row]:
        sql, args = "SELECT ts, task, role, backend, model, input, output, cache_read, cache_write, cost_usd, " \
                    "duration_s, outcome FROM usage WHERE 1 = 1", []
        if since is not None:
            sql += " AND ts >= ?"
            args.append(_iso(since))
        if task is not None:
            sql += " AND task = ?"
            args.append(task)
        with self._db() as db:
            found = db.execute(sql + " ORDER BY ts, id", args).fetchall()
        return [Row(dt.datetime.fromisoformat(r[0]), r[1], r[2], r[3], r[4],
                    Usage(r[5], r[6], r[7], r[8], r[9], r[4]), r[10], r[11]) for r in found]

    # -- notifications, sent once ----------------------------------------------------------

    def first_time(self, key: str, now: dt.datetime) -> bool:
        """True the first time `key` is seen, ever (survives restarts)."""
        with self._db() as db:
            cur = db.execute("INSERT OR IGNORE INTO notifications VALUES (?, ?)", (key, _iso(now)))
            return cur.rowcount == 1

    # -- provider limits -------------------------------------------------------------------

    def set_limit(self, limit: Limit, now: dt.datetime) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO limits VALUES (?, ?, ?, ?, ?, ?)",
                       (limit.backend, int(limit.hit), limit.reason,
                        _iso(limit.resets_at) if limit.resets_at else None, limit.utilization, _iso(now)))

    def clear_limit(self, backend: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM limits WHERE backend = ?", (backend,))

    def limits(self) -> list[Limit]:
        with self._db() as db:
            found = db.execute("SELECT backend, hit, reason, resets_at, utilization FROM limits").fetchall()
        return [Limit(r[0], bool(r[1]), r[2], _time(r[3]), r[4]) for r in found]
