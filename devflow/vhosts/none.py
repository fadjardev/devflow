"""No web server: tasks get no temporary site, and HTTP checks are skipped."""

from __future__ import annotations

from pathlib import Path

from . import Site, VhostError


class NoVhost:
    name = "none"

    def find(self, project_dir: Path) -> Site | None:
        return None

    def create(self, site: Site, task_id: str, worktree: Path) -> str:
        raise VhostError("no vhost provider is configured")

    def remove(self, task_id: str) -> None:
        return None

    def doctor(self) -> list[tuple[bool, str, str]]:
        return [(True, "vhost provider", "none: HTTP checks and screenshots are skipped")]
