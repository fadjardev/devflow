"""One git worktree per task, on its own branch, never touching the main checkout."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import Settings


class GitError(RuntimeError):
    pass


# Unattended: a credential or editor prompt must fail fast, never wait for a human.
_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
        "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"}


def run(cwd: Path, *args: str, timeout: float = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=_ENV, stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)}: timed out after {int(timeout)}s") from None


def git(cwd: Path, *args: str, check: bool = True, timeout: float = 300) -> str:
    proc = run(cwd, *args, timeout=timeout)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "task"


def branch_name(kind: str, task_id: str, title: str, prefix: str = "devflow", taken=lambda name: False) -> str:
    """devflow/fix/261002-01-sort-columns — namespaced so colleagues see where it came from,
    and never a name that already exists locally or on the remote."""
    kind = kind if kind in ("feat", "fix", "refactor", "chore", "docs", "perf") else "fix"
    name = f"{prefix.strip('/')}/{kind}/{task_id}-{slug(title)}" if prefix.strip("/") else f"{kind}/{task_id}-{slug(title)}"
    candidate, n = name, 1
    while taken(candidate):
        n += 1
        candidate = f"{name}-{n}"
    return candidate


def path_for(settings: Settings, project_dir: Path, task_id: str) -> Path:
    return settings.worktrees / f"{project_dir.name}-{task_id}"


def create(settings: Settings, project_dir: Path, task_id: str, branch: str, base: str) -> Path:
    """Add the worktree on a new branch at commit `base` (the fetched shared branch, not local HEAD)."""
    wt = path_for(settings, project_dir, task_id)
    settings.worktrees.mkdir(parents=True, exist_ok=True)
    git(project_dir, "worktree", "add", "-b", branch, str(wt), base)
    link_untracked(settings, project_dir, wt)
    return wt


def link_untracked(settings: Settings, project_dir: Path, wt: Path) -> list[str]:
    """Bring over gitignored things the app needs to run: folders as junctions, files as copies."""
    done = []
    for rel in settings.link_paths:
        src, dst = project_dir / rel, wt / rel
        if not src.exists() or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], capture_output=True, check=True)
        else:
            shutil.copy2(src, dst)
        done.append(rel)
    return done


def _must_exclude(wt: Path, rel: str) -> bool:
    """A linked path needs an explicit exclude only if git would otherwise stage it: present,
    untracked, and not ignored. Ignored paths are skipped by `add -A` anyway, and naming one
    in a pathspec makes git fail ("paths are ignored"). Tracked paths were never linked (the
    worktree has its own copy), so the worker's edits to them must be committed."""
    if not (wt / rel).exists():
        return False
    tracked = run(wt, "ls-files", "--error-unmatch", "--", rel).returncode == 0
    ignored = run(wt, "check-ignore", "-q", "--", rel).returncode == 0
    return not tracked and not ignored


def commit(settings: Settings, wt: Path, message: str) -> str | None:
    """Stage everything except the linked paths; returns the new sha, or None if nothing changed.
    In the middle of a merge this concludes the merge, even if the merge itself changed nothing."""
    pathspec = ["."] + [f":(exclude){p}" for p in settings.link_paths if _must_exclude(wt, p)]
    git(wt, "add", "-A", "--", *pathspec)
    merging = run(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0
    if not merging and not git(wt, "diff", "--cached", "--name-only"):
        return None
    git(wt, "commit", "-q", "-m", message)
    return git(wt, "rev-parse", "HEAD")


def changed_files(wt: Path, base: str) -> list[str]:
    out = git(wt, "diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD")
    return [line for line in out.splitlines() if line]


def diff(wt: Path, base: str, stat: bool = False) -> str:
    return git(wt, "diff", "--stat" if stat else "--patch", f"{base}...HEAD")


def remove(project_dir: Path, wt: Path) -> None:
    """Remove the worktree but keep its branch. Junctions go first, so nothing follows them."""
    if not wt.exists():
        git(project_dir, "worktree", "prune", check=False)
        return
    for dirpath, dirnames, _ in os.walk(wt):
        for d in list(dirnames):
            p = os.path.join(dirpath, d)
            if os.path.isjunction(p) or os.path.islink(p):
                os.rmdir(p)  # removes the link only, never the target
                dirnames.remove(d)
    git(project_dir, "worktree", "remove", "--force", str(wt))
