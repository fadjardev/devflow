# Security

devflow lets AI agents work on your code while you're away from the keyboard.
This page says what protects you, how it is enforced, and what it does **not**
protect against. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## The assumption

An agent will sooner or later read text written to steer it: a README, a
commit message, a branch name, a web page, a dependency's docs. devflow assumes
that happens and relies on what an agent **cannot** do, not on what its prompt
tells it. Every control below is enforced by code or by the agent tool's own
permission system, and each has a test in `tests/test_security.py` that
attempts the bypass.

## What is enforced

| Area | Control | Enforced by | Finding |
| --- | --- | --- | --- |
| Worker shell | Every command is denied unless `WORKER_SHELL_ALLOW` lists it; `git` is always denied, after the allow-list, so no entry reopens it | opencode session ruleset (last matching rule wins) | S1 |
| Worker tools | No `webfetch`, `websearch`, `codesearch`, and no subagents (`task`), which would run without the ruleset | opencode session ruleset | S1 |
| Worker files | File tools can't read `DENY_READ` paths or anything outside the worktree | opencode ruleset (`read`, `external_directory`) | — |
| devflow's git | Runs with an empty `core.hooksPath`, `core.fsmonitor=false`, no external diff, no pager, never prompting | `gitsafe.run` | S2 |
| `.git` tampering | Config (shared and per-worktree), hooks and `info/attributes` are fingerprinted when the worktree is made; a change blocks the task before devflow commits and refuses approval at gate 2 | `gitsafe.fingerprint`, `Flow._check_git` | S2 |
| Linked folders | Each `LINK_PATHS` folder is snapshotted; a write blocks the task and refuses approval | `worktree.snapshot` | S3 |
| Claude | No shell, no Write/Edit, no web; `DENY_READ` paths denied to Read/Grep/Glob | `claude -p --permission-mode dontAsk` allow/deny lists | S4 |
| Claude's git | Only through devflow-mcp's `git` tool: fixed subcommands, an option allow-list, paths inside the repository and outside `DENY_READ` (`rev:path` included) | `gitsafe.check_read_only` | S4 ([ADR 0004](adr/0004-claude-git-through-a-checked-tool.md)) |
| Task notes | Each Claude run gets its own devflow-mcp bound to one task and role; other tasks are refused; only the planner writes the Spec, and only before gate 1; agents can't take a gate | `mcp_server.Binding`, `tasks.TRANSITIONS`, `tasks.append` | S5 |
| Project notes | Only the closer writes them, append-only, at most 4000 characters per write; the close message shows what was added | `mcp_server` | S5 |
| Worker and notes | The worker has no devflow-mcp at all; it reports through its reply | `opencode.config_file` | S5 ([ADR 0003](adr/0003-worker-without-devflow-mcp.md)) |
| HTTP checks | `path` must be a plain `/...` path; the Host header is always the task's; POST/PUT/DELETE skip your checkout ("before") unless `BEFORE_UNSAFE_METHODS=true` | `verify.check_path`, `verify.http_checks` | S6 |
| Who commands | Only `ALLOWED_CHAT_ID`; in a group, only `ALLOWED_USER_IDS` (required); others get no reply | python-telegram-bot filters | S7 |
| Secrets in output | Logs (tracebacks included), Telegram messages and `/diff` are redacted: known token formats, secret-looking `NAME=value`, the bot token, secret env vars, values in each project's `DENY_READ` files | `redact.py` | S8 |
| Verification | Lint and test commands get a minimal environment (`CHECK_ENV_ALLOW` adds names), file names as full paths, a timeout that kills the process tree | `platform.run`, `verify.py` | S9 |
| Local files | Note locks in the runtime folder; the runtime folder created owner-only; `doctor` flags a readable `.env` or runtime folder, `doctor --fix` restricts them | `security.py`, `vault.lock_dir` | S10 |
| Notes Claude reads | A copy of the project's notes and its tasks, not the whole vault | `tasks.copy_view` | S12 |
| Dashboard reach | Listens on `127.0.0.1` only, never another interface | `web.server.HOST` | — |
| Dashboard login | A random token in `<runtime>/dashboard.token` (owner-only), exchanged once for an HttpOnly, SameSite=Strict cookie; every page but `/login` and `/static/` needs it | `web.auth.Guard` | — |
| DNS rebinding | Requests whose `Host` isn't `127.0.0.1:<port>` or `localhost:<port>` are refused | `web.auth.Guard` | — |
| Cross-site requests | Every POST needs a CSRF token derived from the login token (header or form field); a POST with a foreign `Origin` is refused | `web.auth.Guard` | — |
| Dashboard actions | Every button calls the same `Flow` method as the Telegram command, so the dashboard opens no way past a gate the bot doesn't have | `web.server.Dashboard` | — |
| Dashboard config writes | The only file the dashboard writes is the config, and only to append `[projects.<name>]` for an existing git checkout with a valid name; a result that doesn't load is rolled back | `web.server.Dashboard.add_project`, `config.append_project` | — |
| Dashboard output | Notes and agent text are rendered as Markdown with raw HTML off and secrets masked; diffs and logs are masked too; the login token is added to the redaction list | `web.server.markdown`, `redact.py` | — |
| Free-tier worker | With `free_only`, a worker model must end in `:free` or be a listed combo whose every entry does; checked at load and before every worker run, failing closed when the gateway can't be asked | `config._check_free`, `gateway.ensure_free`, `opencode.Client.prompt` | — |
| opencode server | devflow starts `opencode serve` on 127.0.0.1; on opencode 2.x with a password of its own (`<runtime>/opencode.password`, owner-only), and a v2 server it has no password for is refused | `opencode.Client` | — |
| Worker on opencode 2.x | The same ruleset, translated (`shell`, `subagent`); `external_directory`, which v2 allows by default, is denied explicitly, and so are `execute` and image generation | `opencode.ruleset_v2` | — |
| Gateway key | Never written by devflow: the runtime `opencode.json` holds an `{env:NAME}` reference; `gateway.api_key_env` must be a variable name, not a value | `config._gateway`, `opencode.gateway_provider` | — |

## What is not protected

These are real limits. Decide with them in mind.

- **The worker's allowed commands run your code.** `php -l` doesn't execute
  the file, but a test runner, `composer`, `npm` or any interpreter runs code
  the worker wrote, as your user, with full network access. Putting one in
  `WORKER_SHELL_ALLOW` gives a hijacked worker a way around the shell policy.
  The same holds for verification: phpunit and other test commands run the
  worker's code (with a minimal environment, but your user's files and
  network). Only an OS-level sandbox closes this; it is planned for v0.3
  (PRD SR15).
- **`UNSAFE_SHELL=true`** turns the shell policy off except for git. Every gate
  message and `doctor` warn while it is on.
- **opencode's command matching** is opencode's. devflow tests its ruleset
  against a model of opencode's matcher (last matching rule wins, `*`
  wildcards), not against opencode itself, and relies on opencode checking
  every command in a pipeline or `&&` chain, including command substitutions.
- **Detection, not prevention, for `.git` and linked folders.** A planted hook
  never runs in devflow's own git, and the task stops, but the file stays in
  your main checkout's `.git` until you remove it. Your own next `git` command
  in that checkout would run it. Read the stop message before you use git
  there.
- **Linked folders are writable.** On Windows they are junctions, elsewhere
  symlinks; neither can be made read-only for one process. Writes are caught,
  not blocked.
- **Your global git config** (`~/.gitconfig`) and credential helpers are
  outside the fingerprint.
- **Secrets already in git history** are readable through `git log -p` and
  `git show`, by Claude's `git` tool too. `DENY_READ` covers paths, not
  history.
- **Third parties see your code.** Code and notes the agents read go to the
  model providers you configured (Anthropic, opencode's provider). Specs, diffs
  (unless `SHARE_CODE_VIA_TELEGRAM=false`), `/ask` answers and log excerpts go
  through Telegram, which is not end-to-end encrypted. Redaction catches known
  secret formats, not everything.
- **The bot token is the key.** Anyone holding it can read what the bot sends
  and impersonate it; only the chat and user filters stop them from commanding
  it. Keep `.env` private (`doctor` checks).
- **The dashboard trusts your OS user.** Anyone who can read your runtime
  folder can read `dashboard.token` and use the dashboard, as they could read
  the vault, the config and the bot token. The login link is printed to the
  terminal (or the service's output) at start; treat that output like the
  token. There are no separate dashboard users or roles.
- **Adding a project from the dashboard** lets the agents read that
  repository, as editing the config would. Only a logged-in dashboard session
  (token and CSRF) can do it.
- **Other local processes** running as you can reach 127.0.0.1 like any
  program can; the token, not the network, keeps them out.
- **The AI gateway sees the worker's prompts and code**, and so does every
  provider it routes to. Free tiers often come with weaker data terms (some
  providers may log prompts or use them for training): check each provider's
  policy before routing a private repository through it. The gateway itself
  is software you run and must keep updated; bind it to 127.0.0.1.
- **Claude Code and opencode themselves** are trusted. A compromised CLI or
  model provider is out of scope.

## Per operating system

| Control | Windows | Linux | macOS |
| --- | --- | --- | --- |
| Owner-only runtime folder | user-only ACL (`icacls`), checked by SID | mode 0700 | mode 0700 |
| `.env` check in `doctor` | ACL entries for Everyone, Users, Authenticated Users, Guests | group/other mode bits | group/other mode bits |
| Linked folders | junctions | symlinks | symlinks |
| Process-tree kill | `taskkill /T /F` | process group `SIGKILL` | process group `SIGKILL` |

On Windows, folders on a non-system drive often inherit an ACL that lets
Authenticated Users modify them. `doctor` reports that for the runtime folder;
run `python -m devflow doctor --fix`.

## Security testing

- `tests/test_security.py` attempts each bypass above with fake agents.
- `tests/test_web.py` tries the dashboard without the cookie, with another
  Host header, without or with a wrong CSRF token, from another Origin, and
  with HTML in agent text; `tests/test_gateway.py` checks no key value reaches
  the runtime `opencode.json`.
- CI runs gitleaks on the full history, `pip-audit` on the dependencies, and
  Semgrep (`p/python`, `p/secrets`) on every pull request and weekly.
- Changes to the files listed under *Safety changes* in
  [CONTRIBUTING.md](../CONTRIBUTING.md#safety-changes) need a test that shows
  the blocked action is refused.
