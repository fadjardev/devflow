"""Settings, read once from the environment (and `.env` beside the package)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
RUNTIME_DIR = REPO_ROOT / "runtime"


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    vault: Path
    projects: Path
    devcrate_root: Path
    telegram_token: str = ""
    allowed_chat_id: int = 0
    max_workers: int = 3
    max_bounces: int = 2
    opencode_url: str = "http://127.0.0.1:4096"
    opencode_model: str = ""
    claude_model: str = ""
    base_branch: str = "development"
    git_remote: str = "origin"
    branch_prefix: str = "devflow"
    overlap_days: int = 14
    link_paths: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    # Shell commands the worker may run (opencode patterns); everything else is refused.
    worker_shell_allow: list[str] = field(default_factory=list)
    unsafe_shell: bool = False  # allow every shell command except git; see docs/security.md
    before_unsafe_methods: bool = False  # run POST/PUT/DELETE checks against your checkout too

    @property
    def worktrees(self) -> Path:
        return self.projects / ".worktrees"

    @property
    def nginx_prefix(self) -> Path:
        return self.devcrate_root / "nginx"


def load() -> Settings:
    load_dotenv(REPO_ROOT / ".env")
    env = os.environ.get
    return Settings(
        vault=Path(env("VAULT_PATH", r"E:\dev\projects\1-project-knowledge")),
        projects=Path(env("PROJECTS_PATH", r"E:\dev\projects")),
        devcrate_root=Path(env("DEVCRATE_ROOT", r"E:\dev")),
        telegram_token=env("TELEGRAM_TOKEN", ""),
        allowed_chat_id=int(env("ALLOWED_CHAT_ID", "0") or 0),
        max_workers=int(env("MAX_WORKERS", "3")),
        max_bounces=int(env("MAX_BOUNCES", "2")),
        opencode_url=env("OPENCODE_URL", "http://127.0.0.1:4096").rstrip("/"),
        opencode_model=env("OPENCODE_MODEL", ""),
        claude_model=env("CLAUDE_MODEL", ""),
        base_branch=env("BASE_BRANCH", "development"),
        git_remote=env("GIT_REMOTE", "origin"),
        branch_prefix=env("BRANCH_PREFIX", "devflow"),
        overlap_days=int(env("OVERLAP_DAYS", "14")),
        link_paths=_list(env("LINK_PATHS", "vendor,.env,application/config/database.php,application/config/config.php")),
        deny_read=_list(env("DENY_READ", ".env,.env.*,application/config/database.php,**/*-key.pem")),
        worker_shell_allow=_list(env("WORKER_SHELL_ALLOW", "php -l *")),
        unsafe_shell=_bool(env("UNSAFE_SHELL", "")),
        before_unsafe_methods=_bool(env("BEFORE_UNSAFE_METHODS", "")),
    )
