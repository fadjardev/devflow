"""devflow-mcp: the only write path agents have into the Obsidian vault.

Run with `python -m devflow.mcp_server` (stdio). Every tool validates its
arguments against the vault itself — task ids against 05-Tasks, projects
against 01-Projects — so a filename can never come from free text, and
state changes go through the same state machine the bot uses, as actor
"agent": forward progress and giving up only, never a human gate.
"""

from __future__ import annotations

import functools
import logging
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import logs, tasks
from .tasks import TaskStore

VAULT = Path(os.environ.get("DEVFLOW_VAULT") or os.environ.get("VAULT_PATH") or r"E:\dev\projects\1-project-knowledge")
store = TaskStore(VAULT)
mcp = MCPServer("devflow")
log = logging.getLogger("devflow.mcp")


def _guard(fn):
    """Turn refusals into a readable tool result instead of a protocol error, and log every call."""
    @functools.wraps(fn)  # keeps the signature MCPServer builds the tool schema from
    def wrapper(*args, **kwargs):
        token = logs.task_id.set(str(kwargs.get("task_id") or kwargs.get("project") or "-"))
        try:
            result = fn(*args, **kwargs)
            log.info("%s ok", fn.__name__)
            return result
        except (ValueError, TimeoutError) as e:
            log.warning("%s refused: %s", fn.__name__, e)
            return f"REFUSED: {e}"
        except Exception:
            log.exception("%s crashed", fn.__name__)
            raise
        finally:
            logs.task_id.reset(token)
    return wrapper


@mcp.tool()
@_guard
def task_read(task_id: str) -> str:
    """Read a devflow task note (frontmatter + all sections). task_id looks like 261002-01."""
    task = store.get(task_id)
    return task.note.render()


@mcp.tool()
@_guard
def task_update_state(task_id: str, to: str, note: str = "") -> str:
    """Move a task forward. Agents may only do new->planned, coding->review, or ->blocked (give up, explain why in `note`). Gates are human-only."""
    task = store.transition(task_id, to, actor="agent", reason=note)
    return f"OK: {task.id} is now {task.state}"


@mcp.tool()
@_guard
def task_append_log(task_id: str, section: str, text: str) -> str:
    """Append markdown to a task section. Allowed: Spec, Worker log, Review, Verification, Result."""
    store.append(task_id, section, text, actor="agent")
    return f"OK: appended to {section} of {task_id}"


@mcp.tool()
@_guard
def decision_add(project: str, title: str, body: str) -> str:
    """Add a Decision Entry to 01-Projects/<project>.md under ## Decisions. Body should carry **Context:**, **Decision:**, **Alternatives considered:**, **Consequences:**."""
    path = tasks.add_decision(VAULT, project, title, body)
    return f"OK: decision added to {path.name}"


@mcp.tool()
@_guard
def project_note_update(project: str, section: str, text: str, mode: str = "append") -> str:
    """Append to (mode=append) or replace (mode=replace) one ## section of 01-Projects/<project>.md. Frontmatter is never changed; use decision_add for Decisions."""
    path = tasks.update_project_section(VAULT, project, section, text, mode)
    return f"OK: {section} updated in {path.name}"


if __name__ == "__main__":
    # stdout is the protocol channel: log to files only. Several servers can run at
    # once (one per agent), so no rotation; the bot rotates mcp.log at startup.
    logs.setup("mcp", console=False, rotate=False)
    mcp.run()
