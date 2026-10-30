"""Which project a name means and where it lives.

With `devflow.toml` the projects are its `[projects.<name>]` registry. In legacy
mode they are the vault's `01-Projects/<name>.md` notes, with the checkout at
the note's `path:` (default PROJECTS_PATH/<name>).
"""

from __future__ import annotations

from pathlib import Path

from . import vault
from .config import Settings
from .tasks import PROJECTS_DIR
from .vault import VaultError


def names(settings: Settings) -> list[str]:
    if settings.registry is not None:
        return sorted(settings.registry)
    return sorted(p.stem for p in (settings.vault / PROJECTS_DIR).glob("*.md"))


def resolve(settings: Settings, query: str) -> str:
    """Exact name, else the one project whose name starts with / contains `query`."""
    q = query.strip().lower()
    all_names = names(settings)
    for n in all_names:
        if n.lower() == q:
            return n
    for match in (lambda n: n.lower().startswith(q), lambda n: q in n.lower()):
        hits = [n for n in all_names if match(n)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise VaultError(f"“{query}” matches more than one project:\n{_bullets(hits)}\n\nUse more of the name.")
    if not all_names:
        raise VaultError("No projects are configured yet. Add one under [projects] in devflow.toml.")
    raise VaultError(f"No project matches “{query}”. The projects are:\n{_bullets(all_names)}\n\n"
                     f"Any unique part of a name works, e.g. asm.")


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items)


def checkout(settings: Settings, project: str) -> Path:
    """The project's folder: its registry `path`, or (legacy) its note's `path`."""
    if settings.registry is not None:
        if project not in settings.registry:
            raise VaultError(f"unknown project: {project}")
        path = settings.registry[project].path
    else:
        note = vault.read(settings.vault / PROJECTS_DIR / f"{project}.md")
        raw = (note.meta.get("path") or "").strip()
        if raw:
            path = Path(raw)
        elif settings.projects is not None:
            path = settings.projects / project
        else:
            raise VaultError(f"{project}: its note has no path:")
    if not path.is_dir():
        raise VaultError(f"{project}: {path} does not exist")
    return path


def directory(settings: Settings, project: str) -> Path:
    """The project's folder, which tasks need to be a git checkout."""
    path = checkout(settings, project)
    if not (path / ".git").exists():
        raise VaultError(f"{project}: {path} is not a git checkout")
    return path
