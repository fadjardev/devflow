"""Which project a name means, where it lives, and which devcrate vhost serves it."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import vault
from .config import Settings
from .tasks import PROJECTS_DIR
from .vault import VaultError


def names(vault_root: Path) -> list[str]:
    return sorted(p.stem for p in (vault_root / PROJECTS_DIR).glob("*.md"))


def resolve(vault_root: Path, query: str) -> str:
    """Exact name, else the one project whose name starts with / contains `query`."""
    q = query.strip().lower()
    all_names = names(vault_root)
    for n in all_names:
        if n.lower() == q:
            return n
    for match in (lambda n: n.lower().startswith(q), lambda n: q in n.lower()):
        hits = [n for n in all_names if match(n)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise VaultError(f"'{query}' is ambiguous: {', '.join(hits)}")
    raise VaultError(f"no project matches '{query}'. Known: {', '.join(all_names)}")


def checkout(settings: Settings, project: str) -> Path:
    """The project's folder, from its note's `path` (default: PROJECTS_PATH/<name>)."""
    note = vault.read(settings.vault / PROJECTS_DIR / f"{project}.md")
    raw = (note.meta.get("path") or "").strip()
    path = Path(raw) if raw else settings.projects / project
    if not path.is_dir():
        raise VaultError(f"{project}: {path} does not exist")
    return path


def directory(settings: Settings, project: str) -> Path:
    """The project's folder, which tasks need to be a git checkout."""
    path = checkout(settings, project)
    if not (path / ".git").exists():
        raise VaultError(f"{project}: {path} is not a git checkout")
    return path


@dataclass(frozen=True)
class Vhost:
    conf: Path
    host: str
    project_segment: str  # first path segment after `projects/` in `root`
    root_rest: str        # anything after it, e.g. "public"
    port: int             # FastCGI port, 90XY -> PHP X.Y


_SERVER = re.compile(r"server\s*\{")
_NAME = re.compile(r"^\s*server_name\s+([^;\s]+)", re.M)
_ROOT = re.compile(r"^\s*root\s+projects/([^/;\s]+)/?([^;\s]*)\s*;", re.M)
_PORT = re.compile(r"fastcgi_pass\s+127\.0\.0\.1:(\d+)")


def sites_dir(settings: Settings) -> Path:
    return settings.nginx_prefix / "conf" / "sites"


def find_vhost(settings: Settings, project_dir: Path) -> Vhost | None:
    target = os.path.realpath(project_dir)
    for conf in sorted(sites_dir(settings).glob("*.conf")):
        if conf.name.startswith("devflow-"):
            continue
        text = conf.read_text(encoding="utf-8", errors="replace")
        root = _ROOT.search(text)
        if not root:
            continue
        seg_path = settings.projects / root.group(1)
        if os.path.realpath(seg_path) != target:
            continue
        name = _NAME.search(text)
        port = _PORT.search(text)
        if not (name and port):
            continue
        return Vhost(conf, name.group(1), root.group(1), root.group(2).strip("/"), int(port.group(1)))
    return None


def php_exe(settings: Settings, port: int) -> Path | None:
    """FastCGI port 90XY is served by php\\php-X.Y; use that version's CLI for linting."""
    digits = str(port - 9000)
    if len(digits) != 2:
        return None
    exe = settings.devcrate_root / "php" / f"php-{digits[0]}.{digits[1]}" / "php.exe"
    return exe if exe.is_file() else None
