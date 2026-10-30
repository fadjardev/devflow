"""Lint and test commands run in a task's worktree after the worker, per project.

A project lists its own commands (`lint`, `test`), or uses a preset. The `php`
preset is what devflow always did: `php -l` on the changed PHP files with the
site's PHP version, then phpunit or pest when the project has them. Every
command runs with a minimal environment and a timeout that kills its whole
process tree (PRD SR10).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .. import platform
from ..config import CheckConfig
from . import php as php_preset

PRESETS = ("php",)


@dataclass(frozen=True)
class Result:
    title: str
    text: str
    ok: bool


def describe(cfg: CheckConfig) -> str:
    """What the planner is told devflow will run, so it plans tests that will actually run."""
    lines = []
    if cfg.preset == "php":
        lines.append("- `php -l` on every changed `.php` file, with the site's PHP version")
        lines.append("- phpunit or pest, if the project has `vendor/bin/phpunit` + `phpunit.xml` or `vendor/bin/pest`")
    lines += [f"- lint: `{c}`" for c in cfg.lint]
    lines += [f"- test: `{c}`" for c in cfg.test]
    return "\n".join(lines) or "- none: this project has no lint or test commands configured"


def _command(kind: str, command: str, wt: Path, env: dict[str, str], timeout: int) -> Result:
    p = platform.run_shell(command, cwd=wt, env=env, timeout=timeout)
    if p.returncode == 0:
        return Result(f"{kind} `{command}`", "passed", True)
    tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-15:])
    return Result(f"{kind} `{command}`", f"failed (exit {p.returncode})\n```\n{tail}\n```", False)


def run(cfg: CheckConfig, wt: Path, changed: list[str], php: Path | None,
        env_allow: Iterable[str] = ()) -> list[Result]:
    results = []
    if cfg.preset == "php":
        lint, good = php_preset.php_lint(php, wt, changed, env_allow)
        results.append(Result("php -l", lint, good))
        unit = php_preset.phpunit(php, wt, env_allow)
        if unit:
            results.append(Result(unit[2], unit[0], unit[1]))
    env = platform.minimal_env(env_allow)
    results += [_command("lint", c, wt, env, cfg.timeout) for c in cfg.lint]
    results += [_command("test", c, wt, env, cfg.timeout) for c in cfg.test]
    return results
