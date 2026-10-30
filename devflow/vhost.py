"""A throwaway devcrate vhost per task, so the worktree can be exercised over HTTPS.

The project's own (often hand-written) conf is copied, its host renamed to a
sibling under the same wildcard certificate, and its `root` pointed at the
worktree. nginx is tested before every reload; a conf that fails is removed
again so one task can never take the other vhosts down.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Settings
from .projects import Vhost, sites_dir


class VhostError(RuntimeError):
    pass


def temp_host(original: str, task_id: str) -> str:
    """asm.qhomemart.test -> asm-261002-01.qhomemart.test (still under *.qhomemart.test)."""
    first, _, rest = original.partition(".")
    return f"{first}-{task_id}.{rest}" if rest else f"{original}-{task_id}"


def conf_path(settings: Settings, task_id: str) -> Path:
    return sites_dir(settings) / f"devflow-{task_id}.conf"


def render(source: str, vh: Vhost, task_id: str, worktree_name: str) -> tuple[str, str]:
    host = temp_host(vh.host, task_id)
    rest = f"/{vh.root_rest}" if vh.root_rest else ""
    old_root = f"projects/{vh.project_segment}{rest}"
    new_root = f"projects/.worktrees/{worktree_name}{rest}"
    lines = []
    for line in source.splitlines():
        stripped = line.strip()
        # Only names and log files are renamed; certificate paths stay as they are.
        if stripped.startswith(("server_name", "access_log", "error_log")):
            line = line.replace(vh.host, host)
        elif stripped.startswith("root ") and old_root in stripped:
            line = line.replace(old_root, new_root)
        lines.append(line)
    header = f"# devflow temporary vhost for task {task_id} — removed when the task ends\n"
    return host, header + "\n".join(lines) + "\n"


def _nginx(settings: Settings, *args: str) -> subprocess.CompletedProcess[str]:
    exe = settings.nginx_prefix / "current" / "nginx.exe"
    prefix = str(settings.nginx_prefix).rstrip("\\/") + "/"
    return subprocess.run([str(exe), "-p", prefix, *args], cwd=settings.nginx_prefix,
                          capture_output=True, text=True, errors="replace")


def _test_and_reload(settings: Settings) -> None:
    t = _nginx(settings, "-t")
    if t.returncode != 0:
        raise VhostError(t.stderr.strip() or t.stdout.strip())
    r = _nginx(settings, "-s", "reload")
    if r.returncode != 0:
        raise VhostError(f"nginx reload failed: {r.stderr.strip()}")


def create(settings: Settings, vh: Vhost, task_id: str, worktree: Path) -> str:
    host, text = render(vh.conf.read_text(encoding="utf-8", errors="replace"), vh, task_id, worktree.name)
    path = conf_path(settings, task_id)
    path.write_text(text, encoding="utf-8", newline="\n")
    try:
        _test_and_reload(settings)
    except VhostError:
        path.unlink(missing_ok=True)
        raise
    return host


def remove(settings: Settings, task_id: str) -> None:
    path = conf_path(settings, task_id)
    if path.exists():
        path.unlink()
        _test_and_reload(settings)
