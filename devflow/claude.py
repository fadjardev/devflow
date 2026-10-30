"""Claude Code, headless (`claude -p`), as planner, reviewer and closer.

It never gets Write/Edit or a shell: it reads files, reads git through
devflow-mcp's checked `git` tool (ADR 0004), and writes only through
devflow-mcp, which each run gets bound to its own task and role.
`--permission-mode dontAsk` refuses anything not on the allow-list instead of
waiting for an answer nobody is there to give.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, runtime_dir

log = logging.getLogger("devflow.claude")

READ_ONLY = [
    "Read", "Grep", "Glob",
    "mcp__devflow__git",
    "mcp__codebase-memory-mcp",
    "mcp__devflow__task_read",
]
NEVER = ["Write", "Edit", "NotebookEdit", "Bash", "WebFetch", "WebSearch"]

PLANNER_TOOLS = READ_ONLY + ["mcp__devflow__task_append_log", "mcp__devflow__task_update_state"]
REVIEWER_TOOLS = READ_ONLY + ["mcp__devflow__task_append_log"]
CLOSER_TOOLS = ["mcp__devflow__task_read", "mcp__devflow__task_append_log",
                "mcp__devflow__decision_add", "mcp__devflow__project_note_update", "Read"]
ASKER_TOOLS = READ_ONLY  # /ask: answers questions, writes nothing


def secret_rules(settings: Settings) -> list[str]:
    """DENY_READ as Claude Code deny rules. Read rules cover Grep and Glob too. The patterns
    are relative to the working directory and every --add-dir, at any depth."""
    return [f"Read({p if p.startswith('**/') else '**/' + p})" for p in settings.deny_read]


@dataclass(frozen=True)
class Bind:
    """What this run's devflow-mcp is bound to (see mcp_server)."""
    role: str
    task: str = ""
    scope: str = ""


@dataclass
class Result:
    text: str
    session_id: str
    ok: bool
    cost_usd: float = 0.0


def mcp_config(settings: Settings, bind: Bind | None, cwd: Path) -> Path:
    """This run's devflow-mcp entry: this venv's interpreter, bound to one task and role.
    One file per run, since runs for different tasks overlap; `run` deletes it afterwards."""
    folder = runtime_dir() / "mcp"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"claude-{uuid.uuid4().hex}.json"
    env = {"DEVFLOW_VAULT": str(settings.vault), "DEVFLOW_CWD": str(cwd),
           "DEVFLOW_DENY_READ": ",".join(settings.deny_read)}
    if bind:
        env |= {"DEVFLOW_ROLE": bind.role, "DEVFLOW_TASK": bind.task, "DEVFLOW_SCOPE": bind.scope}
    cfg = {"mcpServers": {"devflow": {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "devflow.mcp_server"],
        "env": env,
    }}}
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


async def run(settings: Settings, prompt: str, cwd: Path, tools: list[str],
              resume: str = "", add_dirs: tuple[Path, ...] = (), timeout: float = 1800,
              bind: Bind | None = None) -> Result:
    exe = shutil.which("claude")
    if not exe:
        return Result("claude CLI not found on PATH", "", False)
    config = mcp_config(settings, bind, cwd)
    try:
        return await _run(settings, exe, config, prompt, cwd, tools, resume, add_dirs, timeout)
    finally:
        config.unlink(missing_ok=True)


async def _run(settings: Settings, exe: str, config: Path, prompt: str, cwd: Path, tools: list[str],
               resume: str, add_dirs: tuple[Path, ...], timeout: float) -> Result:
    args = [exe, "-p", "--output-format", "json", "--permission-mode", "dontAsk",
            "--mcp-config", str(config),
            "--allowedTools", *tools, "--disallowedTools", *NEVER, *secret_rules(settings)]
    for d in add_dirs:
        args += ["--add-dir", str(d)]
    if settings.claude_model:
        args += ["--model", settings.claude_model]
    if resume:
        args += ["--resume", resume]
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        # The prompt goes in on stdin: Windows caps a command line at ~32k characters.
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout)
    except TimeoutError:
        proc.kill()
        log.error("claude -p timed out after %ds in %s", timeout, cwd)
        return Result(f"claude timed out after {int(timeout)}s", "", False)
    except asyncio.CancelledError:
        proc.kill()
        raise
    raw = out.decode("utf-8", "replace").strip()
    try:
        data = json.loads(raw.splitlines()[-1]) if raw else {}
    except json.JSONDecodeError:
        log.error("claude -p exit %s, no JSON result. stdout: %s | stderr: %s",
                  proc.returncode, raw[-800:], err.decode("utf-8", "replace").strip()[-800:])
        return Result(raw or err.decode("utf-8", "replace"), "", False)
    if proc.returncode != 0 or data.get("is_error"):
        log.error("claude -p failed (exit %s, %s): %s | stderr: %s", proc.returncode, data.get("subtype", "?"),
                  str(data.get("result", ""))[:800], err.decode("utf-8", "replace").strip()[-800:])
    return Result(
        text=str(data.get("result", "")),
        session_id=str(data.get("session_id", "")),
        ok=proc.returncode == 0 and not data.get("is_error", False),
        cost_usd=float(data.get("total_cost_usd") or 0),
    )
