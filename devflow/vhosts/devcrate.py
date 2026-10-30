"""devcrate: a Windows nginx + PHP stack (`<root>\\nginx`, `<root>\\php\\php-X.Y`).

The project's own (often hand-written) conf is found by its `root
projects/<folder>` line, copied, its host renamed to a sibling under the same
wildcard certificate, and its `root` pointed at the worktree. nginx is tested
before every reload; a conf that fails is removed again so one task can never
take the other vhosts down.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from . import Site, VhostError


@dataclass(frozen=True)
class Vhost:
    conf: Path
    host: str
    project_segment: str  # first path segment after `projects/` in `root`
    root_rest: str        # anything after it, e.g. "public"
    port: int             # FastCGI port, 90XY -> PHP X.Y


_NAME = re.compile(r"^\s*server_name\s+([^;\s]+)", re.M)
_ROOT = re.compile(r"^\s*root\s+projects/([^/;\s]+)/?([^;\s]*)\s*;", re.M)
_PORT = re.compile(r"fastcgi_pass\s+127\.0\.0\.1:(\d+)")


def temp_host(original: str, task_id: str) -> str:
    """asm.qhomemart.test -> asm-261002-01.qhomemart.test (still under *.qhomemart.test)."""
    first, _, rest = original.partition(".")
    return f"{first}-{task_id}.{rest}" if rest else f"{original}-{task_id}"


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


class Devcrate:
    name = "devcrate"

    def __init__(self, settings: Settings):
        if settings.devcrate_root is None:
            raise VhostError("the devcrate vhost provider needs DEVCRATE_ROOT (devcrate_root)")
        self.s = settings
        self.root = settings.devcrate_root
        self.nginx = self.root / "nginx"

    @property
    def sites(self) -> Path:
        return self.nginx / "conf" / "sites"

    def conf_path(self, task_id: str) -> Path:
        return self.sites / f"devflow-{task_id}.conf"

    def php_exe(self, port: int) -> Path | None:
        """FastCGI port 90XY is served by php\\php-X.Y; use that version's CLI for linting."""
        digits = str(port - 9000)
        if len(digits) != 2:
            return None
        exe = self.root / "php" / f"php-{digits[0]}.{digits[1]}" / "php.exe"
        return exe if exe.is_file() else None

    def find_vhost(self, project_dir: Path) -> Vhost | None:
        target = os.path.realpath(project_dir)
        if not self.sites.is_dir() or self.s.projects is None:  # its confs name projects/<folder>
            return None
        projects = self.s.projects
        for conf in sorted(self.sites.glob("*.conf")):
            if conf.name.startswith("devflow-"):
                continue
            text = conf.read_text(encoding="utf-8", errors="replace")
            root = _ROOT.search(text)
            if not root:
                continue
            if os.path.realpath(projects / root.group(1)) != target:
                continue
            name = _NAME.search(text)
            port = _PORT.search(text)
            if not (name and port):
                continue
            return Vhost(conf, name.group(1), root.group(1), root.group(2).strip("/"), int(port.group(1)))
        return None

    def find(self, project_dir: Path) -> Site | None:
        vh = self.find_vhost(project_dir)
        return Site(vh.host, self.php_exe(vh.port), vh) if vh else None

    def _nginx(self, *args: str) -> subprocess.CompletedProcess[str]:
        exe = self.nginx / "current" / "nginx.exe"
        prefix = str(self.nginx).rstrip("\\/") + "/"
        return subprocess.run([str(exe), "-p", prefix, *args], cwd=self.nginx,
                              capture_output=True, text=True, errors="replace")

    def _test_and_reload(self) -> None:
        t = self._nginx("-t")
        if t.returncode != 0:
            raise VhostError(t.stderr.strip() or t.stdout.strip())
        r = self._nginx("-s", "reload")
        if r.returncode != 0:
            raise VhostError(f"nginx reload failed: {r.stderr.strip()}")

    def create(self, site: Site, task_id: str, worktree: Path) -> str:
        vh: Vhost = site.detail
        host, text = render(vh.conf.read_text(encoding="utf-8", errors="replace"), vh, task_id, worktree.name)
        path = self.conf_path(task_id)
        path.write_text(text, encoding="utf-8", newline="\n")
        try:
            self._test_and_reload()
        except VhostError:
            path.unlink(missing_ok=True)
            raise
        return host

    def remove(self, task_id: str) -> None:
        path = self.conf_path(task_id)
        if path.exists():
            path.unlink()
            self._test_and_reload()

    def doctor(self) -> list[tuple[bool, str, str]]:
        exe = self.nginx / "current" / "nginx.exe"
        if not exe.is_file():
            return [(False, "nginx (devcrate)", f"not found at {exe}")]
        t = self._nginx("-t")
        return [(t.returncode == 0, "nginx -t (devcrate)", (t.stderr.strip().splitlines() or [""])[-1])]
