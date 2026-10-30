"""Evidence that a change works, gathered without trusting the worker's word.

1. HTTP checks from the spec's ```devflow-checks block, run against the main
   checkout's vhost ("before") and the task's temporary vhost ("after").
2. `php -l` on every changed .php file, with the PHP version the vhost uses.
3. phpunit, when the worktree has both vendor/bin/phpunit and a phpunit.xml.
4. A headless Chromium screenshot of the first GET check on the "after" vhost.

Requests go to 127.0.0.1 with a Host header (and Chromium gets a resolver
rule), so neither the temporary vhost nor anything else needs a hosts entry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import checks as check_commands
from .checks import php as php_preset
from .checks.schema import BadChecks as BadChecks  # re-exported: callers catch verify.BadChecks
from .checks.schema import validate
from .config import CheckConfig, runtime_dir

log = logging.getLogger("devflow.verify")

_CHECKS = re.compile(r"```devflow-checks\s*\n(.*?)```", re.S)


@dataclass
class Check:
    path: str
    method: str = "GET"
    expect_status: int | None = 200
    contains: str = ""
    absent: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    data: dict[str, Any] | None = None


@dataclass
class Evidence:
    markdown: str
    ok: bool
    screenshot: Path | None = None


SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def check_path(path: str) -> str:
    """A check's path must be a path on the task's vhost: `/...`, never a URL, `//host`,
    or anything a URL parser could read as another host (`@host`, backslashes)."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise BadChecks(f"check path must start with a single '/': {path!r}")
    if "\\" in path or any(ch.isspace() or ord(ch) < 32 for ch in path):
        raise BadChecks(f"check path has a backslash, space or control character: {path!r}")
    parts = urlsplit(path)
    if parts.scheme or parts.netloc:
        raise BadChecks(f"check path is a URL, not a path: {path!r}")
    return path


def page_url(host: str, path: str) -> str:
    """The URL of `path` on `host`, refusing anything that would land on another host."""
    url = f"https://{host}{check_path(path)}"
    if urlsplit(url).hostname != host.lower():
        raise BadChecks(f"{path!r} would leave {host}")
    return url


def parse_checks(spec: str) -> list[Check]:
    m = _CHECKS.search(spec or "")
    if not m:
        return []
    try:
        items = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise BadChecks(f"the devflow-checks block is not valid JSON: {e}") from None
    validate(items)
    checks = [Check(**it) for it in items]
    for c in checks:
        check_path(c.path)
    return checks


async def _http(host: str, c: Check) -> tuple[int, str]:
    async with httpx.AsyncClient(base_url="https://127.0.0.1", verify=False, timeout=30,
                                 follow_redirects=False) as client:
        # Host goes last so a check can't aim at another site on the same server.
        headers = {k: v for k, v in c.headers.items() if k.lower() != "host"} | {"Host": host}
        r = await client.request(c.method, check_path(c.path), headers=headers, data=c.data)
        return r.status_code, r.text


def _judge(c: Check, status: int, body: str) -> list[str]:
    problems = []
    if c.expect_status is not None and status != c.expect_status:
        problems.append(f"status {status} != {c.expect_status}")
    if c.contains and c.contains not in body:
        problems.append(f"missing {c.contains!r}")
    if c.absent and c.absent in body:
        problems.append(f"still contains {c.absent!r}")
    return problems


async def http_checks(checks: list[Check], before_host: str | None, after_host: str,
                      unsafe_before: bool = False) -> tuple[str, bool]:
    """Run each check on the "after" vhost, and on "before" for comparison. "Before" serves the
    developer's main checkout and its database, so only safe methods go there unless the
    project opts in (`unsafe_before`)."""
    rows, ok = ["| Check | Before | After |", "| --- | --- | --- |"], True
    for c in checks:
        cells = []
        for host in (before_host, after_host):
            if not host:
                cells.append("n/a")
                continue
            if host == before_host and c.method.upper() not in SAFE_METHODS and not unsafe_before:
                cells.append("skipped (not run on your checkout)")
                continue
            try:
                status, body = await _http(host, c)
                problems = _judge(c, status, body)
                cells.append(f"{status} " + ("✅" if not problems else "❌ " + "; ".join(problems)))
            except httpx.HTTPError as e:
                problems = [str(e)]
                cells.append(f"error: {e}")
        ok = ok and not problems  # only the "after" result decides
        rows.append(f"| `{c.method} {c.path}` | {cells[0]} | {cells[1]} |")
    return "\n".join(rows), ok


async def screenshot(host: str, path: str, out: Path) -> Path | None:
    url = page_url(host, path)
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("playwright not installed; no screenshot")
        return None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=[f"--host-resolver-rules=MAP {host} 127.0.0.1"])
            page = await browser.new_page(ignore_https_errors=True, viewport={"width": 1366, "height": 900})
            await page.goto(url, wait_until="networkidle", timeout=45000)
            out.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out), full_page=False)
            await browser.close()
        return out
    except Exception:  # a screenshot is a nice-to-have; never fail verification over it
        log.warning("screenshot of https://%s%s failed", host, path, exc_info=True)
        return None


async def run(task_id: str, spec: str, wt: Path, changed: list[str], php: Path | None,
              before_host: str | None, after_host: str | None, unsafe_before: bool = False,
              env_allow: Iterable[str] = (), check_config: CheckConfig | None = None) -> Evidence:
    parts, ok = [], True
    try:
        checks = parse_checks(spec)
    except BadChecks as e:
        checks, ok = [], False
        parts.append(f"**HTTP checks**: refused, {e}")
    if checks and after_host:
        table, good = await http_checks(checks, before_host, after_host, unsafe_before)
        parts.append(f"**HTTP checks** (after = `{after_host}`)\n\n{table}")
        ok &= good
    elif not checks and ok:
        parts.append("**HTTP checks**: none in the spec")
    elif not checks:
        pass
    else:
        parts.append("**HTTP checks**: skipped — no temporary vhost")

    cfg = check_config or CheckConfig(preset="php")
    php = php_preset.find_php(php) if cfg.preset == "php" else php
    for r in await asyncio.to_thread(check_commands.run, cfg, wt, changed, php, env_allow):
        sep = ":\n" if r.text.startswith("```") else ": "
        parts.append(f"**{r.title}**{sep}{r.text}")
        ok &= r.ok
    if not cfg.preset and not cfg.lint and not cfg.test:
        parts.append("**lint and tests**: none configured for this project")

    shot = None
    first_get = next((c for c in checks if c.method.upper() == "GET"), None)
    if first_get and after_host:
        shot = await screenshot(after_host, first_get.path, runtime_dir() / "shots" / f"{task_id}.png")
        parts.append(f"**screenshot**: {'taken' if shot else 'failed (see bot log)'} — `{first_get.path}`")

    verdict = "PASS" if ok else "FAIL"
    return Evidence(f"#### Verification {verdict}\n\n" + "\n\n".join(parts), ok, shot)
