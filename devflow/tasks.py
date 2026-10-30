"""Task notes in `05-Tasks/<project>/<id>.md` and the state machine they carry.

    new ──plan──> planned ══gate 1══> coding ──> review ══gate 2══> done
                     ^                  ^           │
                     └─ /reject (replan) └─ fix-up ─┘   any active state -> blocked / cancelled

`new` means "the planner is running". The two gates are the only way past
`planned` and `review`, and only the bot (a human on Telegram) may take them.
Agents, through devflow-mcp, may only report progress forward and give up.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import vault
from .vault import Note, VaultError

log = logging.getLogger("devflow.tasks")

TASKS_DIR = "05-Tasks"
PROJECTS_DIR = "01-Projects"
REQUESTS_DIR = "04-Requests"

STATES = ("new", "planned", "coding", "review", "done", "blocked", "cancelled")
ACTIVE = ("new", "planned", "coding", "review", "blocked")
FINAL = ("done", "cancelled")

# (from, to) -> who may make the move.
_BOT, _AGENT = "bot", "agent"
TRANSITIONS: dict[tuple[str, str], set[str]] = {
    ("new", "planned"): {_BOT, _AGENT},
    ("planned", "new"): {_BOT},          # gate 1 rejected: replan
    ("planned", "coding"): {_BOT},       # gate 1 approved
    ("coding", "review"): {_BOT, _AGENT},
    ("review", "coding"): {_BOT},        # fix-up round, or gate 2 rejected
    ("review", "done"): {_BOT},          # gate 2 approved
}
for _s in ("new", "planned", "coding", "review"):
    TRANSITIONS[(_s, "blocked")] = {_BOT, _AGENT}
for _s in ACTIVE:
    TRANSITIONS[(_s, "cancelled")] = {_BOT}
for _s in ("new", "planned", "coding", "review"):
    TRANSITIONS[("blocked", _s)] = {_BOT}  # /retry resumes where it stopped

SECTIONS = ("Request", "Spec", "Worker log", "Review", "Verification", "Result", "History")
# Sections an agent may append to through devflow-mcp. Request and History are the bot's.
AGENT_SECTIONS = ("Spec", "Worker log", "Review", "Verification", "Result")
# States in which an agent may still write the Spec: before gate 1.
SPEC_OPEN = ("new", "planned")

ID_RE = re.compile(r"^\d{6}-\d{2}$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


class TransitionError(VaultError):
    pass


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def check_id(task_id: str) -> str:
    if not ID_RE.match(task_id or ""):
        raise VaultError(f"bad task id: {task_id!r} (expected YYMMDD-NN)")
    return task_id


def check_project(vault_root: Path, project: str) -> str:
    """A project is real only if `01-Projects/<project>.md` exists."""
    if not PROJECT_RE.match(project or "") or ".." in project:
        raise VaultError(f"bad project name: {project!r}")
    if not (vault_root / PROJECTS_DIR / f"{project}.md").is_file():
        raise VaultError(f"unknown project: {project} (no {PROJECTS_DIR}/{project}.md)")
    return project


@dataclass
class Task:
    path: Path
    note: Note

    @property
    def id(self) -> str:
        return str(self.note.meta["id"])

    @property
    def project(self) -> str:
        return str(self.note.meta["project_name"])

    @property
    def state(self) -> str:
        return str(self.note.meta["state"])

    @property
    def meta(self) -> dict[str, Any]:
        return self.note.meta


class TaskStore:
    def __init__(self, vault_root: Path):
        self.vault = vault_root

    # -- lookup ------------------------------------------------------------

    def _find(self, task_id: str) -> Path:
        check_id(task_id)
        hits = list((self.vault / TASKS_DIR).glob(f"*/{task_id}.md"))
        if not hits:
            raise VaultError(f"no such task: {task_id}")
        return hits[0]

    def get(self, task_id: str) -> Task:
        path = self._find(task_id)
        return Task(path, vault.read(path))

    def all(self) -> list[Task]:
        root = self.vault / TASKS_DIR
        if not root.is_dir():
            return []
        tasks = [Task(p, vault.read(p)) for p in sorted(root.glob("*/*.md")) if ID_RE.match(p.stem)]
        return sorted(tasks, key=lambda t: t.id)

    def active(self) -> list[Task]:
        return [t for t in self.all() if t.state in ACTIVE]

    def next_id(self, today: dt.date | None = None) -> str:
        prefix = (today or dt.date.today()).strftime("%y%m%d")
        used = [int(t.id[-2:]) for t in self.all() if t.id.startswith(prefix)]
        n = max(used, default=0) + 1
        if n > 99:
            raise VaultError("more than 99 tasks today")
        return f"{prefix}-{n:02d}"

    # -- writes ------------------------------------------------------------

    def create(self, project: str, title: str, request_link: str, request_text: str) -> Task:
        check_project(self.vault, project)
        task_id = self.next_id()
        path = vault.safe_path(self.vault, f"{TASKS_DIR}/{project}/{task_id}.md")
        stamp = now()
        meta = {
            "title": title.strip().splitlines()[0][:120] if title.strip() else task_id,
            "type": "task",
            "id": task_id,
            "project": f"[[{project}]]",
            "project_name": project,
            "state": "new",
            "request": f"[[{request_link}]]",
            "branch": "",
            "base": "",
            "worktree": "",
            "vhost": "",
            "bounces": 0,
            "review_verdict": "",
            "blocked_from": "",
            "claude_session": "",
            "opencode_session": "",
            "tags": ["task"],
            "created": stamp,
            "updated": stamp,
        }
        body = f"\n# {task_id} — {meta['title']}\n"
        note = Note(meta, body)
        quoted = "\n".join(f"> {line}" for line in request_text.strip().splitlines())
        note.set_section("Request", f"From [[{request_link}]]:\n\n{quoted}")
        for s in SECTIONS[1:-1]:
            note.set_section(s, "")
        note.set_section("History", f"- {stamp} — created (new)")
        with vault.locked(path):
            if path.exists():
                raise VaultError(f"task already exists: {task_id}")
            vault.write(path, note)
        return Task(path, note)

    def update(self, task_id: str, **fields: Any) -> Task:
        path = self._find(task_id)
        with vault.locked(path):
            note = vault.read(path)
            note.meta.update(fields)
            note.meta["updated"] = now()
            vault.write(path, note)
        return Task(path, note)

    def transition(self, task_id: str, to: str, actor: str, reason: str = "", **fields: Any) -> Task:
        if to not in STATES:
            raise TransitionError(f"unknown state: {to}")
        path = self._find(task_id)
        with vault.locked(path):
            note = vault.read(path)
            frm = note.meta["state"]
            allowed = TRANSITIONS.get((frm, to), set())
            if actor not in allowed:
                who = "only the bot" if allowed else "nobody"
                log.warning("%s: refused %s -> %s by %s", task_id, frm, to, actor)
                raise TransitionError(f"{task_id}: {frm} -> {to} not allowed for {actor} ({who} may)")
            if to == "blocked":
                note.meta["blocked_from"] = frm
            note.meta.update(fields)
            note.meta["state"] = to
            note.meta["updated"] = now()
            line = f"- {note.meta['updated']} — {frm} -> {to} ({actor})"
            if reason:
                line += f": {' '.join(reason.split())[:300]}"
            note.set_section("History", line, append=True, sep="\n")
            vault.write(path, note)
        log.log(logging.WARNING if to == "blocked" else logging.INFO, "%s: %s -> %s (%s)%s",
                task_id, frm, to, actor, f": {' '.join(reason.split())[:300]}" if reason else "")
        return Task(path, note)

    def append(self, task_id: str, section: str, text: str, actor: str = _BOT, replace: bool = False) -> Task:
        allowed = AGENT_SECTIONS if actor == _AGENT else SECTIONS
        match = next((s for s in allowed if s.lower() == section.lower()), None)
        if match is None:
            raise VaultError(f"{actor} may not write section {section!r}; allowed: {', '.join(allowed)}")
        path = self._find(task_id)
        with vault.locked(path):
            note = vault.read(path)
            if note.meta["state"] in FINAL and actor == _AGENT:
                raise VaultError(f"{task_id} is {note.meta['state']}; it no longer takes agent writes")
            if match == "Spec" and actor == _AGENT and note.meta["state"] not in SPEC_OPEN:
                # The human approved this text at gate 1; the worker implements whatever it says.
                raise VaultError(f"{task_id}: the Spec was approved at gate 1 and can't change now")
            note.set_section(match, text, append=not replace)
            note.meta["updated"] = now()
            vault.write(path, note)
        return Task(path, note)


# -- project notes (01-Projects) ----------------------------------------------

_SECTION_NAME = re.compile(r"^[\w][\w .&/()-]{0,59}$")


def _project_note(vault_root: Path, project: str) -> Path:
    check_project(vault_root, project)
    return vault.safe_path(vault_root, f"{PROJECTS_DIR}/{project}.md")


def add_decision(vault_root: Path, project: str, title: str, body: str) -> Path:
    """Insert a Decision Entry (00-Meta/Templates/Decision Entry.md shape) under `## Decisions`."""
    title = " ".join(title.split())[:100]
    if not title:
        raise VaultError("decision needs a title")
    path = _project_note(vault_root, project)
    entry = f"### {dt.date.today():%Y-%m-%d} — {title}\n\n{body.strip()}"
    with vault.locked(path):
        note = vault.read(path)
        note.set_section("Decisions", entry, append=True)
        vault.write(path, note)
    return path


def update_project_section(vault_root: Path, project: str, section: str, text: str, mode: str = "append") -> Path:
    section = section.strip()
    if not _SECTION_NAME.match(section):
        raise VaultError(f"bad section name: {section!r}")
    if section.lower() == "decisions":
        raise VaultError("use decision_add for the Decisions section")
    if mode not in ("append", "replace"):
        raise VaultError("mode must be append or replace")
    path = _project_note(vault_root, project)
    with vault.locked(path):
        note = vault.read(path)  # frontmatter is carried through untouched
        note.set_section(section, text, append=(mode == "append"))
        vault.write(path, note)
    return path


def create_request(vault_root: Path, project: str, text: str, today: dt.date | None = None) -> str:
    """Log a raw request verbatim per Conventions; returns the note's link title."""
    check_project(vault_root, project)
    day = (today or dt.date.today()).isoformat()
    base = f"Request {project} {day}"
    title, n = base, 1
    while (vault_root / REQUESTS_DIR / project / f"{title}.md").exists():
        n += 1
        title = f"{base} ({n})"
    path = vault.safe_path(vault_root, f"{REQUESTS_DIR}/{project}/{title}.md")
    with vault.locked(path):
        vault.write(path, Note({}, text.strip() + "\n\n_(logged via devflow Telegram)_\n"))
    return title


def copy_view(vault_root: Path, dest: Path, project: str) -> Path:
    """A copy of the notes Claude may read for `project` (or `*`, every project): the project
    notes and their task notes, in the vault's layout. Claude gets this folder instead of the
    whole vault, which may hold anything (PRD SR13). Refreshed on every run."""
    wanted = [f"{PROJECTS_DIR}/*.md", f"{TASKS_DIR}/*/*.md"] if project == "*" else \
        [f"{PROJECTS_DIR}/{check_project(vault_root, project)}.md", f"{TASKS_DIR}/{project}/*.md"]
    keep = set()
    for pattern in wanted:
        for src in vault_root.glob(pattern):
            rel = src.relative_to(vault_root)
            keep.add(rel)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.read_bytes())
    for old in [p for p in dest.rglob("*.md") if p.relative_to(dest) not in keep]:
        old.unlink()
    dest.mkdir(parents=True, exist_ok=True)
    return dest
