# Contributing to devflow

Thanks for taking an interest. devflow started as one developer's tool, so the
most useful contributions right now are the ones that make it work on setups
other than the original one (Windows, PHP behind nginx, an Obsidian vault).
They also need to keep its safety model intact.

## Before you start

- **Bugs:** open an issue with what you ran, what you expected, and what
  happened. Attach the relevant lines from `runtime\devflow-errors.log` (or
  `/log <id>`), with secrets and private code removed.
- **Features and larger changes:** open an issue first and describe the
  problem before writing code. Something that fits one setup may not fit the
  general case. Agreeing on the shape up front saves you from rewriting it.
- **Security problems:** please **don't** open a public issue. Report them
  privately through GitHub's *Report a vulnerability* button on the
  repository's Security tab. [SECURITY.md](SECURITY.md) says what counts and
  what to expect.

Everyone taking part is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

You need Python 3.11+ and git. Claude Code, opencode, nginx and a Telegram bot
are **not** needed to run the tests, because the agents are faked.

```bat
git clone https://github.com/fadjardev/devflow.git
cd devflow
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check devflow tests
.venv\Scripts\python -m mypy
```

On Linux or macOS, use `.venv/bin/python` instead. CI runs the suite on
Windows, Linux and macOS. OS-specific code belongs in `devflow/platform.py`.
The devcrate vhost provider is Windows-only by nature.

To run the bot itself, follow [Setup](README.md#setup) in the README.

## Making a change

1. Fork the repository and create a branch off the latest `main` (see
   [Branches](#branches)).
2. Keep the change focused: one fix or feature per pull request.
3. **Add or update tests.** Pipeline behaviour is tested end to end with faked
   agents against real git repositories (`tests/test_pipeline.py`); follow
   that pattern rather than mocking git.
4. Run the tests, ruff and mypy (see above) and make sure they pass. CI runs
   the same checks on Windows, Linux and macOS.
5. Update the README when you change a command, a setting (also in
   `.env.example`), a requirement, or a known limit, and add a line under
   *Unreleased* in [CHANGELOG.md](CHANGELOG.md) for anything a user would
   notice.

### Branches

`main` is the only long-lived branch. It must always pass the tests, and
releases are tagged from it. Every change, including the maintainer's, is made
on a short-lived branch and merged into `main` through a pull request.

Name branches `<type>/<short-description>`, with the same types as
[commit messages](#commit-messages):

| Branch | For |
| --- | --- |
| `feat/inline-approve-buttons` | a new feature |
| `fix/worktree-locked-on-close` | a bug fix |
| `docs/contributing-guide` | documentation only |
| `refactor/vhost-provider` | restructuring without a behaviour change |
| `test/mcp-path-traversal`, `ci/github-actions`, `chore/…` | tests, CI, maintenance |

- Use lowercase words joined by hyphens. Add the issue number when there is one
  (`fix/42-stale-fetch`).
- Before opening a pull request, rebase onto `main` (`git fetch origin` then
  `git rebase origin/main`). Don't merge `main` into your branch.
- Pull requests are squash-merged or rebased, so `main` stays linear. The
  branch is deleted after the merge.
- Don't use the `devflow/` prefix for your own branches. devflow creates its
  task branches under that prefix (`BRANCH_PREFIX`).

### Code style

Match the surrounding code. The existing style is:

- type hints on public functions, and `from __future__ import annotations`
  at the top of each module;
- a module docstring that says what the module is for and why;
- comments only where the *why* isn't obvious from the code;
- `logging` (with the task id context from `devflow.logs`) instead of `print`;
- new modules pass `mypy --strict`: add them to the strict list in
  `pyproject.toml`.

### Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):
`<type>(<optional scope>): <summary>`, for example
`fix(worktree): exclude only linked paths git would stage`.

- `type` is one of `feat`, `fix`, `docs`, `style`, `refactor`, `perf`,
  `test`, `build`, `ci`, `chore`.
- The scope is usually the module name: `bot`, `pipeline`, `worktree`,
  `verify` and so on.
- Write the summary in the imperative mood, with no trailing period, in about
  70 characters.
- Use the body to explain *why* the change is needed. The diff already shows
  what changed.

### Safety changes

devflow lets agents work unattended, so some areas need extra care. A pull
request that touches any of these must say so in its description and explain
why the change is safe:

- the Claude allow-lists and deny rules (`devflow/claude.py`);
- the opencode permission ruleset and watchdog (`devflow/opencode.py`);
- the devflow-mcp tools, path checks and state transitions
  (`devflow/mcp_server.py`, `devflow/vault.py`, `devflow/tasks.py`);
- the Telegram chat allow-list (`devflow/bot.py`);
- anything that runs git, or links or removes files in a worktree
  (`devflow/worktree.py`, `devflow/sync.py`).

The rule is that safety is **enforced by tooling, not by prompt text**. Don't
loosen a restriction for convenience, and don't move a rule from code into a
prompt. A change that tightens a restriction should come with a test showing
that the blocked action is refused. The [Safety model](README.md#safety-model)
lists the gaps that are already known.

### Prompts

The agent prompts live in `devflow/prompts/`. The pipeline parses some of their output
(`TITLE:`, `VERDICT: APPROVE|FIXUP`, the `devflow-checks` block). If you change
a prompt, keep those markers, and check the parsing tests still pass.

## Pull requests

- Describe what the change does and why, and how you tested it.
- Link the issue it addresses.
- Expect review comments. Small, focused pull requests get merged faster.
