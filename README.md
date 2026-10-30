# devflow

**A supervised AI coding workflow you drive from Telegram.** Claude Code plans
and reviews, [opencode](https://opencode.ai) writes the code on a cheaper model,
and you approve twice from your phone: once for the plan, once for the diff.
Every task ends as a git branch you merge yourself, with evidence that the change
works.

```text
/task shop add sorting to the orders table
  → Claude reads the code and writes a spec          → you: /approve   (gate 1)
  → opencode implements it in an isolated worktree
  → devflow checks it: HTTP before/after, lint, tests, screenshot, merge simulation
  → Claude reviews the diff, sends fix-ups back if needed
  → you see the diff stat, evidence and screenshot   → you: /approve   (gate 2)
  → branch kept, worktree removed, decision recorded. You merge.
```

You can also just **ask** about a project from Telegram (`/ask shop where is
checkout validated?`): Claude reads the code and your notes and answers, changing
nothing.

> **Status: personal tool, shared as-is.** devflow was built for one developer's
> setup: Windows, PHP web apps served by a local nginx, and an
> [Obsidian](https://obsidian.md) vault as the task tracker. The workflow ideas
> are general; parts of the code are not (see [Requirements](#requirements) and
> [Known limits](#known-limits)). Issues and forks are welcome.

## Why

- **Cost.** Writing code is most of the tokens; judgment is in the plan and the
  review. A strong model does the judgment, a cheaper one does the typing, and
  the strong model still reads every diff.
- **Control.** Agents work unattended, but nothing they do counts until you
  approve it. devflow itself never merges, never pushes, and never touches your
  base branch. The agents are restricted, not sandboxed (see
  [Safety model](#safety-model)).
- **Evidence over claims.** devflow verifies the change itself rather than
  trusting the agent's report.
- **Away from the desk.** Start, steer, approve, or stop work from your phone.

## How it works

```mermaid
flowchart LR
    you([You on Telegram]) <--> bot[devflow bot]
    bot --> note[(task note in the vault)]
    bot --> planner[Claude: plan]
    planner -- spec --> note
    bot -->|gate 1 /approve| wt[git worktree + temporary vhost]
    bot --> worker[opencode: code]
    worker --> wt
    wt --> verify[verify: HTTP checks, php -l, phpunit, screenshot]
    verify --> reviewer[Claude: review]
    reviewer -- FIXUP --> worker
    reviewer -- APPROVE --> bot
    bot -->|gate 2 /approve| done[branch kept, worktree removed]
    planner & worker & reviewer -. writes .-> mcp[devflow-mcp] -.-> note
    bot -->|/ask| asker[Claude: answer, read-only]
```

| Part | Role |
| --- | --- |
| **Telegram bot** | Your commands in, notifications out. Answers only one allowed chat id |
| **Pipeline** | Moves each task from phase to phase until the next human gate |
| **Claude Code** (`claude -p`) | Planner, reviewer, closer, and the read-only answerer for `/ask`. Never edits project files |
| **opencode** (`opencode serve`) | Worker: implements the spec inside its own git worktree |
| **devflow-mcp** | A small MCP server that is the agents' *only* way to write to the vault, with its rules enforced in code |
| **Verifier** | HTTP checks, `php -l`, phpunit, headless screenshot, `git merge-tree` |

### A task's life

1. **`/task <project> <request>`** logs the request and creates a task note in
   state `new`. Task ids look like `YYMMDD-NN`.
2. **Plan.** Claude reads the request, the project's docs and code (read-only),
   and writes a spec: goal, cause (for bugs, with `file:line`), per-file changes,
   what not to touch, acceptance checks, and a conventional-commit title. If it
   needs a human decision, it asks, and you reply with `/answer`.
3. **Gate 1: `/approve <id>`.** devflow fetches, creates a git worktree on a new
   branch `devflow/<type>/<id>-<slug>` from the freshly fetched base branch, links
   in the gitignored files the app needs to run, and serves the worktree on a
   temporary vhost.
4. **Code.** opencode implements the spec; devflow commits the result.
5. **Verify.** devflow gathers evidence itself (see [Verification](#verification)).
6. **Review.** Claude reads the spec, the diff, and the evidence, then answers
   `APPROVE` or `FIXUP` with a fix-up spec that goes back to the worker. After
   `MAX_BOUNCES` fix-ups the task is parked for you.
7. **Gate 2: `/approve <id>`.** You get the diff stat, the evidence and the
   screenshot (`/diff` for the full patch). The merge is simulated again; if the
   base branch has moved and now conflicts, approval is refused until you
   `/refresh`. On approval the worktree and vhost are removed, the **branch is
   kept**, and Claude records anything worth remembering in the project note.
8. **You merge**, through a pull request or however your team works.

## Telegram commands

| Command | Does |
| --- | --- |
| `/task <project> <request>` | New task. `<project>` is the exact name or any unique part of it |
| `/ask <project> <question>` · `/ask all <question>` | Read-only question about one project or all of them. **Reply to an answer** to follow up in the same conversation |
| `/status` | Active tasks and which ones wait at a gate |
| `/show <id>` | Title, branch, vhost, spec, last review, recent history |
| `/approve <id>` | Gate 1 (plan → coding) or gate 2 (review → done) |
| `/reject <id> <why>` | Gate 1: back to the planner. Gate 2: back to the worker as a fix-up |
| `/answer <id> <answer>` | Same as `/reject`; reads better when the planner asked a question |
| `/diff <id>` | The full diff as a `.diff` file |
| `/retry <id>` | Resume a blocked task in the phase where it stopped |
| `/refresh <id>` | Catch a task up with the latest base branch, then verify and review again |
| `/stop <id>` | Cancel: stop the worker, remove worktree and vhost, keep the branch |
| `/log` · `/log <n>` · `/log <id>` | Recent warnings and errors, or one task's log lines |
| `/help` | The list |

Messages from any other chat are ignored without a reply.

## Safety model

> **Read this before pointing devflow at code you don't fully trust.** The
> agents run as your own user account, with no OS-level sandbox. The rules below
> stop an agent that is behaving normally. An agent that has been steered by
> malicious text it read (prompt injection from a file, commit message, branch
> or web page) can get around several of them. Run devflow only on
> repositories you trust, on a machine or user account whose credentials
> you're willing to expose to the worker.

Prompts alone are not a security boundary, so devflow configures each tool's
permission system as tightly as it can. Here is what that does and doesn't
cover today.

- **Claude** runs headless with `--permission-mode dontAsk` and a per-role
  allow-list: read, search, read-only git subcommands, and the devflow-mcp tools
  that role needs. Write, Edit, `git push/commit/checkout`, and the Read tool on
  secret files (`DENY_READ`) are denied.
  *Not yet covered:* the git rules match by prefix, so some arguments of
  `git diff`/`git log` (`--no-index`, `--output`) may still read or write files
  outside these rules.
- **opencode** runs with a per-session ruleset. Its file tools can't read
  `DENY_READ` files or reach outside the worktree, and a list of history- and
  branch-changing `git` commands is denied. A watchdog rejects any permission
  prompt that would otherwise hang with nobody at the terminal, and tells you on
  Telegram. Each worker run is capped at 45 minutes.
  *Not yet covered:* the worker's **shell is otherwise unrestricted**. Shell
  commands can read secret files, reach the network, write outside the
  worktree (including the main checkout's `.git` hooks and config), and get
  around the git denylist. If a non-interactive git credential is configured,
  that includes pushing. Folders linked into the worktree (`LINK_PATHS`) point at
  your main checkout, so writes inside them land there and don't appear in the
  diff.
- **devflow-mcp** exposes five narrow tools (read a task, change its state along
  the allowed transitions, append to a section, add a decision, update a project
  note). File names come from validated ids and existing project names, never
  from free text, and every path must stay inside the task, request, or project
  folders of the vault. Agents can't take a human gate.
  *Not yet covered:* the tools aren't scoped to the agent's own task or role, so
  any agent, the worker included, can write to other tasks and to project
  notes. That includes a task's spec after you approved it at gate 1.
- **Git**: devflow's own git operations commit only on its `devflow/...`
  branches. devflow never merges, pushes, or force-pushes, and never changes
  your base branch or your main checkout's working tree. (This covers what
  devflow does, not what a misbehaving worker's shell could do; see above.)
- **Telegram**: only the allowed chat is answered. If that chat is a group,
  every member can issue every command. Specs, diffs, `/ask` answers and
  `/log` excerpts go through Telegram's servers.

Also not protected: a secret that was ever committed is still visible through
read-only `git log -p`. Code and data the agents read go to whichever model
providers you configure. Tests and checks run the worker's code as your user.

Closing these gaps (deny-by-default worker shell, git hook protection, scoped
MCP tools, user-level allow-list, log redaction, and an optional worker
sandbox) is planned work.

## Verification

devflow collects evidence itself instead of trusting the worker's account:

- **HTTP checks.** The planner writes a `devflow-checks` JSON block into the spec:

  ````text
  ```devflow-checks
  [{"method": "GET", "path": "/orders", "expect_status": 200, "contains": "Orders"}]
  ```
  ````

  Fields: `path`, `method`, `expect_status`, `contains`, `absent`, `headers`,
  `data`. Each check runs against the main checkout (**before**) and the task's
  worktree (**after**); only "after" decides pass or fail.
- **`php -l`** on every changed PHP file, with the PHP version the project's
  vhost uses.
- **phpunit**, when the project has `vendor/bin/phpunit` and a `phpunit.xml`.
- **Screenshot** of the first GET check, taken with headless Chromium (Playwright)
  and sent with the gate-2 message.
- **Merge simulation** against the base branch with `git merge-tree`, plus a list
  of other unmerged branches that touch the same files.

## Working alongside a team

devflow assumes other people push to the same repository:

- Before planning, it fetches and tells the planner which other unmerged branches
  were active in the last `OVERLAP_DAYS` and which files they change, and warns
  you when the spec touches one of them.
- Worktrees start from the **fetched** base branch, not from whatever your local
  checkout happens to be on.
- The merge is simulated at verification and again at gate 2.
- `/refresh` rebases a branch that was never pushed, and merges the base branch
  into one that was, so no force-push is ever needed. Conflicts go back to the
  worker; devflow refuses to commit while conflict markers remain.

All git calls run with prompts disabled, so `git fetch` needs a non-interactive
credential (for example a token in Git Credential Manager). Without one, devflow
keeps working on the last-fetched refs and says so in every message.

## Requirements

- **Windows.** Worktrees link folders with directory junctions, and the
  temporary vhosts drive a local `nginx.exe`.
- **Python 3.11+.**
- **[Claude Code](https://claude.com/claude-code)** (`claude` on `PATH`, logged in)
  and **[opencode](https://opencode.ai)** (`opencode` on `PATH`, with a provider
  configured), plus **git**.
- **A Telegram bot**: create one with [@BotFather](https://t.me/BotFather); get your
  numeric chat id from [@userinfobot](https://t.me/userinfobot).
- **A notes vault** (plain Markdown; [Obsidian](https://obsidian.md) works well)
  with this layout:

  ```text
  <vault>/
    01-Projects/<project>.md   one note per project; devflow knows only these projects
    04-Requests/                raw requests, written by devflow
    05-Tasks/                   task notes, written by devflow
  ```

  A project's folder is `PROJECTS_PATH/<project>` unless its note's frontmatter
  sets `path:`. Tasks need it to be a git checkout; `/ask` does not.
- **Optional, for HTTP checks, linting and screenshots:** a local nginx serving
  each project from `<nginx prefix>/conf/sites/*.conf` with PHP over FastCGI, laid
  out like [devcrate](https://github.com/fadjardev/devcrate-stack), the portable
  Windows PHP stack devflow was built beside. Projects without a vhost still go
  through plan, code, review, and merge simulation; they just skip the HTTP
  checks and the screenshot.

## Setup

```bat
git clone https://github.com/fadjardev/devflow.git
cd devflow
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m playwright install chromium-headless-shell
copy .env.example .env
```

Fill in `.env` (see below), then:

```bat
.venv\Scripts\python -m devflow doctor
.venv\Scripts\python -m devflow
```

`doctor` checks the token, the vault, the tools on `PATH`, nginx, and Playwright,
then lists each project with the vhost and PHP version devflow will use. The
second command starts the bot; it greets you with 🟢 on Telegram. Keep the window
open, or run it as a scheduled task at logon.

devflow-mcp is wired into each agent run automatically. To use its tools in your
own interactive Claude Code sessions as well (optional):

```bat
claude mcp add -s user -e DEVFLOW_VAULT=C:\path\to\vault devflow -- C:\path\to\devflow\.venv\Scripts\python.exe -m devflow.mcp_server
```

## Configuration

All settings live in `.env`; [`.env.example`](.env.example) documents each one.

| Setting | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_TOKEN`, `ALLOWED_CHAT_ID` | — | The bot, and the only chat it answers |
| `VAULT_PATH` | — | The notes vault |
| `PROJECTS_PATH` | — | Where project checkouts live; worktrees go in `.worktrees\` under it |
| `DEVCRATE_ROOT` | — | Root of the local nginx/PHP stack (`nginx\`, `php\php-X.Y\`) |
| `MAX_WORKERS` | `3` | Tasks coding at once (one per project) |
| `MAX_BOUNCES` | `2` | Reviewer fix-ups before a task is parked for you |
| `OPENCODE_URL` | `http://127.0.0.1:4096` | devflow starts `opencode serve` here if nothing answers |
| `OPENCODE_MODEL` | opencode's default | `provider/model` as listed by `opencode models` |
| `CLAUDE_MODEL` | your default | e.g. `sonnet`, to plan and review more cheaply |
| `BASE_BRANCH`, `GIT_REMOTE` | `development`, `origin` | Tasks start from `<remote>/<base>` |
| `BRANCH_PREFIX` | `devflow` | Branches are `<prefix>/<type>/<id>-<slug>` |
| `OVERLAP_DAYS` | `14` | How far back other unmerged branches count as active |
| `LINK_PATHS` | see example | Gitignored files and folders a worktree needs to run |
| `DENY_READ` | see example | Files no agent may read (globs relative to the project) |

## Logs

Everything is logged under `runtime\`, every line tagged with its task id:

| File | Holds |
| --- | --- |
| `devflow.log` | Everything the bot does: commands, phases and their durations, state changes, agent costs, commits, verification |
| `devflow-errors.log` | Warnings and errors only, with tracebacks |
| `mcp.log`, `mcp-errors.log` | devflow-mcp tool calls, refusals and crashes |
| `opencode-serve.log` | Output of the `opencode serve` process devflow started |

From Telegram, `/log` shows recent problems and `/log <id>` one task's story. A
phase that fails parks its task as `blocked` and tells you which phase; fix the
cause and `/retry`.

## Known limits

- **Pages behind a login** can't be checked automatically yet; HTTP checks and
  screenshots see the login page. The planner names what you must check by hand.
- **"Before" means your main checkout as it is now**, uncommitted changes and
  current branch included.
- **A bot restart** parks tasks that were mid-phase as `blocked`; `/retry` resumes
  them. Tasks waiting at a gate are unaffected. `/ask` follow-ups are kept in
  memory and lost on restart.
- **Telegram limits**: messages are capped at 4096 characters, so long specs and
  diffs are clipped (use `/show`, `/diff`, or the note). Formatting is a small
  Markdown subset; tables and links arrive as plain text.
- **Opening a temporary vhost in your own browser** needs a hosts-file entry;
  devflow doesn't add one. Its own checks don't need it.
- **Cost**: Claude runs on your Claude Code account. A `/ask` answer costs roughly
  $0.30 on the default model; set `CLAUDE_MODEL` to spend less.

## Project layout

| Path | Does |
| --- | --- |
| `devflow/bot.py` | Telegram commands, chat-id allow-list, `/ask` follow-ups by reply |
| `devflow/pipeline.py` | The phases: plan → gate 1 → code → verify → review → gate 2 → close; `/ask` |
| `devflow/claude.py` | `claude -p` in each role, with its allow-list and secret-file deny rules |
| `devflow/opencode.py` | `opencode serve` client, permission ruleset, and watchdog |
| `devflow/mcp_server.py` | devflow-mcp, the agents' only write path into the vault |
| `devflow/tasks.py` | Task notes and the state machine |
| `devflow/vault.py` | Safe, atomic, locked note writes; Obsidian-tolerant frontmatter |
| `devflow/projects.py` | Project lookup by name, its checkout, and the vhost that serves it |
| `devflow/worktree.py` | One git worktree and branch per task |
| `devflow/vhost.py` | A temporary nginx vhost per task |
| `devflow/verify.py` | HTTP checks, `php -l`, phpunit, headless screenshot |
| `devflow/sync.py` | Fetch, base branch, other unmerged branches, merge simulation |
| `devflow/fmt.py` | Agent Markdown → Telegram HTML, split without breaking code blocks |
| `devflow/logs.py` | Logging setup behind `runtime\*.log` and `/log` |
| `prompts/` | The agent prompts: plan, worker, review, close, ask |

## Tests

```bat
.venv\Scripts\python -m pytest -q
```

The suite runs the whole pipeline with faked agents against real git
repositories, including a colleague pushing concurrently, conflicts and
`/refresh`, `/stop`, restart recovery, and a failing phase being parked.

## Contributing

Bug reports, fixes and work that makes devflow run on more setups are welcome.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, code and
commit conventions, and the extra care needed around the safety model.
Please report security problems privately, not in a public issue.

## License

[MIT](LICENSE)
