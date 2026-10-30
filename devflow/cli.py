"""The `devflow` command: run the bot, write a config, check the setup.

    devflow [--config FILE] [run]       run the Telegram bot
    devflow [--config FILE] doctor [--fix]
    devflow init                        write devflow.toml interactively
    devflow migrate-env                 write devflow.toml from the old .env
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__, config, projects, security, vhosts

# -- doctor ---------------------------------------------------------------------------------

def doctor(settings: config.Settings, fix: bool = False, config_file: Path | None = None) -> int:
    ok = True

    def line(good: bool, what: str, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"  {'ok ' if good else 'BAD'}  {what}" + (f" - {detail}" if detail else ""))

    print("devflow doctor")
    print(f"  --   settings: {settings.source}")
    line(bool(settings.telegram_token), "Telegram token set")
    line(bool(settings.allowed_chat_id), "Telegram chat id set")
    if settings.allowed_chat_id < 0:
        line(bool(settings.allowed_user_ids), "user ids set for the group chat")
    private = [("runtime folder", config.runtime_dir())]
    private.append(("config file", config_file) if config_file else (".env", config.REPO_ROOT / ".env"))
    for what, path in private:
        loose = security.loose_permissions(path)
        if loose and fix:
            security.restrict(path)
            loose = security.loose_permissions(path)
        line(not loose, f"{what} private", f"{path}: {loose}; run: devflow doctor --fix" if loose else "")
    if settings.vault_is_own:
        print(f"  --   no vault configured: notes in {settings.vault}")
    else:
        line((settings.vault / "01-Projects").is_dir() or settings.registry is not None, "vault", str(settings.vault))
    if settings.projects is not None:
        line(settings.projects.is_dir(), "projects folder", str(settings.projects))
    line(not settings.unsafe_shell, "worker shell restricted",
         "unsafe_shell is on: the worker can run any command except git" if settings.unsafe_shell
         else f"allowed: {', '.join(settings.worker_shell_allow) or 'nothing'}")
    for exe in ("claude", "opencode", "git"):
        line(bool(shutil.which(exe)), f"{exe} on PATH", shutil.which(exe) or "")
    try:
        sites: vhosts.Provider | None = vhosts.get(settings)
    except (ValueError, vhosts.VhostError) as e:
        sites = None
        line(False, "vhost provider", str(e))
    for good, what, detail in sites.doctor() if sites else []:
        line(good, what, detail)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        line(True, "playwright headless chromium launches")
    except Exception as e:
        first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        line(False, "playwright headless chromium", f"{first}; for screenshots run: "
             f"\"{sys.executable}\" -m playwright install chromium-headless-shell")

    print("\nprojects")
    names = projects.names(settings)
    if not names:
        line(False, "projects", "none configured")
    for name in names:
        try:
            pdir = projects.directory(settings, name)
        except ValueError as e:
            line(settings.registry is None, name, str(e))  # a registry entry must work; a vault note may be a non-code project
            continue
        site = sites.find(pdir) if sites else None
        print(f"  ok   {name}: {pdir}" + (f", https://{site.host}" + ("" if site.php else " (no PHP CLI found)")
                                         if site else ", no vhost (HTTP checks skipped)"))
    return 0 if ok else 1


# -- writing devflow.toml -------------------------------------------------------------------

def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, Path):
        v = str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # a JSON string is a valid TOML basic string
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"can't write {v!r} as TOML")


def render_toml(tables: dict[str, dict[str, Any]]) -> str:
    """`{"telegram": {...}, "projects.app": {...}}` as TOML, empty tables and None values skipped."""
    out = ["# devflow settings. Reference: https://github.com/fadjardev/devflow#configuration", ""]
    for name, values in tables.items():
        kept = {k: v for k, v in values.items() if v is not None}
        if not kept:
            continue
        key = ".".join(json.dumps(part) if not part.replace("_", "").replace("-", "").isalnum() else part
                       for part in name.split(".", 1)) if name.startswith("projects.") else name
        out.append(f"[{key}]")
        out += [f"{k} = {_toml_value(v)}" for k, v in kept.items()]
        out.append("")
    return "\n".join(out)


def _write(path: Path, text: str, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(f"{path} already exists; use --force to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    security.restrict(path)  # it may hold the bot token
    config.from_toml(path)  # what we wrote must load
    print(f"wrote {path}")


def migrate_env(target: Path, force: bool = False) -> None:
    """devflow.toml from the current .env: the same projects (from the vault notes), the same
    paths and safety settings, and the runtime folder kept where it is."""
    s = config.from_env()
    registry: dict[str, dict[str, Any]] = {}
    for name in projects.names(s):
        try:
            registry[f"projects.{name}"] = {"path": projects.checkout(s, name)}
        except ValueError as e:
            print(f"skipped {name}: {e}")
    tables: dict[str, dict[str, Any]] = {
        "telegram": {"token": s.telegram_token or None, "chat_id": s.allowed_chat_id or None,
                     "user_ids": s.allowed_user_ids or None},
        "paths": {"vault": s.vault, "projects": s.projects, "runtime": config.REPO_ROOT / "runtime"},
        "agents": {"max_workers": s.max_workers, "max_bounces": s.max_bounces, "opencode_url": s.opencode_url,
                   "opencode_model": s.opencode_model or None, "claude_model": s.claude_model or None},
        "git": {"remote": s.git_remote, "base_branch": s.base_branch, "branch_prefix": s.branch_prefix,
                "overlap_days": s.overlap_days},
        "safety": {"link_paths": s.link_paths, "deny_read": s.deny_read, "worker_shell_allow": s.worker_shell_allow,
                   "unsafe_shell": s.unsafe_shell, "check_env_allow": s.check_env_allow or None,
                   "before_unsafe_methods": s.before_unsafe_methods,
                   "share_code_via_telegram": s.share_code_via_telegram},
        "vhost": {"provider": s.vhost_provider, "devcrate_root": s.devcrate_root},
        **registry,
    }
    _write(target, render_toml(tables), force)
    print(f"devflow now reads {target}. The old .env is no longer read; remove it once the bot runs.")


def init(target: Path, ask: Callable[[str], str] = input, force: bool = False) -> None:
    """Ask for the essentials and write a starter devflow.toml."""
    def q(prompt: str, default: str = "") -> str:
        answer = ask(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
        return answer or default

    print("devflow init: a few questions; press Enter to accept the default in [brackets].")
    token = q("Telegram bot token from @BotFather (empty: set TELEGRAM_TOKEN in the environment instead)")
    chat = int(q("Telegram chat id that may use the bot (from @userinfobot)", "0") or 0)
    users = [int(u) for u in q("Telegram user ids allowed in that chat, comma-separated"
                               + (" (required for a group)" if chat < 0 else ""), "").split(",") if u.strip()]
    vault = q("Obsidian vault for task notes (empty: devflow keeps its own notes)")
    tables: dict[str, dict[str, Any]] = {
        "telegram": {"token": token or None, "chat_id": chat or None, "user_ids": users or None},
        "paths": {"vault": vault or None},
        "safety": {"worker_shell_allow": []},
        "vhost": {"provider": "none"},
    }
    print("Projects: a name and the path of its git checkout. Empty name to finish.")
    while True:
        name = q("  project name")
        if not name:
            break
        path = Path(q(f"  path of {name}")).expanduser()
        if not (path / ".git").exists():
            print(f"  {path} is not a git checkout; skipped")
            continue
        tables[f"projects.{name}"] = {"path": path.resolve()}
    _write(target, render_toml(tables), force)
    print("Next: devflow doctor, then devflow run.")


# -- entry point ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="devflow", description="Supervised AI coding workflow driven from Telegram.")
    parser.add_argument("--version", action="version", version=f"devflow {__version__}")
    parser.add_argument("--config", type=Path, help="devflow.toml to use (default: DEVFLOW_CONFIG, then "
                                                   f"{config.default_config()}, then the legacy .env)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the Telegram bot (the default)")
    d = sub.add_parser("doctor", help="check the setup")
    d.add_argument("--fix", action="store_true", help="make the config and runtime folder private")
    for name, what in (("init", "write devflow.toml interactively"), ("migrate-env", "write devflow.toml from .env")):
        c = sub.add_parser(name, help=what)
        c.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = parser.parse_args(argv)
    target = args.config or config.default_config()

    try:
        if args.command == "init":
            return init(target, force=args.force)
        if args.command == "migrate-env":
            return migrate_env(target, force=args.force)
        settings = config.load(args.config)
    except config.ConfigError as e:
        raise SystemExit(f"devflow: {e}") from None
    if args.command == "doctor":
        used = Path(settings.source) if settings.registry is not None else None  # the TOML that was read
        raise SystemExit(doctor(settings, fix=args.fix, config_file=used))
    from .bot import main as run_bot
    run_bot(settings)


if __name__ == "__main__":
    main(sys.argv[1:])
