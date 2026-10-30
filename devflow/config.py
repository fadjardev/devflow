"""Settings: `devflow.toml` with a project registry, or the older `.env`.

devflow reads the first of:

1. the file given with `--config` or DEVFLOW_CONFIG;
2. `devflow.toml` in the user config folder (`devflow init` writes it);
3. `.env` beside the package plus the environment ("legacy" mode, as before
   v0.2: projects are the vault's `01-Projects` notes).

Secrets (TELEGRAM_TOKEN) may stay in the environment in every mode. A missing
required setting is an error that names it; nothing falls back to a path on
somebody else's machine.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import platformdirs
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
APP = "devflow"


class ConfigError(ValueError):
    pass


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP, appauthor=False, roaming=True))


def data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP, appauthor=False))


def default_config() -> Path:
    return config_dir() / "devflow.toml"


def runtime_dir() -> Path:
    """Logs, MCP configs, locks, screenshots. DEVFLOW_RUNTIME also reaches the MCP server
    processes devflow starts, so they write where the bot does."""
    return Path(os.environ.get("DEVFLOW_RUNTIME") or REPO_ROOT / "runtime")


def prompts_dir() -> Path:
    return Path(os.environ.get("DEVFLOW_PROMPTS") or Path(__file__).resolve().parent / "prompts")


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class CheckConfig:
    """Lint and test commands run in the worktree after the worker (see checks/)."""
    preset: str | None = None  # "php": php -l on changed files, then phpunit or pest
    lint: list[str] = field(default_factory=list)
    test: list[str] = field(default_factory=list)
    timeout: int = 600  # seconds per command


@dataclass(frozen=True)
class Project:
    """One `[projects.<name>]` entry. Unset overrides fall back to the global setting."""
    name: str
    path: Path
    base_branch: str | None = None
    link_paths: list[str] | None = None
    deny_read: list[str] | None = None
    worker_shell_allow: list[str] | None = None
    check_env_allow: list[str] | None = None
    checks: CheckConfig | None = None


@dataclass(frozen=True)
class Settings:
    vault: Path  # the notes folder; without a configured vault, devflow's own (vault_is_own)
    projects: Path | None = None  # the folder holding project checkouts, if there is one
    devcrate_root: Path | None = None
    vhost_provider: str = "devcrate"  # none | devcrate
    telegram_token: str = ""
    allowed_chat_id: int = 0
    allowed_user_ids: list[int] = field(default_factory=list)  # required when the chat is a group
    max_workers: int = 3
    max_bounces: int = 2
    opencode_url: str = "http://127.0.0.1:4096"
    opencode_model: str = ""
    claude_model: str = ""
    base_branch: str = "development"  # "" = the remote's default branch
    git_remote: str = "origin"
    branch_prefix: str = "devflow"
    overlap_days: int = 14
    link_paths: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    # Shell commands the worker may run (opencode patterns); everything else is refused.
    worker_shell_allow: list[str] = field(default_factory=list)
    unsafe_shell: bool = False  # allow every shell command except git; see docs/security.md
    before_unsafe_methods: bool = False  # run POST/PUT/DELETE checks against your checkout too
    check_env_allow: list[str] = field(default_factory=list)  # env vars lint/test commands may see
    share_code_via_telegram: bool = True  # False: /diff sends a summary and a local path instead
    checks: CheckConfig = field(default_factory=lambda: CheckConfig(preset="php"))
    registry: dict[str, Project] | None = None  # None: the projects are the vault's notes (legacy)
    vault_is_own: bool = False  # no vault configured: notes live in the runtime folder
    source: str = ""  # where these settings came from, for doctor

    @property
    def worktrees(self) -> Path:
        return (self.projects or runtime_dir()) / ".worktrees"

    def for_project(self, name: str) -> Settings:
        """These settings with the project's own overrides applied."""
        p = (self.registry or {}).get(name)
        if p is None:
            return self
        overrides = {f.name: getattr(p, f.name) for f in dataclasses.fields(p)
                     if f.name not in ("name", "path") and getattr(p, f.name) is not None}
        return dataclasses.replace(self, **overrides)


# -- legacy: .env and the environment -----------------------------------------------------

def from_env(env_file: Path | None = None) -> Settings:
    """The settings as devflow read them before v0.2, minus the fallbacks to the author's
    own paths: VAULT_PATH and PROJECTS_PATH must now be set."""
    load_dotenv(env_file or REPO_ROOT / ".env")
    env = os.environ.get
    missing = [k for k in ("VAULT_PATH", "PROJECTS_PATH") if not env(k)]
    if missing:
        raise ConfigError(f"{' and '.join(missing)} not set. Run `devflow init` to write {default_config()}, "
                          f"or set {', '.join(missing)} in {env_file or REPO_ROOT / '.env'}.")
    devcrate = env("DEVCRATE_ROOT", "")
    return Settings(
        vault=Path(env("VAULT_PATH", "")),
        projects=Path(env("PROJECTS_PATH", "")),
        devcrate_root=Path(devcrate) if devcrate else None,
        vhost_provider=env("VHOST_PROVIDER", "devcrate" if devcrate else "none"),
        telegram_token=env("TELEGRAM_TOKEN", ""),
        allowed_chat_id=int(env("ALLOWED_CHAT_ID", "0") or 0),
        allowed_user_ids=[int(u) for u in _list(env("ALLOWED_USER_IDS", ""))],
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
        share_code_via_telegram=_bool(env("SHARE_CODE_VIA_TELEGRAM", "true")),
        check_env_allow=_list(env("CHECK_ENV_ALLOW", "")),
        checks=CheckConfig(preset="php"),  # what devflow always ran
        source=f"legacy: {env_file or REPO_ROOT / '.env'} and the environment",
    )


# -- devflow.toml ---------------------------------------------------------------------------

DEFAULT_DENY_READ = [".env", ".env.*", "**/*.pem", "**/*.key", "**/id_rsa*", "**/id_ed25519*"]


class _Table:
    """One TOML table, read key by key with type checks; unknown keys are errors (typos)."""

    def __init__(self, data: dict[str, Any], where: str):
        self.data, self.where = data, where
        self.used: set[str] = set()

    def _get(self, key: str, kind: type | tuple[type, ...], default: Any) -> Any:
        self.used.add(key)
        if key not in self.data:
            return default
        value = self.data[key]
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ConfigError(f"{self.where}.{key} must be {_kind(kind)}, not {value!r}")
        return value

    def text(self, key: str, default: str = "") -> str:
        return str(self._get(key, str, default))

    def number(self, key: str, default: int) -> int:
        return int(self._get(key, int, default))

    def flag(self, key: str, default: bool) -> bool:
        return bool(self._get(key, bool, default))

    def texts(self, key: str, default: list[str] | None) -> list[str] | None:
        value = self._get(key, list, default)
        if value is not None and not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{self.where}.{key} must be a list of strings")
        return None if value is None else list(value)

    def path(self, key: str, base: Path) -> Path | None:
        value = self.text(key)
        return (base / Path(value).expanduser()).resolve() if value else None

    def table(self, key: str) -> _Table:
        return _Table(self._get(key, dict, {}), f"{self.where}.{key}" if self.where else key)

    def done(self) -> None:
        unknown = sorted(set(self.data) - self.used)
        if unknown:
            raise ConfigError(f"unknown setting{'s' if len(unknown) > 1 else ''} in {self.where or 'the top level'}: "
                              f"{', '.join(unknown)}")


def _kind(kind: type | tuple[type, ...]) -> str:
    names = {str: "a string", int: "a whole number", bool: "true or false", list: "a list", dict: "a table"}
    return " or ".join(names.get(k, k.__name__) for k in (kind if isinstance(kind, tuple) else (kind,)))


def _checks(table: _Table) -> CheckConfig:
    preset = table._get("preset", str, None)
    if preset not in (None, "php"):
        raise ConfigError(f"{table.where}.preset must be \"php\" or left out, not {preset!r}")
    cfg = CheckConfig(preset=preset, lint=table.texts("lint", []) or [], test=table.texts("test", []) or [],
                      timeout=table.number("timeout", 600))
    table.done()
    return cfg


def from_toml(path: Path) -> Settings:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    base = path.parent
    top = _Table(data, "")

    tg = top.table("telegram")
    token = tg.text("token") or os.environ.get("TELEGRAM_TOKEN", "") or ""
    chat_id = tg.number("chat_id", int(os.environ.get("ALLOWED_CHAT_ID", "0") or 0))
    user_ids = tg._get("user_ids", list, [])
    if not all(isinstance(u, int) for u in user_ids):
        raise ConfigError("telegram.user_ids must be a list of numbers")
    tg.done()

    paths = top.table("paths")
    vault = paths.path("vault", base)
    projects_root = paths.path("projects", base)
    runtime = paths.path("runtime", base)
    paths.done()

    agents = top.table("agents")
    git = top.table("git")
    safety = top.table("safety")
    vh = top.table("vhost")
    provider = vh.text("provider", "none")
    devcrate = vh.path("devcrate_root", base)
    vh.done()

    registry: dict[str, Project] = {}
    projects = top.table("projects")
    for name in list(projects.data):
        entry = projects.table(name)
        where = projects.where + "." + name
        p = entry.path("path", base)
        if p is None:
            raise ConfigError(f"{where}.path is required")
        registry[name] = Project(
            name=name, path=p,
            base_branch=entry._get("base_branch", str, None),
            link_paths=entry.texts("link_paths", None),
            deny_read=entry.texts("deny_read", None),
            worker_shell_allow=entry.texts("worker_shell_allow", None),
            check_env_allow=entry.texts("check_env_allow", None),
            checks=_checks(entry.table("checks")) if "checks" in entry.data else None,
        )
        entry.done()
    projects.done()

    if runtime is not None and not os.environ.get("DEVFLOW_RUNTIME"):
        os.environ["DEVFLOW_RUNTIME"] = str(runtime)
    elif not os.environ.get("DEVFLOW_RUNTIME"):
        os.environ["DEVFLOW_RUNTIME"] = str(data_dir() / "runtime")

    settings = Settings(
        vault=vault or runtime_dir() / "vault",
        vault_is_own=vault is None,
        projects=projects_root,
        devcrate_root=devcrate,
        vhost_provider=provider,
        telegram_token=token,
        allowed_chat_id=chat_id,
        allowed_user_ids=list(user_ids),
        max_workers=agents.number("max_workers", 3),
        max_bounces=agents.number("max_bounces", 2),
        opencode_url=agents.text("opencode_url", "http://127.0.0.1:4096").rstrip("/"),
        opencode_model=agents.text("opencode_model"),
        claude_model=agents.text("claude_model"),
        base_branch=git.text("base_branch"),
        git_remote=git.text("remote", "origin"),
        branch_prefix=git.text("branch_prefix", "devflow"),
        overlap_days=git.number("overlap_days", 14),
        link_paths=safety.texts("link_paths", []) or [],
        deny_read=safety.texts("deny_read", DEFAULT_DENY_READ) or [],
        worker_shell_allow=safety.texts("worker_shell_allow", []) or [],
        unsafe_shell=safety.flag("unsafe_shell", False),
        before_unsafe_methods=safety.flag("before_unsafe_methods", False),
        check_env_allow=safety.texts("check_env_allow", []) or [],
        share_code_via_telegram=safety.flag("share_code_via_telegram", True),
        checks=_checks(top.table("checks")),
        registry=registry,
        source=str(path),
    )
    for t in (agents, git, safety, top):
        t.done()
    if provider not in ("none", "devcrate"):
        raise ConfigError(f"vhost.provider must be none or devcrate, not {provider!r}")
    if provider == "devcrate" and devcrate is None:
        raise ConfigError("vhost.devcrate_root is required with provider = \"devcrate\"")
    return settings


def load(path: Path | None = None) -> Settings:
    """The settings from `path`, DEVFLOW_CONFIG, the user's devflow.toml, or legacy `.env`."""
    explicit = path or (Path(os.environ["DEVFLOW_CONFIG"]) if os.environ.get("DEVFLOW_CONFIG") else None)
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"config file not found: {explicit}")
        return from_toml(explicit)
    if default_config().is_file():
        return from_toml(default_config())
    return from_env()
