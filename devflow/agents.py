"""How the two agent CLIs are doing, for the dashboard: installed, which version, the opencode
server, the last run, today's use and any provider limit in force. Nothing here starts work."""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import time
from dataclasses import dataclass

from .usage import Limit

VERSION_TTL = 600.0  # seconds; a CLI's version rarely changes while devflow runs
_versions: dict[str, tuple[float, str]] = {}


@dataclass(frozen=True)
class AgentStatus:
    backend: str            # claude | opencode
    name: str               # what people call it
    role: str               # what it does in devflow
    path: str               # where the CLI is, "" when not on PATH
    version: str
    state: str              # ready | working | limited | missing | error
    detail: str
    model: str
    server: str = ""        # opencode only: the `opencode serve` it talks to
    last_run: dt.datetime | None = None
    last_outcome: str = ""
    runs_today: int = 0
    tokens_today: int = 0
    limit: Limit | None = None


async def version(exe: str, timeout: float = 15) -> str:
    """`<exe> --version`, first line, cached. "" when it doesn't answer in time."""
    cached = _versions.get(exe)
    if cached and time.monotonic() - cached[0] < VERSION_TTL:
        return cached[1]
    text = ""
    try:
        proc = await asyncio.create_subprocess_exec(exe, "--version", stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            text = out.decode("utf-8", "replace").strip().splitlines()[0][:80] if out.strip() else ""
        except TimeoutError:
            proc.kill()
    except OSError:
        text = ""
    _versions[exe] = (time.monotonic(), text)
    return text


async def status(flow: object) -> list[AgentStatus]:
    """Both agents, from the Flow's settings, its usage record and its opencode client."""
    from .pipeline import Flow  # the dashboard passes its Flow; imported here to keep this module light
    assert isinstance(flow, Flow)
    s, spend = flow.s, flow.spend
    now = spend.now()
    day_start = dt.datetime.combine(now.astimezone().date(), dt.time(), tzinfo=now.astimezone().tzinfo)
    rows = spend.db.rows(since=day_start - dt.timedelta(days=30))
    busy = {b: False for b in ("claude", "opencode")}
    for t in flow.store.active():
        if flow.running(t):
            busy["opencode" if t.state == "coding" else "claude"] = True

    out = []
    for backend, name, role, model in (
            ("claude", "Claude Code", "plans, reviews, closes, answers Ask", s.claude_model or "Claude Code default"),
            ("opencode", "opencode", "writes the code (the worker)", s.opencode_model or "opencode default")):
        exe = shutil.which(backend) or ""
        mine = [r for r in rows if r.backend == backend]
        today = [r for r in mine if r.ts >= day_start]
        last = mine[-1] if mine else None
        limit = spend.active_limit(backend)
        server = ""
        if backend == "opencode":
            alive = await flow.oc.alive()
            server = f"{s.opencode_url} ({'running' if alive else 'starts when a task reaches coding'})"
        if not exe:
            state, detail = "missing", f"`{backend}` is not on PATH"
        elif limit:
            state, detail = "limited", limit.reason + (f"; resets {limit.resets_at:%d %b %H:%M}" if limit.resets_at else "")
        elif busy[backend]:
            state, detail = "working", "running a task step now"
        elif last and last.outcome == "error":
            state, detail = "error", "the last run ended with an error; see Logs"
        else:
            state, detail = "ready", "idle"
        out.append(AgentStatus(
            backend=backend, name=name, role=role, path=exe, version=await version(exe) if exe else "",
            state=state, detail=detail, model=model, server=server,
            last_run=last.ts if last else None, last_outcome=last.outcome if last else "",
            runs_today=len(today), tokens_today=sum(r.usage.tokens for r in today), limit=limit))
    return out
