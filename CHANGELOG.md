# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: a minor version may break
things, and the changelog says how).

## [Unreleased]

### Added

- CI on Windows, Linux and macOS with Python 3.11 and the latest release:
  ruff, mypy and the test suite.
- `LICENSE` (MIT), `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`,
  issue and pull request templates.

- Security scanning in CI (gitleaks, pip-audit, Semgrep) and Dependabot.
- `ALLOWED_USER_IDS`, `WORKER_SHELL_ALLOW`, `UNSAFE_SHELL`, `CHECK_ENV_ALLOW`,
  `BEFORE_UNSAFE_METHODS` and `SHARE_CODE_VIA_TELEGRAM` settings.
- `python -m devflow doctor --fix` makes `.env` and the runtime folder private.
- [docs/security.md](docs/security.md): what is enforced and what isn't.
- `devflow.toml` with a `[projects.<name>]` registry and per-project overrides
  ([devflow.example.toml](devflow.example.toml)); the `devflow` command with
  `run`, `init`, `migrate-env` and `doctor`.
- Runs on Linux and macOS (symlinks, process groups).
- Works without a vault: devflow keeps its notes in its runtime folder.
- Per-project lint and test commands (`[checks]`), with a `php` preset that
  reproduces the old behaviour and adds pest.
- The `devflow-checks` block is validated against a schema.

### Changed

- Python 3.11 or newer is required (was 3.10).
- **Breaking:** without a `devflow.toml`, `VAULT_PATH` and `PROJECTS_PATH`
  must be set; nothing falls back to built-in paths. `DEVCRATE_ROOT` has no
  default, and without it HTTP checks are skipped.
- An empty `base_branch` (the TOML default) follows the remote's default
  branch.
- The vhost code moved behind a provider interface (`none`, `devcrate`).
- The prompts ship inside the package (`devflow/prompts/`).
- **Breaking:** the worker's shell runs only the commands in
  `WORKER_SHELL_ALLOW` (default `php -l *`); git is always refused.
- **Breaking:** Claude has no shell. It reads git through devflow-mcp's `git`
  tool. Custom prompts that ran git in a shell must use the tool.
- **Breaking:** the worker gets no devflow-mcp. It gives up by ending its reply
  with `BLOCKED: <why>`; its reply is the worker log.
- Each Claude run's devflow-mcp is bound to its own task and role. The spec
  can't change after gate 1; project notes are closer-only and append-only,
  and the close message shows what was added.
- Claude reads a copy of the project's notes instead of the whole vault.
- devflow refuses to start for a group chat without `ALLOWED_USER_IDS`.
- Logs and Telegram messages mask secrets.
- Lint and test commands run with a minimal environment.
- POST/PUT/DELETE checks no longer run against your own checkout by default.
- Note locks moved from the temp folder into the runtime folder.

### Fixed

- Removing a worktree with a linked folder failed on Python 3.11.
- Security review findings S1-S10 and S12 (PRD §7.2): agents could run any
  shell command, plant git hooks that ran on the next commit, write through
  linked folders unnoticed, write files with `git log --output`, write other
  tasks' notes and the approved spec, aim HTTP checks at other hosts, and see
  secrets in logs; group members could command the bot.

## 0.1.0

The first version, used by its author on Windows: Telegram bot, plan and review
by Claude Code, coding by opencode in git worktrees, task notes in an Obsidian
vault.
