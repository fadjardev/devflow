"""Which project a name means, where it lives, and which devcrate vhost serves it."""

from __future__ import annotations

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
            raise VaultError(f"“{query}” matches more than one project:\n{_bullets(hits)}\n\nUse more of the name.")
    raise VaultError(f"No project matches “{query}”. The projects are:\n{_bullets(all_names)}\n\n"
                     f"Any unique part of a name works, e.g. asm.")


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items)


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
