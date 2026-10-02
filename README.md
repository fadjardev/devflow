# devflow

Claude Code plans and reviews, opencode codes, you approve from Telegram.

**The full documentation lives in the vault:**
`1-project-knowledge/03-Notes/devflow - AI Dev Workflow.md`
(`[[devflow - AI Dev Workflow]]` in Obsidian). This README covers only running it.

## Run

```bat
cd E:\dev\projects\devflow
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m playwright install chromium-headless-shell
copy .env.example .env          & rem then fill in TELEGRAM_TOKEN and ALLOWED_CHAT_ID
.venv\Scripts\python -m devflow doctor
.venv\Scripts\python -m devflow
```

Python 3.10+ is required (the `mcp` SDK); devcrate's `python\current` is 3.8,
so the venv is built on the system Python.

## Layout

| File | Does |
| --- | --- |
| `devflow/bot.py` | Telegram commands, chat-id allow-list, `/ask` follow-ups by reply |
| `devflow/pipeline.py` | the phases: plan → gate 1 → code → verify → review → gate 2 → close; `/ask` answers |
| `devflow/logs.py` | logging setup: `runtime/*.log`, each line tagged with its task; backs `/log` |
| `devflow/sync.py` | fetch, base branch, other unmerged branches, merge simulation |
| `devflow/tasks.py` | task notes, the state machine, project-note writes |
| `devflow/vault.py` | safe, atomic, locked note writes; Obsidian-tolerant frontmatter |
| `devflow/mcp_server.py` | devflow-mcp — the agents' only write path into the vault |
| `devflow/claude.py` | `claude -p` as planner / reviewer / closer / asker; secret-file deny rules |
| `devflow/opencode.py` | `opencode serve` client, permission ruleset + watchdog |
| `devflow/worktree.py` | one git worktree + branch per task |
| `devflow/vhost.py` | temporary devcrate nginx vhost per task |
| `devflow/verify.py` | HTTP checks, `php -l`, phpunit, headless screenshot |
| `prompts/` | the agent prompts: plan, worker, review, close, ask |

## Logs

`runtime/devflow.log` (everything) and `runtime/devflow-errors.log` (warnings and
errors) for the bot; `runtime/mcp.log` and `runtime/mcp-errors.log` for devflow-mcp;
`runtime/opencode-serve.log` for the opencode server. From Telegram: `/log`, `/log <id>`.

## Test

```bat
.venv\Scripts\python -m pytest -q
```
