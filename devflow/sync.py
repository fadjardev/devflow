"""Staying in step with colleagues on the shared branch.

- Tasks start from the freshly fetched `origin/development`, never from
  whatever the local checkout happens to be on.
- Colleagues' unmerged branches and the files they touch are surfaced to the
  planner and to you, so overlapping work is visible before it collides.
- Before gate 2, the merge into the shared branch is simulated with
  `git merge-tree` — nothing in any checkout is touched.

Fetching can fail (no non-interactive credential). Then everything works
against the last-fetched remote-tracking branch, and says it is stale.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .worktree import GitError, git, run

log = logging.getLogger("devflow.sync")


@dataclass
class Fetch:
    ok: bool
    message: str

    def note(self) -> str:
        return "fetched just now" if self.ok else f"⚠️ fetch failed, base may be stale ({self.message})"


@dataclass
class Overlap:
    branch: str
    author: str
    age: str
    files: list[str] = field(default_factory=list)


def fetch(settings: Settings, pdir: Path) -> Fetch:
    try:
        git(pdir, "fetch", "--prune", "--quiet", settings.git_remote, timeout=120)
        return Fetch(True, "")
    except (GitError, TimeoutError) as e:
        log.warning("git fetch %s in %s failed: %s", settings.git_remote, pdir.name, e)
        return Fetch(False, str(e).splitlines()[-1][:200] if str(e) else "error")


def _has(pdir: Path, ref: str) -> bool:
    return git(pdir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False) != ""


def default_branch(settings: Settings, pdir: Path) -> str:
    """The remote's default branch (what `<remote>/HEAD` points at), else main or master."""
    head = git(pdir, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{settings.git_remote}/HEAD", check=False)
    if head.startswith(f"{settings.git_remote}/"):
        return head.split("/", 1)[1]
    for name in ("main", "master"):
        if _has(pdir, f"{settings.git_remote}/{name}") or _has(pdir, name):
            return name
    raise GitError(f"{settings.git_remote} has no HEAD, main or master; set base_branch for this project")


def base_ref(settings: Settings, pdir: Path) -> str:
    """`origin/development` if it exists, else the local `development`. An empty base_branch
    means the remote's default branch."""
    branch = settings.base_branch or default_branch(settings, pdir)
    for ref in (f"{settings.git_remote}/{branch}", branch):
        if _has(pdir, ref):
            return ref
    raise GitError(f"neither {settings.git_remote}/{branch} nor {branch} exists")


def sha(pdir: Path, ref: str) -> str:
    return git(pdir, "rev-parse", f"{ref}^{{commit}}")


def branch_taken(settings: Settings, pdir: Path, name: str) -> bool:
    return _has(pdir, f"refs/heads/{name}") or _has(pdir, f"refs/remotes/{settings.git_remote}/{name}")


def on_remote(settings: Settings, pdir: Path, name: str) -> bool:
    return _has(pdir, f"refs/remotes/{settings.git_remote}/{name}")


def overlaps(settings: Settings, pdir: Path, base: str, exclude: tuple[str, ...] = ()) -> list[Overlap]:
    """Remote branches active in the last OVERLAP_DAYS, not merged into `base`, with the files they change."""
    remote = settings.git_remote
    skip = {f"{remote}/{settings.base_branch}", f"{remote}/main", f"{remote}/master", f"{remote}/HEAD", remote,
            *(f"{remote}/{b}" for b in exclude)}
    since = time.time() - settings.overlap_days * 86400
    out = git(pdir, "for-each-ref", "--sort=-committerdate",
              "--format=%(refname:short)\t%(committerdate:unix)\t%(authorname)\t%(committerdate:relative)",
              f"refs/remotes/{remote}/")
    found = []
    for line in out.splitlines():
        try:
            ref, stamp, author, age = line.split("\t")
        except ValueError:
            continue
        if ref in skip or int(stamp) < since:
            continue
        if git(pdir, "rev-list", "--count", f"{base}..{ref}", check=False) in ("", "0"):
            continue  # already merged
        files = git(pdir, "diff", "--name-only", f"{base}...{ref}", check=False).splitlines()
        found.append(Overlap(ref.removeprefix(f"{remote}/"), author, age, [f for f in files if f]))
        if len(found) >= 30:
            break
    return found


def describe(found: list[Overlap], only_files: list[str] | None = None) -> str:
    """Markdown list; with `only_files`, keep just branches touching those files."""
    lines = []
    for o in found:
        files = o.files if only_files is None else sorted(set(o.files) & set(only_files))
        if only_files is not None and not files:
            continue
        shown = ", ".join(f"`{f}`" for f in files[:12]) + (f" (+{len(files) - 12} more)" if len(files) > 12 else "")
        lines.append(f"- `{o.branch}` — {o.author}, {o.age}: {shown or '(no file changes)'}")
    return "\n".join(lines)


def mentioned(found: list[Overlap], text: str) -> list[Overlap]:
    """Branches whose files are named in `text` (the spec) — a cheap early warning."""
    return [o for o in found if any(f in text for f in o.files)]


def merge_check(wt: Path, base: str) -> tuple[bool, list[str]]:
    """Simulate merging HEAD into `base`. (clean, conflicting files). Touches no checkout."""
    p = run(wt, "merge-tree", "--write-tree", "--name-only", "--no-messages", base, "HEAD")
    if p.returncode not in (0, 1):  # 0 clean, 1 conflicts, anything else is git failing
        raise GitError(f"merge-tree: {p.stderr.strip() or p.stdout.strip()}")
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    return p.returncode == 0, lines[1:]


def unmerged(wt: Path) -> list[str]:
    return [f for f in git(wt, "diff", "--name-only", "--diff-filter=U", check=False).splitlines() if f]


def markers_left(wt: Path, files: list[str]) -> list[str]:
    """Files still holding conflict markers."""
    bad = []
    for f in files:
        try:
            text = (wt / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(line.startswith(("<<<<<<< ", ">>>>>>> ")) for line in text.splitlines()):
            bad.append(f)
    return bad
