# 13. Speak both opencode server APIs

Date: 2026-11-01 · Status: accepted

## Context

opencode 2.x (`@opencode/cli`) replaced the `opencode serve` API. devflow's
worker client was written for 1.x (`opencode-ai`), and against a 2.x server
every call failed (`POST /session` answered 405; `GET /path` answered the web
app's HTML with 200, so the health check passed). The two generations can't
both be installed as `opencode`, and people move to 2.x for their own use.

Checked live against opencode 2.2.5:

- routes live under `/api`; HTTP Basic auth (`opencode:<password>`) on every
  request; `OPENCODE_PASSWORD` sets the password and is then not printed;
- the directory is the session's `location: {directory}`, the model a
  `Model.Ref {providerID, id}` set with `POST /api/session/{id}/model`;
- `POST /api/session/{id}/prompt` only admits the input; the run ends with an
  `idle` message (`outcome` succeeded, failed or interrupted, with a structured
  `error {type, message, status, retryAfterMs}` on failure);
- permissions are `{action, resource, effect}`, last match wins; `bash` is now
  `shell`, `task` is `subagent`, `doom_loop` is gone, `execute` and image
  generation are new, and **`external_directory` is allowed by default**;
- per-session rules held against the built-in agent's allow-all: with a real
  model, `git status`, reading `.env` and reading a file outside the worktree
  were refused, writing inside it was allowed, and nothing was left asking;
- a session's folder stays locked on Windows until the location is evicted
  (`DELETE /api/debug/location?location[directory]=…`);
- config moved from `provider`/`npm`/`options` to
  `providers`/`package`/`settings`; `OPENCODE_CONFIG` still works.

## Decision

The client probes the server (`GET /api/info` → v2, `GET /path` returning
JSON → v1) and speaks that generation; before starting a server it reads
`opencode --version` to write the matching config. One ruleset is still
built (`opencode.ruleset`) and translated for v2 (`ruleset_v2`), which also
denies `execute` and image generation. devflow starts a v2 server with a
password of its own, kept owner-only in `<runtime>/opencode.password`, so a
restarted devflow can still use the server it left running; a v2 server that
asks for another password was started outside devflow and is reported, not
retried.

## Consequences

- Both opencode 1.x and 2.x work; nothing changes for 1.x users.
- The v2 health check needs the password, so a stray server on the port is
  named in the error instead of silently passing the check.
- Shell commands in `worker_shell_allow` that run code can read the server's
  environment, password included, and so reach the API, as they could reach
  the password-less v1 API before. Keep that list to commands that don't run
  project code (docs/security.md).
- If opencode changes the v2 API again, `tests/test_opencode_v2.py` describes
  the shapes devflow relies on.
