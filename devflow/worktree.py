"""One git worktree per task, on its own branch, never touching the main checkout."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import gitsafe, platform
from .config import Settings


class GitError(RuntimeError):
    pass


def run(cwd: Path, *args: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    """git without hooks, fsmonitor or external diff drivers (see gitsafe), never prompting."""
    try:
        return gitsafe.run(cwd, *args, timeout=timeout)
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


def branch_name(kind: str, task_id: str, title: str, prefix: str = "devflow",
                taken: Callable[[str], bool] = lambda name: False) -> str:
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
            platform.link_dir(src, dst)
        else:
            shutil.copy2(src, dst)
        done.append(rel)
    return done


def linked_dirs(settings: Settings, wt: Path) -> list[str]:
    """The LINK_PATHS that are links in this worktree (folders shared with the main checkout)."""
    return [rel for rel in settings.link_paths if (wt / rel).exists() and platform.is_link(wt / rel)]


def snapshot(wt: Path, rels: list[str]) -> dict[str, str]:
    """A hash per linked folder over every file's path, size and modification time. A linked
    folder is the main checkout's own folder, so a write there bypasses the diff and the
    review (PRD SR4); comparing snapshots is how devflow notices."""
    prints = {}
    for rel in rels:
        root = wt / rel
        h = hashlib.sha256()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                p = os.path.join(dirpath, name)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                h.update(f"{os.path.relpath(p, root)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
        prints[rel] = h.hexdigest()[:16]
    return prints


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
            if platform.is_link(p):
                os.rmdir(p)  # removes the link only, never the target
                dirnames.remove(d)
    git(project_dir, "worktree", "remove", "--force", str(wt))
