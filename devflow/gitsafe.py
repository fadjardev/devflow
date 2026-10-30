"""git that agents can't turn against devflow or the developer.

Three controls (PRD SR3, SR5):

- `HARDEN`: every git devflow runs ignores hooks, fsmonitor, external diff
  drivers and textconv, so nothing planted in `.git` or the config runs.
- `fingerprint` / `changes`: the parts of `.git` that can make git run code
  (config, hooks, attributes) are recorded when a worktree is made and
  compared before every commit and at gate 2.
- `check_read_only`: the only git Claude may run, through devflow-mcp's `git`
  tool. Subcommands and options come from an allow-list, and paths must stay
  inside the repository and outside DENY_READ (ADR 0004).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path, PurePosixPath

from .config import runtime_dir


class UnsafeGit(ValueError):
    pass


def _no_hooks_dir() -> str:
    """An empty folder for core.hooksPath, so git finds no hooks wherever .git says they are."""
    path = runtime_dir() / "no-hooks"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def harden() -> list[str]:
    return ["-c", f"core.hooksPath={_no_hooks_dir()}", "-c", "core.fsmonitor=false",
            "-c", "diff.external=", "-c", "core.pager=cat", "-c", "protocol.ext.allow=never"]


# Unattended: a credential or editor prompt must fail fast, never wait for a human.
ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
       "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no",
       "GIT_EXTERNAL_DIFF": "", "GIT_PAGER": "cat"}


def run(cwd: Path, *args: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *harden(), *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=ENV, stdin=subprocess.DEVNULL, timeout=timeout)


# -- tamper detection -----------------------------------------------------------------------

def _git_dirs(wt: Path) -> tuple[Path, Path]:
    out = run(wt, "rev-parse", "--path-format=absolute", "--git-common-dir", "--git-dir", timeout=30)
    if out.returncode != 0:
        raise UnsafeGit(f"not a git checkout: {wt}")
    common, own = out.stdout.splitlines()[:2]
    return Path(common), Path(own)


def _config(path: Path) -> str:
    """The config as git reads it, includes followed, minus branch.* (devflow and colleagues
    create branches all the time; a branch entry can't make git run anything)."""
    if not path.is_file():
        return ""
    out = run(path.parent, "config", "--file", str(path), "--list", "--includes", timeout=30)
    return "\n".join(sorted(line for line in out.stdout.splitlines() if not line.startswith("branch.")))


def _digest(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()[:16]


def fingerprint(wt: Path) -> dict[str, str]:
    """Hashes of everything in .git that can make git run code: config (shared and per
    worktree), hooks, and info/attributes (which names filter drivers)."""
    common, own = _git_dirs(wt)
    prints = {".git/config": _digest(_config(common / "config")),
              ".git/config.worktree": _digest(_config(own / "config.worktree")),
              ".git/info/attributes": _digest((common / "info" / "attributes").read_bytes()
                                              if (common / "info" / "attributes").is_file() else b"")}
    hooks = common / "hooks"
    if hooks.is_dir():
        for hook in sorted(hooks.iterdir()):
            if hook.is_file() and not hook.name.endswith(".sample"):
                prints[f".git/hooks/{hook.name}"] = _digest(hook.read_bytes())
    return prints


def changes(baseline: dict[str, str], wt: Path) -> list[str]:
    """What differs from the fingerprint taken when the worktree was made."""
    now = fingerprint(wt)
    return sorted(k for k in baseline.keys() | now.keys() if baseline.get(k) != now.get(k))


# -- Claude's read-only git -----------------------------------------------------------------

_DIFF_OPTS = {"--stat", "--shortstat", "--numstat", "--name-only", "--name-status", "-p", "--patch",
              "--no-patch", "-s", "-w", "--ignore-all-space", "-b", "--ignore-space-change", "-M",
              "--find-renames", "--diff-filter", "--no-color", "--color", "--word-diff", "-U", "--unified",
              "--minimal", "--summary", "--full-index", "--abbrev"}
_LOG_OPTS = _DIFF_OPTS | {"--oneline", "--format", "--pretty", "--graph", "--decorate", "--no-decorate",
                          "--all", "--branches", "--remotes", "--since", "--until", "--after", "--before",
                          "--author", "--committer", "--grep", "-i", "--regexp-ignore-case", "--follow", "-n",
                          "--max-count", "--skip", "--reverse", "--first-parent", "--no-merges", "--merges",
                          "-S", "-G", "--date", "--abbrev-commit", "--left-right", "--cherry-pick",
                          "--ancestry-path", "--topo-order", "--date-order", "--source", "--name-status",
                          "--full-history", "--simplify-by-decoration", "-L"}
ALLOWED: dict[str, set[str]] = {
    "log": _LOG_OPTS,
    "show": _LOG_OPTS,
    "diff": _DIFF_OPTS | {"--cached", "--staged", "--merge-base", "--check"},
    "status": {"--short", "-s", "--porcelain", "--branch", "-b", "--untracked-files", "-u", "--ignored"},
    "blame": {"-L", "-w", "-M", "-C", "--porcelain", "--line-porcelain", "-e", "-s", "--date", "--abbrev",
              "--show-name", "--show-number", "-n", "-l", "-t", "--root"},
    "rev-parse": {"--abbrev-ref", "--short", "--verify", "--symbolic-full-name", "--show-toplevel", "-q",
                  "--quiet"},
    "merge-base": {"--is-ancestor", "--fork-point", "--all", "--octopus"},
    "ls-files": {"--others", "-o", "--exclude-standard", "--cached", "-c", "--modified", "-m", "--deleted",
                 "-d", "--full-name", "--error-unmatch"},
}
# Options whose value may come in the next argument rather than after "=".
_TAKES_VALUE = {"-n", "--max-count", "--skip", "--author", "--committer", "--grep", "--since", "--until",
                "--after", "--before", "--format", "--pretty", "--date", "--diff-filter", "-S", "-G", "-L",
                "-U", "--unified", "--abbrev"}
# Short options that carry their value attached: -n5, -U3, -L10,20, -uno, -M50%.
_ATTACHED = ("-n", "-U", "-L", "-S", "-G", "-u", "-M", "-C")
# Diff-producing subcommands get these, whatever the config says.
_SAFE_DIFF = ["--no-ext-diff", "--no-textconv"]


def _denied(rel: str, deny_read: list[str]) -> bool:
    p = PurePosixPath(rel)
    for pattern in deny_read:
        pat = pattern.replace("\\", "/")
        while pat.startswith("**/"):
            pat = pat[3:]
        if p.match(pat) or any(PurePosixPath(*p.parts[i:]).match(pat) for i in range(len(p.parts))):
            return True
    return False


def _check_path(arg: str, repo: Path, deny_read: list[str]) -> None:
    raw = arg.replace("\\", "/")
    if not raw or raw in (".", "./"):
        return
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":") or raw.startswith("~"):
        raise UnsafeGit(f"path outside the repository: {arg}")
    root = repo.resolve()
    target = (root / raw).resolve()
    if not target.is_relative_to(root):
        raise UnsafeGit(f"path outside the repository: {arg}")
    rel = target.relative_to(root).as_posix()
    if rel.split("/", 1)[0] == ".git":
        raise UnsafeGit(f"path inside .git: {arg}")
    if _denied(rel, deny_read):
        raise UnsafeGit(f"path is in DENY_READ: {arg}")


def _check_revision(arg: str, repo: Path, deny_read: list[str]) -> None:
    """A revision, a range, or `<rev>:<path>`. Whatever could name a file is checked as a path."""
    rev, colon, path = arg.partition(":")
    if colon:
        if len(rev) == 1 and rev.isalpha():  # C:\... is a Windows path, not rev:path
            raise UnsafeGit(f"path outside the repository: {arg}")
        _check_path(path, repo, deny_read)
    for part in rev.replace("...", "..").split(".."):
        if part.startswith("-"):
            raise UnsafeGit(f"not a revision: {arg}")
        _check_path(part.split("@{", 1)[0].rstrip("^~0123456789"), repo, deny_read)


def check_read_only(args: list[str], repo: Path, deny_read: list[str]) -> list[str]:
    """Validate a read-only git command; returns the arguments to run. Raises UnsafeGit."""
    if not args:
        raise UnsafeGit("no git subcommand")
    sub, rest = args[0], list(args[1:])
    if sub not in ALLOWED:
        raise UnsafeGit(f"git {sub} is not allowed; allowed: {', '.join(sorted(ALLOWED))}")
    allowed = ALLOWED[sub]
    out = [sub] + (_SAFE_DIFF if sub in ("log", "show", "diff") else [])
    paths_only = False
    i = 0
    while i < len(rest):
        arg = rest[i]
        i += 1
        if paths_only:
            _check_path(arg, repo, deny_read)
        elif arg == "--":
            paths_only = True
        elif sub in ("log", "show") and arg[:1] == "-" and arg[1:].isdigit():
            pass  # -5: the last five commits
        elif arg.startswith("-"):
            name = arg.split("=", 1)[0]
            attached = next((s for s in _ATTACHED if arg.startswith(s) and len(arg) > len(s)
                             and not arg.startswith("--")), None)
            if name not in allowed and not (attached and attached in allowed):
                raise UnsafeGit(f"option not allowed for git {sub}: {name}")
            if name in _TAKES_VALUE and "=" not in arg and not attached:
                if i >= len(rest):
                    raise UnsafeGit(f"{name} needs a value")
                out.append(arg)
                arg = rest[i]
                i += 1
        else:
            _check_revision(arg, repo, deny_read)
        out.append(arg)
    return out


def run_read_only(args: list[str], repo: Path, deny_read: list[str], limit: int = 60_000) -> str:
    """Run a checked read-only git command in `repo` and return its output, clipped."""
    checked = check_read_only(args, repo, deny_read)
    try:
        proc = run(repo, *checked, timeout=120)
    except subprocess.TimeoutExpired:
        raise UnsafeGit(f"git {checked[0]} timed out") from None
    text = proc.stdout if proc.returncode == 0 else f"exit {proc.returncode}\n{proc.stderr}\n{proc.stdout}"
    return text if len(text) <= limit else text[:limit] + f"\n… (clipped at {limit} characters)"
