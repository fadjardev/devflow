# 3. The opencode worker gets no devflow-mcp

Date: 2026-10-30 · Status: accepted

## Context

devflow-mcp must be bound to one task id and one role per agent run (PRD SR6,
finding S5): an agent working on task A must not be able to read or write task
B, and the worker must not be able to rewrite the approved Spec.

Claude runs as one `claude -p` process per run, so each run can get its own MCP
config with its task id and role in the environment.

opencode runs as **one** `opencode serve` process for every session. Its MCP
servers are configured once, when the server starts (`OPENCODE_CONFIG`), and
are shared by all sessions. There is no per-session MCP environment, so the
server cannot know which task a call comes from. A capability token in the
prompt would be readable by a hijacked worker's own session, but anything the
server can't tie to the session is guesswork.

The worker used devflow-mcp for two things: appending to *Worker log* and
blocking itself (`task_update_state(to="blocked")`).

## Decision

The worker gets no devflow-mcp at all.

- devflow already stores the worker's final reply in *Worker log*; that stays
  the only Worker log entry.
- The worker blocks itself by ending its reply with a line
  `BLOCKED: <why>`. devflow parses it and makes the transition as the bot.
- `prompts/worker.md` says so.

## Consequences

- The worker cannot touch any task note, so S5 is closed for the worker by
  construction rather than by checks.
- A worker that writes `BLOCKED:` in the middle of a normal reply is treated as
  blocked; only a line that starts with it counts, which keeps this unlikely.
