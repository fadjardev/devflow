"""devflow-mcp: the only write path agents have into task and project notes.

Run with `python -m devflow.mcp_server` (stdio). devflow starts one server per
agent run, bound to that run's task and role through the environment
(PRD SR6), and checks the binding on every call:

- DEVFLOW_VAULT: the vault (required; there is no default).
- DEVFLOW_ROLE: planner, reviewer, closer or asker. Unset means every call is refused.
- DEVFLOW_TASK: the one task this run may read and write (empty for asker).
- DEVFLOW_SCOPE: asker only, the project it may read tasks of, or `*`.
- DEVFLOW_CWD / DEVFLOW_DENY_READ: where the `git` tool runs and what it may not read.

Every tool also validates its arguments against the vault itself, so a
filename can never come from free text, and state changes go through the same
state machine the bot uses, as actor "agent": forward progress and giving up
only, never a human gate. The opencode worker gets no devflow-mcp (ADR 0003).
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer

from . import gitsafe, logs, tasks
from .tasks import TaskStore
from .vault import VaultError

mcp = MCPServer("devflow")
log = logging.getLogger("devflow.mcp")

ROLE_TOOLS: dict[str, set[str]] = {
    "planner": {"task_read", "task_append_log", "task_update_state", "git"},
    "reviewer": {"task_read", "task_append_log", "git"},
    "closer": {"task_read", "task_append_log", "decision_add", "project_note_update"},
    "asker": {"task_read", "git"},
}
ROLE_SECTIONS: dict[str, set[str]] = {"planner": {"Spec"}, "reviewer": {"Review"}, "closer": {"Result"}}
ROLE_STATES: dict[str, set[str]] = {"planner": {"planned", "blocked"}}
PROJECT_NOTE_LIMIT = 4000  # characters per closer write; a project note is read by every later run


class Refused(VaultError):
    pass


@dataclass(frozen=True)
class Binding:
    vault: Path
    role: str
    task: str
    scope: str
    cwd: Path | None
    deny_read: list[str]

    @classmethod
    def from_env(cls) -> Binding:
        env = os.environ.get
        vault = env("DEVFLOW_VAULT", "")
        if not vault:
            raise Refused("devflow-mcp has no vault (DEVFLOW_VAULT unset)")
        role = env("DEVFLOW_ROLE", "")
        if role not in ROLE_TOOLS:
            raise Refused("devflow-mcp is not bound to a role for this run")
        task = env("DEVFLOW_TASK", "")
        if role != "asker" and not task:
            raise Refused(f"devflow-mcp ({role}) is not bound to a task")
        cwd = env("DEVFLOW_CWD", "")
        return cls(Path(vault), role, task, env("DEVFLOW_SCOPE", ""), Path(cwd) if cwd else None,
                   [p for p in env("DEVFLOW_DENY_READ", "").split(",") if p.strip()])

    def allow(self, tool: str) -> None:
        if tool not in ROLE_TOOLS[self.role]:
            raise Refused(f"the {self.role} may not use {tool}")

    def own_task(self, store: TaskStore, task_id: str) -> tasks.Task:
        """The bound task, or (for the asker) a task in its scope."""
        tasks.check_id(task_id)
        if self.role == "asker":
            t = store.get(task_id)
            if self.scope not in ("*", t.project):
                raise Refused(f"{task_id} belongs to {t.project}, not {self.scope}")
            return t
        if task_id != self.task:
            raise Refused(f"this run is bound to task {self.task}, not {task_id}")
        return store.get(task_id)


F = TypeVar("F", bound=Callable[..., str])


def _guard(fn: F) -> F:
    """Bind, check the role, turn refusals into a readable tool result instead of a protocol
    error, and log every call."""
    @functools.wraps(fn)  # keeps the signature MCPServer builds the tool schema from
    def wrapper(*args: Any, **kwargs: Any) -> str:
        token = logs.task_id.set(str(kwargs.get("task_id") or os.environ.get("DEVFLOW_TASK") or "-"))
        try:
            binding = Binding.from_env()
            binding.allow(fn.__name__)
            result = fn(binding, *args, **kwargs)
            log.info("%s ok (%s)", fn.__name__, binding.role)
            return result
        except (ValueError, TimeoutError) as e:
            log.warning("%s refused: %s", fn.__name__, e)
            return f"REFUSED: {e}"
        except Exception:
            log.exception("%s crashed", fn.__name__)
            raise
        finally:
            logs.task_id.reset(token)

    # MCPServer reads the parameters from the signature; the binding is not one of them.
    sig = inspect.signature(fn)
    wrapper.__signature__ = sig.replace(parameters=list(sig.parameters.values())[1:])  # type: ignore[attr-defined]
    return wrapper  # type: ignore[return-value]


@mcp.tool()
@_guard
def task_read(b: Binding, task_id: str) -> str:
    """Read a devflow task note (frontmatter + all sections). task_id looks like 261002-01."""
    return b.own_task(TaskStore(b.vault), task_id).note.render()


@mcp.tool()
@_guard
def task_update_state(b: Binding, task_id: str, to: str, note: str = "") -> str:
    """Move your task forward: new->planned when the spec is written, or ->blocked to give up (explain why in `note`). Gates are human-only."""
    store = TaskStore(b.vault)
    current = b.own_task(store, task_id).state
    if "agent" in tasks.TRANSITIONS.get((current, to), set()) and to not in ROLE_STATES.get(b.role, set()):
        raise Refused(f"the {b.role} may not move a task to {to}")
    task = store.transition(task_id, to, actor="agent", reason=note)
    return f"OK: {task.id} is now {task.state}"


@mcp.tool()
@_guard
def task_append_log(b: Binding, task_id: str, section: str, text: str) -> str:
    """Append markdown to your section of the task: Spec (planner), Review (reviewer) or Result (closer)."""
    store = TaskStore(b.vault)
    b.own_task(store, task_id)
    if section.strip().lower() not in {s.lower() for s in ROLE_SECTIONS.get(b.role, set())}:
        raise Refused(f"the {b.role} may write only {', '.join(sorted(ROLE_SECTIONS.get(b.role, set())))}")
    store.append(task_id, section, text, actor="agent")
    return f"OK: appended to {section} of {task_id}"


def _closer_project(b: Binding, project: str, text: str) -> None:
    t = b.own_task(TaskStore(b.vault), b.task)
    if project != t.project:
        raise Refused(f"this run is about {t.project}, not {project}")
    if len(text) > PROJECT_NOTE_LIMIT:
        raise Refused(f"too long ({len(text)} characters; at most {PROJECT_NOTE_LIMIT})")


@mcp.tool()
@_guard
def decision_add(b: Binding, project: str, title: str, body: str) -> str:
    """Add a Decision Entry to 01-Projects/<project>.md under ## Decisions. Body should carry **Context:**, **Decision:**, **Alternatives considered:**, **Consequences:**."""
    _closer_project(b, project, title + body)
    path = tasks.add_decision(b.vault, project, title, body)
    return f"OK: decision added to {path.name}"


@mcp.tool()
@_guard
def project_note_update(b: Binding, project: str, section: str, text: str, mode: str = "append") -> str:
    """Append to one ## section of 01-Projects/<project>.md (append only; frontmatter is never changed; use decision_add for Decisions)."""
    if mode != "append":
        raise Refused("project notes are append-only for agents")
    _closer_project(b, project, text)
    path = tasks.update_project_section(b.vault, project, section, text, "append")
    return f"OK: {section} updated in {path.name}"


@mcp.tool()
@_guard
def git(b: Binding, args: list[str]) -> str:
    """Run a read-only git command in this run's repository, e.g. ["log", "--oneline", "-n", "20"], ["show", "origin/main:app/x.php"], ["diff", "BASE...HEAD"]. Subcommands: log, show, diff, status, blame, rev-parse, merge-base, ls-files. Options outside a safe list are refused."""
    if b.cwd is None or not (b.cwd / ".git").exists():
        raise Refused("this run has no git repository")
    return gitsafe.run_read_only(args, b.cwd, b.deny_read)


if __name__ == "__main__":
    # stdout is the protocol channel: log to files only. Several servers can run at
    # once (one per agent), so no rotation; the bot rotates mcp.log at startup.
    logs.setup("mcp", console=False, rotate=False)
    mcp.run()
