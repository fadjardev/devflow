"""The php preset: `php -l` on changed files, then phpunit or pest when the project has them."""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path

from .. import platform


def find_php(site_php: Path | None) -> Path | None:
    """The site's PHP CLI (devcrate knows its version), else `php` on PATH."""
    if site_php:
        return site_php
    found = shutil.which("php")
    return Path(found) if found else None


def php_lint(php: Path | None, wt: Path, files: list[str], env_allow: Iterable[str] = ()) -> tuple[str, bool]:
    php_files = [f for f in files if f.endswith(".php")]
    if not php_files:
        return "no PHP files changed", True
    if not php:
        return "skipped: no php.exe for this vhost's version", True
    bad = []
    env = platform.minimal_env(env_allow)
    for f in php_files:
        # The full path, so a file named "-d.php" can't be read as an option.
        p = platform.run([str(php), "-l", str(wt / f)], cwd=wt, env=env, timeout=60)
        if p.returncode != 0:
            bad.append(f"- `{f}`: {(p.stdout + p.stderr).strip()[:300]}")
    return ("all clean" if not bad else "\n".join(bad)), not bad


def phpunit(php: Path | None, wt: Path, env_allow: Iterable[str] = ()) -> tuple[str, bool, str] | None:
    """phpunit (with a phpunit.xml) or pest, whichever the project has; (output, ok, name)."""
    for name, config in (("phpunit", ("phpunit.xml", "phpunit.xml.dist")), ("pest", ())):
        runner = wt / "vendor" / "bin" / name
        if not (php and runner.exists() and (not config or any((wt / n).exists() for n in config))):
            continue
        p = platform.run([str(php), str(runner), "--colors=never"], cwd=wt, env=platform.minimal_env(env_allow),
                         timeout=900)
        tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-15:])
        return f"```\n{tail}\n```", p.returncode == 0, name
    return None
