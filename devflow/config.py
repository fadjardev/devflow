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
import json
import os
import re
import tomllib
import zoneinfo
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
class Caps:
    """Spending caps for one window; None = no cap."""
    daily_usd: float | None = None
    monthly_usd: float | None = None
    per_task_usd: float | None = None
    daily_tokens: int | None = None
    monthly_tokens: int | None = None

    def any(self) -> bool:
        return any(v is not None for v in dataclasses.astuple(self))


ON_EXCEED = ("warn", "pause-new", "stop")


@dataclass(frozen=True)
class BudgetConfig:
    """User budgets and provider-limit handling (PRD §5.7)."""
    caps: Caps = field(default_factory=Caps)
    backends: dict[str, Caps] = field(default_factory=dict)  # claude, opencode: their own caps
    warn_at: list[float] = field(default_factory=lambda: [0.5, 0.8])
    on_exceed: str = "warn"   # warn | pause-new | stop
    timezone: str = ""        # IANA name; "" = this machine's
    auto_resume: bool = False  # resume tasks a provider limit stopped once it resets


FREE_SUFFIX = ":free"  # how OpenRouter and the gateways that pass its ids on name a free-tier model


@dataclass(frozen=True)
class GatewayConfig:
    """An OpenAI-compatible AI gateway (OmniRoute, 9Router, LiteLLM, OpenRouter) that devflow
    registers as an opencode provider for the worker. The key stays in the environment."""
    base_url: str                       # e.g. http://127.0.0.1:20128/v1
    models: list[str]                   # model ids as the gateway names them; the first is the default
    provider_id: str = "gateway"        # the worker model is "<provider_id>/<model>"
    name: str = "AI gateway"
    api_key_env: str = ""               # the environment variable that holds the key, if one is needed
    free_only: bool = False             # every worker model must be a free tier (ADR 0012)
    free_combos: list[str] = field(default_factory=list)  # gateway combos whose models are checked to be free

    def model(self, model_id: str) -> str:
        return f"{self.provider_id}/{model_id}"

    def allowed(self, worker_model: str) -> bool:
        """With free_only: "<provider_id>/<id>" where id ends in ":free" or is a listed combo."""
        provider, _, model_id = worker_model.partition("/")
        return provider == self.provider_id and (model_id.endswith(FREE_SUFFIX) or model_id in self.free_combos)


@dataclass(frozen=True)
class DashboardConfig:
    """The local web dashboard (ADR 0011). It only ever listens on 127.0.0.1."""
    enabled: bool = False   # also start it with `devflow run` (beside Telegram)
    port: int = 8765


@dataclass(frozen=True)
class Project:
    """One `[projects.<name>]` entry. Unset overrides fall back to the global setting."""
    name: str
    path: Path
    opencode_model: str | None = None
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
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    gateway: GatewayConfig | None = None  # the worker's models through an AI gateway
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
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

    def real(self, key: str) -> float | None:
        value = self._get(key, (int, float), None)
        return None if value is None else float(value)

    def maybe_number(self, key: str) -> int | None:
        value = self._get(key, int, None)
        return None if value is None else int(value)

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


def _caps(t: _Table) -> Caps:
    return Caps(daily_usd=t.real("daily_usd"), monthly_usd=t.real("monthly_usd"), per_task_usd=t.real("per_task_usd"),
                daily_tokens=t.maybe_number("daily_tokens"), monthly_tokens=t.maybe_number("monthly_tokens"))


def _budget(top: _Table) -> BudgetConfig:
    b = top.table("budget")
    caps = _caps(b)
    backends = {}
    per = b.table("backend")
    for name in list(per.data):
        if name not in ("claude", "opencode"):
            raise ConfigError(f"budget.backend.{name}: the backends are claude and opencode")
        t = per.table(name)
        backends[name] = _caps(t)
        t.done()
    per.done()
    warn_at = b._get("warn_at", list, [0.5, 0.8])
    if not all(isinstance(w, int | float) and 0 < w < 1 for w in warn_at):
        raise ConfigError("budget.warn_at must be fractions between 0 and 1, e.g. [0.5, 0.8]")
    on_exceed = b.text("on_exceed", "warn")
    if on_exceed not in ON_EXCEED:
        raise ConfigError(f"budget.on_exceed must be one of {', '.join(ON_EXCEED)}, not {on_exceed!r}")
    tz = b.text("timezone")
    if tz:
        try:
            zoneinfo.ZoneInfo(tz)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f"budget.timezone: unknown time zone {tz!r}") from None
    b.done()
    lim = top.table("limits")
    auto = lim.flag("auto_resume", False)
    lim.done()
    return BudgetConfig(caps, backends, sorted(float(w) for w in warn_at), on_exceed, tz, auto)


_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,100}$")


def _gateway(top: _Table) -> GatewayConfig | None:
    if not top.data.get("gateway"):  # absent, or an empty table with every key commented out
        top.used.add("gateway")
        return None
    g = top.table("gateway")
    url = g.text("base_url").rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConfigError("gateway.base_url must be an http(s) URL, e.g. \"http://127.0.0.1:20128/v1\"")
    models = g.texts("models", []) or []
    if not models or not all(m.strip() for m in models):
        raise ConfigError("gateway.models must list at least one model id")
    provider_id = g.text("provider_id", "gateway")
    if not _PROVIDER_RE.match(provider_id):
        raise ConfigError(f"gateway.provider_id must be lowercase letters, digits, - or _, not {provider_id!r}")
    key_env = g.text("api_key_env")
    if key_env and not _ENV_RE.match(key_env):
        raise ConfigError(f"gateway.api_key_env is the NAME of an environment variable, not {key_env!r}")
    free_only = g.flag("free_only", False)
    free_combos = g.texts("free_combos", []) or []
    if free_combos and not free_only:
        raise ConfigError("gateway.free_combos only means something with free_only = true")
    cfg = GatewayConfig(base_url=url, models=models, provider_id=provider_id, name=g.text("name", "AI gateway"),
                        api_key_env=key_env, free_only=free_only, free_combos=free_combos)
    g.done()
    if free_only:
        paid = [m for m in models if not cfg.allowed(cfg.model(m))]
        if paid:
            raise ConfigError(f"gateway.free_only: not a free model ({', '.join(paid)}); a free model id ends in "
                              f"\"{FREE_SUFFIX}\", and a combo must be listed in gateway.free_combos")
    return cfg


def _check_free(settings: Settings) -> None:
    """free_only: the worker model of every project must be one of the gateway's free ones."""
    gw = settings.gateway
    if gw is None or not gw.free_only:
        return
    names = ["(global)"] + list(settings.registry or {})
    for name in names:
        model = settings.opencode_model if name == "(global)" else settings.for_project(name).opencode_model
        if not gw.allowed(model):
            where = "agents.opencode_model" if name == "(global)" else f"projects.{name}.opencode_model"
            raise ConfigError(f"{where} = {model!r} is not allowed with gateway.free_only: use "
                              f"\"{gw.provider_id}/<model>{FREE_SUFFIX}\" or a combo from gateway.free_combos")


def _dashboard(top: _Table) -> DashboardConfig:
    d = top.table("dashboard")
    port = d.number("port", 8765)
    if not 1024 <= port <= 65535:
        raise ConfigError(f"dashboard.port must be between 1024 and 65535, not {port}")
    cfg = DashboardConfig(enabled=d.flag("enabled", False), port=port)
    d.done()
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
            opencode_model=entry._get("opencode_model", str, None),
            base_branch=entry._get("base_branch", str, None),
            link_paths=entry.texts("link_paths", None),
            deny_read=entry.texts("deny_read", None),
            worker_shell_allow=entry.texts("worker_shell_allow", None),
            check_env_allow=entry.texts("check_env_allow", None),
            checks=_checks(entry.table("checks")) if "checks" in entry.data else None,
        )
        entry.done()
    projects.done()

    gateway = _gateway(top)
    model = agents.text("opencode_model") or (gateway.model(gateway.models[0]) if gateway else "")

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
        opencode_model=model,
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
        budget=_budget(top),
        gateway=gateway,
        dashboard=_dashboard(top),
        registry=registry,
        source=str(path),
    )
    for t in (agents, git, safety, top):
        t.done()
    _check_free(settings)
    if provider not in ("none", "devcrate"):
        raise ConfigError(f"vhost.provider must be none or devcrate, not {provider!r}")
    if provider == "devcrate" and devcrate is None:
        raise ConfigError("vhost.devcrate_root is required with provider = \"devcrate\"")
    return settings


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def append_project(config_file: Path, name: str, checkout: Path) -> Settings:
    """Add `[projects.<name>]` to the end of a devflow.toml and return the settings it now gives.
    The file is only appended to, so comments and layout stay; if the result doesn't load, the
    file is put back as it was."""
    before = config_file.read_text(encoding="utf-8")
    key = name if _BARE_KEY.match(name) else json.dumps(name)
    path = json.dumps(checkout.resolve().as_posix())  # a JSON string is a valid TOML basic string
    sep = "" if before.endswith("\n") or not before else "\n"
    config_file.write_text(f"{before}{sep}\n[projects.{key}]\npath = {path}\n", encoding="utf-8")
    try:
        return from_toml(config_file)
    except ConfigError:
        config_file.write_text(before, encoding="utf-8")
        raise


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
