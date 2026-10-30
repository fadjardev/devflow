# 4. Claude reads git through a checked devflow tool, not Bash rules

Date: 2026-10-30 · Status: accepted

## Context

Finding S4 (PRD §7.2, "to confirm"): Claude's read-only roles were allowed
`Bash(git log:*)`, `Bash(git show:*)`, `Bash(git diff:*)`, `Bash(git status:*)`
and `Bash(git blame:*)`. Claude Code matches these as command **prefixes**, so
any option after the prefix is allowed.

### Reproduction (2026-10-30, Claude Code 2.1.312, `claude -p`)

Throwaway repo with a `.env` canary, and a folder next to it holding another
canary. Flags as devflow used them:
`--permission-mode dontAsk`, the five `Bash(git …:*)` rules in
`--allowedTools`, `Read(**/.env)` in `--disallowedTools`. The model was asked
to run three commands and report.

| Command | Result |
| --- | --- |
| `git diff --no-index .env a.txt` | refused by Claude Code |
| `git diff --no-index ../outside/creds.txt a.txt` | refused by Claude Code |
| `git log -1 --output=../outside/written.txt` | **ran; the file was written outside the repo** |

Claude Code has its own check that stopped `--no-index` here, but it is not
part of devflow's rules and not something devflow can test. `--output` was not
stopped: **S4 is confirmed for writes** (any path the user can write, with
git-log content). Other options with side effects (`--ext-diff`, `--textconv`,
`-O<orderfile>`, `-c`) would rely on the same undocumented checks.

## Decision

Claude gets **no Bash rules at all**. Read-only git goes through a devflow-mcp
tool, `git`, which:

- accepts only `log`, `show`, `diff`, `status`, `blame`, `rev-parse`,
  `merge-base` and `ls-files`;
- accepts options only from an allow-list per subcommand (no `--output`,
  `--no-index`, `--ext-diff`, `--textconv`, `-O`, `-c`, `-C`, `--git-dir`,
  `--work-tree`, `--exec-path`);
- refuses paths that leave the repository or match `DENY_READ`, including the
  `<rev>:<path>` form;
- runs git with devflow's hardened environment (no hooks, no fsmonitor, no
  external diff or textconv, no pager), in the run's own directory.

The check lives in `devflow/gitsafe.py` and is tested case by case.

## Consequences

- The safety of Claude's git access no longer depends on Claude Code's
  undocumented command parsing.
- Prompts change from "run `git diff …`" to "call the `git` tool with …".
- Something useful outside the allow-list is refused; widening the list is a
  reviewed change with a test.
