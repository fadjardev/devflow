"""Where a task's change can be seen over HTTP: a temporary site per task.

A provider finds the site that serves a project's main checkout ("before"),
makes a temporary one for a task's worktree ("after"), and removes it. With
the `none` provider there are no sites; HTTP checks and screenshots are
skipped and every other phase runs as usual.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..config import Settings


class VhostError(RuntimeError):
    pass


@dataclass(frozen=True)
class Site:
    host: str
    php: Path | None = None  # the PHP CLI matching the site's PHP version, when the provider knows it
    detail: Any = None       # the provider's own data about the site


class Provider(Protocol):
    name: str

    def find(self, project_dir: Path) -> Site | None: ...
    def create(self, site: Site, task_id: str, worktree: Path) -> str: ...
    def remove(self, task_id: str) -> None: ...
    def doctor(self) -> list[tuple[bool, str, str]]: ...


def get(settings: Settings) -> Provider:
    from . import devcrate, none
    if settings.vhost_provider == "devcrate":
        return devcrate.Devcrate(settings)
    if settings.vhost_provider == "none":
        return none.NoVhost()
    raise ValueError(f"unknown vhost provider: {settings.vhost_provider!r} (none or devcrate)")
