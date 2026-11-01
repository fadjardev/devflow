"""opencode as the worker, driven over `opencode serve`'s HTTP API.

Two API generations are spoken (ADR 0013), chosen by what the server answers:

- **v1** (`opencode-ai` 1.x): routes at the root, the directory as `?directory=`, a synchronous
  `POST /session/{id}/message`, permissions as `{permission, pattern, action}`.
- **v2** (`@opencode/cli` 2.x): routes under `/api`, HTTP Basic auth, the directory as the
  session's `location`, an asynchronous `prompt` that devflow waits on, permissions as
  `{action, resource, effect}`.

Each session gets its own permission ruleset: allow/deny only, never "ask", because nobody is at
the terminal to answer and the session would hang. A watchdog rejects anything that still asks.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import gateway, platform, security, usage
from .config import GatewayConfig, Settings, runtime_dir
from .usage import Limit, Usage

log = logging.getLogger("devflow.opencode")

# Tools that reach the network or start a subagent. A subagent (`task`) runs in a session of
# its own, without this session's ruleset, so it would escape every rule below.
NO_TOOLS = ["webfetch", "websearch", "codesearch", "task"]

# v1 permission names that v2 renamed, and v2 actions with no v1 counterpart that devflow denies:
# `execute` runs code outside the shell rules, and image generation calls a paid model.
V2_ACTION = {"bash": "shell", "task": "subagent"}
V2_ONLY_DENY = ["execute", "experimental_image_generate"]
V1_ONLY = {"doom_loop", "codesearch"}  # v2 has no such action; nothing to translate

WAIT_POLL = 2.0  # seconds between looks at a v2 session while the worker runs
LIMIT_RETRIES = 3  # v2 attempts on a rate-limited call before devflow stops the run
LIMIT_MIN_PAUSE = dt.timedelta(minutes=15)  # the shortest wait before a limited task resumes


class OpencodeError(RuntimeError):
    pass


class OpencodeLimit(OpencodeError):
    """The model provider refused the worker's run: HTTP 429 or an exhausted quota."""

    def __init__(self, message: str, limit: Limit):
        super().__init__(message)
        self.limit = limit


# -- the server's config -------------------------------------------------------------------------

def config_file(settings: Settings, api: int = 1) -> Path:
    """Extra opencode config (OPENCODE_CONFIG). It registers no devflow-mcp: `opencode serve`
    shares one MCP config across all sessions, so it couldn't be bound to one task (ADR 0003).
    The worker reports back in its reply instead.

    With a `[gateway]`, it registers the gateway as an OpenAI-compatible provider (ADR 0010),
    in the config shape of the server's API generation. The key is written as an `{env:NAME}`
    reference, never as its value."""
    runtime_dir().mkdir(exist_ok=True)
    path = runtime_dir() / "opencode.json"
    cfg: dict[str, object] = {"$schema": "https://opencode.ai/config.json"}
    if settings.gateway:
        if api >= 2:
            cfg["providers"] = gateway_provider_v2(settings.gateway)
        else:
            cfg["provider"] = gateway_provider(settings.gateway)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


def gateway_provider(gw: GatewayConfig) -> dict[str, object]:
    options: dict[str, str] = {"baseURL": gw.base_url}
    if gw.api_key_env:
        options["apiKey"] = "{env:" + gw.api_key_env + "}"
    return {gw.provider_id: {
        "npm": "@ai-sdk/openai-compatible",
        "name": gw.name,
        "options": options,
        "models": {m: {"name": m} for m in gw.models},
    }}


def gateway_provider_v2(gw: GatewayConfig) -> dict[str, object]:
    """v2 renamed `provider`→`providers`, `npm`→`package` (AI SDK packages take `aisdk:`) and
    `options`→`settings`."""
    settings: dict[str, str] = {"baseURL": gw.base_url}
    if gw.api_key_env:
        settings["apiKey"] = "{env:" + gw.api_key_env + "}"
    return {gw.provider_id: {
        "package": "aisdk:@ai-sdk/openai-compatible",
        "name": gw.name,
        "settings": settings,
        "models": {m: {"name": m} for m in gw.models},
    }}


# -- the worker's permissions --------------------------------------------------------------------

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


def ruleset_v2(rules: list[dict[str, str]]) -> list[dict[str, str]]:
    """The same rules for a v2 server: `{action, resource, effect}`, renamed actions, in the
    same order (v2 also applies the last match). v2 allows `external_directory` by default;
    the explicit deny above keeps the worker inside its worktree."""
    out = [{"action": V2_ACTION.get(r["permission"], r["permission"]), "resource": r["pattern"], "effect": r["action"]}
           for r in rules if r["permission"] not in V1_ONLY]
    out += [{"action": a, "resource": "*", "effect": "deny"} for a in V2_ONLY_DENY]
    return out


# -- the client ------------------------------------------------------------------------------------

def password_file() -> Path:
    return runtime_dir() / "opencode.password"


def server_password() -> str:
    """The password devflow starts `opencode serve` with, kept (owner-only) so a devflow
    restart can still talk to the server it left running."""
    p = password_file()
    if p.is_file():
        pw = p.read_text(encoding="utf-8").strip()
        if len(pw) >= 32:
            return pw
    runtime_dir().mkdir(parents=True, exist_ok=True)
    pw = secrets.token_urlsafe(32)
    p.write_text(pw, encoding="utf-8")
    security.restrict(p)
    return pw


def cli_major(exe: str) -> int:
    """The installed opencode's major version (`opencode v2.2.5` -> 2, `1.19.5` -> 1)."""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return 1
    m = re.search(r"(\d+)\.\d+", out or "")
    return int(m.group(1)) if m else 1


class Client:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = settings.opencode_url
        self.password = server_password()
        self.http = httpx.AsyncClient(base_url=self.base, timeout=httpx.Timeout(30, read=None),
                                      auth=("opencode", self.password))
        self._server: subprocess.Popen | None = None
        self.api: int | None = None  # 1 or 2 once a server answered

    async def _probe(self) -> int | None:
        """Which API the server on opencode_url speaks; None when nothing usable answers.
        Raises when a v2 server answers that devflow has no password for."""
        try:
            r = await self.http.get("/api/info", timeout=3)
            if r.status_code == 401:
                raise OpencodeError(
                    f"an opencode server on {self.base} asks for a password devflow doesn't have: it was "
                    f"started outside devflow. Stop it, and devflow starts its own.")
            if r.status_code == 200 and "json" in r.headers.get("content-type", "") and "version" in r.json():
                return 2
            r = await self.http.get("/path", timeout=3)  # v1; on v2 this is the web app's HTML
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                return 1
        except (httpx.HTTPError, ValueError):
            return None
        return None

    async def alive(self) -> bool:
        try:
            api = await self._probe()
        except OpencodeError:
            return False
        if api:
            self.api = api
        return api is not None

    async def ensure_server(self) -> None:
        api = await self._probe()
        if api:
            self.api = api
            return
        exe = shutil.which("opencode")
        if not exe:
            raise OpencodeError("opencode not found on PATH")
        major = await asyncio.to_thread(cli_major, exe)
        port = str(urlparse(self.base).port or 4096)
        env = {**os.environ, "OPENCODE_CONFIG": str(config_file(self.settings, major)),
               "OPENCODE_PASSWORD": self.password}  # v2 reads it; v1 ignores it
        out = open(runtime_dir() / "opencode-serve.log", "ab")
        log.info("starting opencode serve v%d on port %s (output in %s)", major, port,
                 runtime_dir() / "opencode-serve.log")
        self._server = subprocess.Popen([exe, "serve", "--port", port, "--hostname", "127.0.0.1"] if major >= 2
                                        else [exe, "serve", "--port", port],
                                        env=env, stdout=out, stderr=out,
                                        **platform.group_kwargs())  # type: ignore[call-overload]
        for _ in range(60):
            api = await self._probe()
            if api:
                self.api = api
                return
            await asyncio.sleep(1)
        log.error("opencode serve did not answer on %s within 60s; see opencode-serve.log", self.base)
        raise OpencodeError(f"opencode serve did not come up on {self.base}")

    async def _api(self) -> int:
        if self.api is None:
            await self.ensure_server()
        return self.api or 1

    # -- sessions ---------------------------------------------------------------------------

    async def create_session(self, directory: Path, title: str, rules: list[dict[str, str]] | None = None) -> str:
        """A session with the worker's permissions: `rules` (the project's), else the global ones."""
        rules = rules or ruleset(self.settings)
        if await self._api() >= 2:
            r = await self.http.post("/api/session", json={"title": title, "location": {"directory": str(directory)},
                                                           "permissions": ruleset_v2(rules)})
            r.raise_for_status()
            return str(r.json()["data"]["id"])
        r = await self.http.post("/session", params={"directory": str(directory)},
                                 json={"title": title, "permission": rules})
        r.raise_for_status()
        return str(r.json()["id"])

    async def _watch_permissions(self, session: str, directory: Path, on_denied) -> None:
        """Anything still set to "ask" would wait forever for a human at a terminal.
        Reject it, tell the worker why, and report it."""
        v2 = await self._api() >= 2
        params = {"directory": str(directory)}
        failures = 0
        while True:
            await asyncio.sleep(3)
            try:
                if v2:
                    pending = (await self.http.get(f"/api/session/{session}/permission", timeout=10)).json()["data"]
                else:
                    pending = (await self.http.get("/permission", params=params, timeout=10)).json()
                failures = 0
            except (httpx.HTTPError, ValueError, KeyError) as e:
                failures += 1
                if failures in (3, 20):  # once when it starts looking real, once more when it persists
                    log.warning("permission poll failing (%d in a row): %s", failures, e)
                continue
            for req in pending:
                if req.get("sessionID") != session:
                    continue
                # A crash here would kill the watchdog silently and leave the worker waiting forever.
                try:
                    message = "devflow: not permitted for unattended workers"
                    if v2:
                        await self.http.post(f"/api/session/{session}/permission/{req['id']}/reply", timeout=10,
                                             json={"decision": "reject", "message": message})
                        what, patterns = req.get("action", "?"), req.get("resources") or []
                    else:
                        await self.http.post(f"/permission/{req['id']}/reply", params=params, timeout=10,
                                             json={"reply": "reject", "message": message})
                        what, patterns = req.get("permission", "?"), req.get("patterns") or []
                    if on_denied:
                        await on_denied(what, patterns)
                except Exception:
                    log.exception("could not reject permission request %s", req.get("id"))

    async def prompt(self, session: str, directory: Path, text: str, timeout: float, on_denied=None,
                     model_name: str | None = None) -> str:
        """Send one prompt and wait for the assistant to finish; returns its final text.
        `model_name` ("provider/model", e.g. a project's override) beats the global setting."""
        chosen = model_name or self.settings.opencode_model
        gw = self.settings.gateway
        if gw and gw.free_only:  # checked before every run: a combo can be edited at any time
            refused = await gateway.ensure_free(gw, chosen)
            if refused:
                log.error("free_only refused the worker model %s: %s", chosen, refused)
                raise OpencodeError(f"free_only: {refused}. Nothing was sent to the model.")
        watchdog = asyncio.create_task(self._watch_permissions(session, directory, on_denied))
        try:
            if await self._api() >= 2:
                return await asyncio.wait_for(self._prompt_v2(session, text, chosen), timeout)
            return await asyncio.wait_for(self._prompt_v1(session, directory, text, chosen), timeout)
        except TimeoutError:
            await self.abort(session, directory)
            log.error("worker timed out after %ds; session %s aborted", timeout, session)
            raise OpencodeError(f"worker still busy after {int(timeout // 60)} minutes; aborted") from None
        finally:
            watchdog.cancel()

    async def _prompt_v1(self, session: str, directory: Path, text: str, chosen: str) -> str:
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if chosen:
            provider, _, model = chosen.partition("/")
            body["model"] = {"providerID": provider, "modelID": model}
        r = await self.http.post(f"/session/{session}/message", params={"directory": str(directory)}, json=body)
        r.raise_for_status()
        data = r.json()
        err = (data.get("info") or {}).get("error")
        if err:
            log.error("opencode session %s ended with an error: %s", session, json.dumps(err)[:1000])
            hit = usage.opencode_limit(err)
            if hit:
                raise OpencodeLimit(f"opencode: {hit.reason}", hit)
            raise OpencodeError(f"opencode: {json.dumps(err)[:500]}")
        texts = [p.get("text", "") for p in data.get("parts", []) if p.get("type") == "text"]
        return "\n".join(t for t in texts if t).strip()

    async def _prompt_v2(self, session: str, text: str, chosen: str) -> str:
        """v2 admits the prompt and runs the agent loop in the background: set the model, send
        the text, then wait for the `idle` message that follows it."""
        if chosen:
            provider, _, model = chosen.partition("/")
            r = await self.http.post(f"/api/session/{session}/model",
                                     json={"model": {"providerID": provider, "id": model}})
            r.raise_for_status()
        r = await self.http.post(f"/api/session/{session}/prompt", json={"text": text})
        r.raise_for_status()
        sent = r.json()["data"]
        since = float((sent.get("time") or {}).get("created") or 0)
        while True:
            await asyncio.sleep(WAIT_POLL)
            msgs = await self._messages_v2(session, since)
            idle = next((m for m in msgs if m.get("type") == "idle"), None)
            if idle is None:
                await self._give_up_on_limit(session, msgs)
                continue
            outcome = idle.get("outcome")
            assistants = [m for m in msgs if m.get("type") == "assistant"]
            if outcome != "succeeded":
                err = idle.get("error") or next((m["error"] for m in reversed(assistants) if m.get("error")), None)
                log.error("opencode session %s ended %s: %s", session, outcome, json.dumps(err)[:1000])
                hit = usage.opencode_limit_v2(err)
                if hit:
                    raise OpencodeLimit(f"opencode: {hit.reason}", hit)
                raise OpencodeError(f"opencode: worker {outcome}: {json.dumps(err)[:500] if err else 'no reason given'}")
            for m in reversed(assistants):  # the last thing the worker said
                texts = [c.get("text", "") for c in m.get("content") or [] if c.get("type") == "text"]
                if any(t.strip() for t in texts):
                    return "\n".join(t for t in texts if t).strip()
            return ""

    async def _give_up_on_limit(self, session: str, msgs: list[dict[str, Any]]) -> None:
        """v2 retries a rate-limited model call on its own, without end, and the session
        never goes idle. Behind a gateway the combo's fallbacks are already spent by
        then, so after a few attempts stop the run and report the limit: the pipeline
        pauses the task and resumes it after the reset instead of timing out."""
        last = next((m for m in reversed(msgs) if m.get("type") == "assistant"), None)
        retry = (last or {}).get("retry") or {}
        if int(retry.get("attempt") or 0) < LIMIT_RETRIES:
            return
        hit = usage.opencode_limit_v2(retry.get("error"))
        if hit is None:
            return
        floor = dt.datetime.now(dt.UTC) + LIMIT_MIN_PAUSE  # a short cooldown on the last
        if hit.resets_at is None or hit.resets_at < floor:  # fallback says little about the rest
            hit = dataclasses.replace(hit, resets_at=floor)
        await self.abort(session, Path())
        log.warning("opencode session %s still retrying after %d attempts; stopped: %s",
                    session, retry.get("attempt"), hit.reason)
        raise OpencodeLimit(f"opencode: {hit.reason}", hit)

    async def _messages_v2(self, session: str, since: float) -> list[dict[str, Any]]:
        """The session's messages created at or after `since` (ms), oldest first."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):  # newest first, page by page, until the prompt is behind us
            params: dict[str, Any] = {"order": "desc", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            r = await self.http.get(f"/api/session/{session}/message", params=params, timeout=30)
            r.raise_for_status()
            page = r.json()
            data = page.get("data") or []
            newer = [m for m in data if float((m.get("time") or {}).get("created") or 0) >= since]
            out += newer
            cursor = (page.get("cursor") or {}).get("next")
            if len(newer) < len(data) or not cursor:
                break
        return list(reversed(out))

    async def session_usage(self, session: str, directory: Path) -> Usage:
        """Tokens and cost of every assistant message in the session so far."""
        try:
            if await self._api() >= 2:
                r = await self.http.get(f"/api/session/{session}", timeout=30)
                r.raise_for_status()
                return usage.opencode_usage_v2(r.json()["data"])
            r = await self.http.get(f"/session/{session}/message", params={"directory": str(directory)}, timeout=30)
            r.raise_for_status()
            return usage.opencode_usage(r.json())
        except (httpx.HTTPError, ValueError, KeyError, OpencodeError) as e:
            log.warning("could not read opencode usage for %s: %s", session, e)
            return Usage()

    async def abort(self, session: str, directory: Path) -> None:
        try:
            if await self._api() >= 2:
                await self.http.post(f"/api/session/{session}/interrupt", timeout=10)
            else:
                await self.http.post(f"/session/{session}/abort", params={"directory": str(directory)}, timeout=10)
        except (httpx.HTTPError, OpencodeError) as e:
            log.warning("could not abort opencode session %s: %s", session, e)

    async def dispose(self, directory: Path) -> None:
        """opencode keeps an instance, with a file watcher, for every directory it has worked in.
        On Windows the watcher's open handle stops the folder from being deleted ("Permission
        denied" from `git worktree remove`), so let go of it before removing a worktree.
        v2 calls this evicting a location."""
        try:
            if self.api is None:
                self.api = await self._probe()
            if self.api is None:
                return  # no server running means no watcher either
            if self.api >= 2:
                await self.http.delete("/api/debug/location", params={"location[directory]": str(directory)}, timeout=15)
            else:
                await self.http.post("/instance/dispose", params={"directory": str(directory)}, timeout=15)
        except (httpx.HTTPError, OpencodeError) as e:
            log.info("no opencode instance to dispose for %s (%s)", directory, e)

    async def close(self) -> None:
        await self.http.aclose()
        if self._server and self._server.poll() is None:
            # opencode.CMD wraps node: kill the whole tree, not just cmd.exe.
            platform.kill_tree(self._server)
