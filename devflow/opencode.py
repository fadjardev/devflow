"""opencode as the worker, driven over `opencode serve`'s HTTP API.

One server serves every worktree: each request carries `?directory=`. Each
session gets its own permission ruleset — allow/deny only, never "ask",
because nobody is at the terminal to answer and the session would hang.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import RUNTIME_DIR, Settings

log = logging.getLogger("devflow.opencode")

# Tools that reach the network or start a subagent. A subagent (`task`) runs in a session of
# its own, without this session's ruleset, so it would escape every rule below.
NO_TOOLS = ["webfetch", "websearch", "codesearch", "task"]


class OpencodeError(RuntimeError):
    pass


def config_file(settings: Settings) -> Path:
    """Extra opencode config (OPENCODE_CONFIG) registering devflow-mcp."""
    RUNTIME_DIR.mkdir(exist_ok=True)
    path = RUNTIME_DIR / "opencode.json"
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"devflow": {
            "type": "local",
            "command": [sys.executable, "-m", "devflow.mcp_server"],
            "environment": {"DEVFLOW_VAULT": str(settings.vault)},
            "enabled": True,
        }},
    }
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


def path_pattern(glob: str) -> str:
    """opencode matches read patterns against the absolute path, where `*` spans separators:
    `.env` -> `*.env`, `application/config/database.php` -> `*application*config*database.php`."""
    p = glob.replace("\\", "/")
    while p.startswith(("**/", "*/")):
        p = p.split("/", 1)[1]
    p = "*" + p.replace("/", "*")
    while "**" in p:
        p = p.replace("**", "*")
    return p


def ruleset(settings: Settings) -> list[dict[str, str]]:
    """The worker's permissions. opencode applies the LAST matching rule, so the order matters:
    every shell command is denied, then the project's allow-list opens specific commands, then
    git is denied again so no allow-list entry (not even `*`) can reopen it. devflow does all git."""
    rules = [{"permission": "read", "pattern": path_pattern(p), "action": "deny"} for p in settings.deny_read]
    rules.append({"permission": "bash", "pattern": "*", "action": "deny"})
    allow = ["*"] if settings.unsafe_shell else settings.worker_shell_allow
    rules += [{"permission": "bash", "pattern": p, "action": "allow"} for p in allow]
    rules += [{"permission": "bash", "pattern": p, "action": "deny"} for p in ("git", "git *", "*/git *", "*git.exe *")]
    rules += [{"permission": p, "pattern": "*", "action": "deny"} for p in NO_TOOLS]
    rules += [
        {"permission": "external_directory", "pattern": "*", "action": "deny"},
        {"permission": "question", "pattern": "*", "action": "deny"},
        {"permission": "doom_loop", "pattern": "*", "action": "deny"},
    ]
    return rules


class Client:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = settings.opencode_url
        self.http = httpx.AsyncClient(base_url=self.base, timeout=httpx.Timeout(30, read=None))
        self._server: subprocess.Popen | None = None

    async def alive(self) -> bool:
        try:
            return (await self.http.get("/path", timeout=3)).status_code == 200
        except httpx.HTTPError:
            return False

    async def ensure_server(self) -> None:
        if await self.alive():
            return
        exe = shutil.which("opencode")
        if not exe:
            raise OpencodeError("opencode not found on PATH")
        port = str(urlparse(self.base).port or 4096)
        env = {**os.environ, "OPENCODE_CONFIG": str(config_file(self.settings))}
        out = open(RUNTIME_DIR / "opencode-serve.log", "ab")
        log.info("starting opencode serve on port %s (output in %s)", port, RUNTIME_DIR / "opencode-serve.log")
        self._server = subprocess.Popen([exe, "serve", "--port", port], env=env, stdout=out, stderr=out,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(60):
            if await self.alive():
                return
            await asyncio.sleep(1)
        log.error("opencode serve did not answer on %s within 60s; see opencode-serve.log", self.base)
        raise OpencodeError(f"opencode serve did not come up on {self.base}")

    async def create_session(self, directory: Path, title: str) -> str:
        r = await self.http.post("/session", params={"directory": str(directory)},
                                 json={"title": title, "permission": ruleset(self.settings)})
        r.raise_for_status()
        return r.json()["id"]

    async def _watch_permissions(self, session: str, directory: Path, on_denied) -> None:
        """Anything still set to "ask" would wait forever for a human at a terminal.
        Reject it, tell the worker why, and report it."""
        params = {"directory": str(directory)}
        failures = 0
        while True:
            await asyncio.sleep(3)
            try:
                pending = (await self.http.get("/permission", params=params, timeout=10)).json()
                failures = 0
            except (httpx.HTTPError, ValueError) as e:
                failures += 1
                if failures in (3, 20):  # once when it starts looking real, once more when it persists
                    log.warning("permission poll failing (%d in a row): %s", failures, e)
                continue
            for req in pending:
                if req.get("sessionID") != session:
                    continue
                # A crash here would kill the watchdog silently and leave the worker waiting forever.
                try:
                    await self.http.post(f"/permission/{req['id']}/reply", params=params, timeout=10,
                                         json={"reply": "reject", "message": "devflow: not permitted for unattended workers"})
                    if on_denied:
                        await on_denied(req.get("permission", "?"), req.get("patterns") or [])
                except Exception:
                    log.exception("could not reject permission request %s", req.get("id"))

    async def prompt(self, session: str, directory: Path, text: str, timeout: float, on_denied=None) -> str:
        """Send one prompt and wait for the assistant to finish; returns its final text."""
        body: dict = {"parts": [{"type": "text", "text": text}]}
        if self.settings.opencode_model:
            provider, _, model = self.settings.opencode_model.partition("/")
            body["model"] = {"providerID": provider, "modelID": model}
        watchdog = asyncio.create_task(self._watch_permissions(session, directory, on_denied))
        try:
            r = await asyncio.wait_for(
                self.http.post(f"/session/{session}/message", params={"directory": str(directory)}, json=body),
                timeout,
            )
        except TimeoutError:
            await self.abort(session, directory)
            log.error("worker timed out after %ds; session %s aborted", timeout, session)
            raise OpencodeError(f"worker still busy after {int(timeout // 60)} minutes; aborted") from None
        finally:
            watchdog.cancel()
        r.raise_for_status()
        data = r.json()
        err = (data.get("info") or {}).get("error")
        if err:
            log.error("opencode session %s ended with an error: %s", session, json.dumps(err)[:1000])
            raise OpencodeError(f"opencode: {json.dumps(err)[:500]}")
        texts = [p.get("text", "") for p in data.get("parts", []) if p.get("type") == "text"]
        return "\n".join(t for t in texts if t).strip()

    async def abort(self, session: str, directory: Path) -> None:
        try:
            await self.http.post(f"/session/{session}/abort", params={"directory": str(directory)}, timeout=10)
        except httpx.HTTPError as e:
            log.warning("could not abort opencode session %s: %s", session, e)

    async def dispose(self, directory: Path) -> None:
        """opencode keeps an instance, with a file watcher, for every directory it has worked in.
        On Windows the watcher's open handle stops the folder from being deleted ("Permission
        denied" from `git worktree remove`), so dispose of it before removing a worktree."""
        try:
            await self.http.post("/instance/dispose", params={"directory": str(directory)}, timeout=15)
        except httpx.HTTPError as e:  # no server running means no watcher either
            log.info("no opencode instance to dispose for %s (%s)", directory, e)

    async def close(self) -> None:
        await self.http.aclose()
        if self._server and self._server.poll() is None:
            # opencode.CMD wraps node: kill the whole tree, not just cmd.exe.
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self._server.pid)], capture_output=True)
