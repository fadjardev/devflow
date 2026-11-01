# devflow: notes for coding agents

devflow is a supervised AI coding workflow: Claude Code plans and reviews,
opencode writes the code, a human approves at two gates. Front ends: a
Telegram bot and a local web dashboard, both driving one `Flow`. Read
`README.md` for what it does, `docs/architecture.md` (section 9 for the latest
modules) for how, and `docs/security.md` before touching anything listed under
*Safety changes* in `CONTRIBUTING.md`.

## Commands

The venv was made by uv; its `.exe` launchers can fail with "uv trampoline
failed", so call tools through the interpreter:

```sh
.venv/Scripts/python.exe -m pytest -q          # ~95 s; tests/test_web.py and test_gateway.py are fast
.venv/Scripts/ruff.exe check .
.venv/Scripts/python.exe -m mypy               # strict for every module listed in pyproject.toml
.venv/Scripts/python.exe -m devflow --config <file> doctor
.venv/Scripts/python.exe -m devflow --config <file> dashboard [--url]
```

All three checks must pass before you call a change done. New modules go into
the strict mypy list in `pyproject.toml`. After changing dependencies,
regenerate the lock with the command at the top of `requirements.lock`.

## Where things are

| Change | Look at |
| --- | --- |
| A phase (plan, code, verify, review, close) | `devflow/pipeline.py` (`Flow`), prompts in `devflow/prompts/` |
| A setting | `devflow/config.py` (TOML tables are typo-checked by `_Table.done()`), `devflow.example.toml`, README "Configuration" |
| Telegram | `devflow/bot.py` (thin: parse, call `Flow`, reply) |
| Dashboard | `devflow/web/server.py` (routes), `web/auth.py` (Guard), `web/templates/`, `web/static/app.css` |
| Startup shared by both front ends | `devflow/runtime.py` |
| Worker models / AI gateway | `devflow/opencode.py` (`config_file`, `prompt`), `devflow/gateway.py` |
| Usage and budgets | `devflow/usage/` |
| Task notes and states | `devflow/tasks.py`, `devflow/vault.py` |

## Rules

- **Safety is enforced by code, not by prompt text.** Never loosen an
  allow-list, ruleset, gate or dashboard check for convenience. A change that
  tightens one needs a test that shows the blocked action is refused
  (`tests/test_security.py`, `tests/test_web.py`).
- **Front ends stay thin.** Anything that changes a task goes through a `Flow`
  method, so Telegram and the dashboard can't drift apart. Add shared read
  helpers to `Flow` (like `at_gate`, `running`), not to one front end.
- **Never write a secret to a file.** Keys are referenced by environment
  variable name (`{env:NAME}`); anything shown to a user goes through
  `redact.redact`.
- **Task notes:** an agent's text inside a section must not use `## `
  headings; they start a new section. Specs use `###`.
- **Docs move with the code:** README section, `devflow.example.toml`,
  CHANGELOG (Unreleased), and an ADR in `docs/adr/` for a design decision.
- Code, comments, docs and commit messages are in English. Commits follow
  Conventional Commits.

## Tests

Tests never touch your real config: `tests/conftest.py` points the runtime
and the config at `tmp_path`. Agents are faked (`FakeOpencode` in
`tests/test_pipeline.py`); a fake's `prompt()` must accept
`model_name=None`. Dashboard tests use Starlette's `TestClient` with
`base_url="http://127.0.0.1:8765"`, or the Host check refuses them.
