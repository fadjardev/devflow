import dataclasses
import os
import subprocess
import sys

from devflow import config, worktree


def g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_commit_handles_ignored_tracked_and_untracked_linked_paths(tmp_path):
    """Like asm: .env and vendor are gitignored, config.php is tracked. Before the fix,
    `git add` failed with "paths are ignored" because the excludes named ignored paths."""
    repo = tmp_path / "repo"
    repo.mkdir()
    g(repo, "init", "-q", "-b", "development")
    g(repo, "config", "user.email", "t@t")
    g(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text(".env\n/vendor/\n")
    (repo / "config.php").write_text("<?php $a = 1;\n")
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "-m", "init")

    (repo / ".env").write_text("SECRET=1\n")              # linked, ignored
    (repo / "vendor").mkdir()
    (repo / "vendor" / "lib.php").write_text("<?php\n")   # linked, ignored
    (repo / "local.cfg").write_text("copied in\n")        # linked, untracked, not ignored
    (repo / "config.php").write_text("<?php $a = 2;\n")   # linked name, but tracked: the worker's edit
    (repo / "new.php").write_text("<?php // new\n")       # the worker's new file

    settings = dataclasses.replace(config.load(), link_paths=[".env", "vendor", "local.cfg", "config.php"])
    sha = worktree.commit(settings, repo, "fix: test")

    assert sha
    committed = set(g(repo, "show", "--name-only", "--format=", "HEAD").splitlines())
    assert committed == {"config.php", "new.php"}
    assert "local.cfg" in g(repo, "status", "--porcelain")  # left alone, not committed


def test_is_link_sees_junctions_and_symlinks_but_not_folders(tmp_path):
    """worktree.remove unlinks these before deleting the worktree; on Python 3.11 there is
    no os.path.isjunction, so junctions must be recognised another way."""
    target = tmp_path / "vendor"
    target.mkdir()
    link = tmp_path / "link"
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        os.symlink(target, link, target_is_directory=True)

    assert worktree._is_link(str(link))
    assert not worktree._is_link(str(target))
