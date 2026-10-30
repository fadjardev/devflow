"""What differs between Windows, Linux and macOS: links, process trees, environments.

Everything here has one implementation per OS family, so the rest of devflow
doesn't branch on the platform.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

WINDOWS = sys.platform == "win32"

# What a lint or test command needs to start, and nothing else: no provider keys, no
# Telegram token, no cloud credentials (PRD SR10). Projects add their own names.
BASE_ENV = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS", "USER", "USERNAME", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL",
    "LC_CTYPE", "TZ",
)


def minimal_env(extra: Iterable[str] = ()) -> dict[str, str]:
    allowed = {name.upper() for name in (*BASE_ENV, *extra)}
    return {k: v for k, v in os.environ.items() if k.upper() in allowed}


def kill_tree(proc: subprocess.Popen[Any]) -> None:
    """Kill a process and everything it started (a test runner's workers, node under a .cmd)."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.kill()


def group_kwargs() -> dict[str, object]:
    """Start a process in a group of its own, so kill_tree can take its children too."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def run(args: Sequence[str] | str, cwd: Path, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
    """subprocess.run with a minimal environment, no stdin, and the whole tree killed on timeout.
    A string is passed to Windows as the exact command line (see run_shell)."""
    cmd = args if isinstance(args, str) else list(args)
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            **group_kwargs())  # type: ignore[call-overload]
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = proc.communicate()
        return subprocess.CompletedProcess(cmd, -9, out, err + f"\n(killed after {int(timeout)}s)")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run_shell(command: str, cwd: Path, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
    """A command line from the project's settings, through the OS shell (so `npm` finds npm.cmd)."""
    if sys.platform == "win32":
        # cmd's own quoting: /s /c strips the outer pair and runs the rest as typed. Passing a
        # list would escape the inner quotes the way C programs expect, which cmd doesn't read.
        return run(f'cmd /d /s /c "{command}"', cwd, env, timeout)
    return run(["/bin/sh", "-c", command], cwd, env, timeout)


def is_link(path: str | Path) -> bool:
    """A symlink or a Windows junction. os.path.isjunction only exists from Python 3.12."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return bool(isjunction(path))
    attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def link_dir(src: Path, dst: Path) -> None:
    """Make `dst` point at the folder `src`: a junction on Windows (no admin rights needed),
    a symlink elsewhere."""
    if WINDOWS:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], capture_output=True, check=True)
    else:
        os.symlink(src, dst, target_is_directory=True)
