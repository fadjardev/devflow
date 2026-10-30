# 6. Usage lives in SQLite before tasks do

Date: 2026-10-30 · Status: accepted

## Context

Budgets need sums over time windows ("today's spend on Claude"), per task and
per role, and notifications that are sent once even across restarts. Task
state still lives in Markdown notes; moving it to SQLite is v0.3 work.

## Decision

Usage gets its own SQLite database now, `<runtime>/devflow.db`, with four
tables: `usage` (one row per agent run: time, task, role, backend, model,
input/output/cache-read/cache-write tokens, cost, duration, outcome),
`sessions` (the last cumulative total a session reported, so a resumed run
records only what it added), `notifications` (dedup keys) and `limits`
(provider limit state). The schema version is in `PRAGMA user_version`. No
prompt or response text is stored.

## Consequences

- v0.3 adds task tables to the same database; usage rows already carry task
  ids to join on.
- A cost the backend doesn't report is stored as NULL, never estimated.
