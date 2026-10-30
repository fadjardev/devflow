"""The task pipeline: plan -> gate 1 -> code -> verify -> review (-> fix-up) -> gate 2 -> close.

Each task runs as one asyncio job that carries it from where it is to the next
human gate, then ends. The bot calls the public methods; everything that
reaches Telegram goes through `Notifier`, so the pipeline never prints.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Protocol

from . import claude, gitsafe, logs, opencode, projects, sync, tasks, verify, vhost, worktree
from .config import PROMPTS_DIR, RUNTIME_DIR, Settings
from .tasks import TaskStore
from .vault import VaultError

log = logging.getLogger("devflow")

WORKER_TIMEOUT = 45 * 60
ASK_TIMEOUT = 10 * 60
ASK_SLOTS = 2  # questions answered at once; more wait their turn
ALL_PROJECTS = ("all", "*")
_TITLE = re.compile(r"TITLE:\s*(\w+)(?:\(([^)]*)\))?!?:\s*(.+)")
_CONVENTIONAL = re.compile(r"(\w+)(?:\(([^)]*)\))?!?:\s*(.+)")  # title_line, stored without "TITLE:"
_BLOCKED = re.compile(r"^BLOCKED:[ \t]*(.*)$", re.M)  # the worker gives up (ADR 0003)
_VERDICT = re.compile(r"VERDICT:\s*(APPROVE|FIXUP)\b(.*)", re.S)


class Notifier(Protocol):
    async def text(self, message: str) -> None: ...
    async def photo(self, path: Path, caption: str) -> None: ...


class FlowError(Exception):
    """Something the human asked for that can't be done right now."""


@dataclass(frozen=True)
class Asked:
    """A /ask conversation: the Claude session to resume and where it runs."""
    session: str
    cwd: Path
    label: str
    scope: str


def _prompt(name: str, **values) -> str:
    return Template((PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")).safe_substitute(values)


def _clip(text: str, limit: int = 1500) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n…"


def _added(before: str, after: str) -> str:
    """The lines an append-only writer added to a note."""
    old = set(before.splitlines())
    return "\n".join(line for line in after.splitlines() if line not in old).strip()


def _title_parts(t: tasks.Task) -> tuple[str, str, str]:
    m = _CONVENTIONAL.match(t.meta.get("title_line") or "")
    if m:
        return m.group(1).lower(), (m.group(2) or "").strip(), m.group(3).strip()
    return "fix", "", t.meta.get("title", t.id)


class Flow:
    def __init__(self, settings: Settings, notifier: Notifier):
        self.s = settings
        self.store = TaskStore(settings.vault)
        self.n = notifier
        self.oc = opencode.Client(settings)
        self.jobs: dict[str, asyncio.Task] = {}
        self.ask_slots = asyncio.Semaphore(ASK_SLOTS)

    # -- plumbing ----------------------------------------------------------

    def _spawn(self, task_id: str, coro) -> None:
        if task_id in self.jobs:
            coro.close()
            raise FlowError(f"{task_id} already has a job running")
        self.jobs[task_id] = asyncio.create_task(self._guarded(task_id, coro))

    async def _guarded(self, task_id: str, coro) -> None:
        logs.task_id.set(task_id)  # this job's own context: every line it logs carries the id
        phase, started = getattr(coro, "__name__", "job"), time.monotonic()
        log.info("%s started", phase)
        try:
            await coro
            log.info("%s finished in %.0fs", phase, time.monotonic() - started)
        except asyncio.CancelledError:
            log.info("%s cancelled after %.0fs", phase, time.monotonic() - started)
            raise
        except Exception as e:  # any failure parks the task where a human can see it
            log.exception("%s failed after %.0fs", phase, time.monotonic() - started)
            state = "?"
            try:
                state = self.store.get(task_id).state
                if state in ("new", "planned", "coding", "review"):
                    self.store.transition(task_id, "blocked", "bot", f"{type(e).__name__}: {e}")
                    state = "blocked"
            except Exception:
                log.exception("could not mark %s blocked", task_id)
            what = {"blocked": "blocked", "done": "approved, but closing it failed"}.get(state, "failed")
            await self.notify(f"⛔ {task_id} {what} in {phase}: {type(e).__name__}: {_clip(str(e), 600)}\n"
                               f"/retry {task_id} · /show {task_id} · /log {task_id}")
        finally:
            self.jobs.pop(task_id, None)

    async def notify(self, message: str) -> None:
        """Send without raising: used where a Telegram error must not hide the error being reported."""
        try:
            await self.n.text(message)
        except Exception:
            log.exception("could not send to Telegram: %s", message[:200])

    def _project_dir(self, t: tasks.Task) -> Path:
        return projects.directory(self.s, t.project)

    async def _check_git(self, t: tasks.Task, wt: Path) -> None:
        """Refuse to go on if anything in .git that makes git run code changed since the
        worktree was made: a hook, the config, attributes (PRD SR3)."""
        baseline = t.meta.get("git_guard")
        if not baseline:
            log.warning("no .git fingerprint for this task (made before devflow checked); not compared")
            return
        changed = await asyncio.to_thread(gitsafe.changes, baseline, wt)
        if changed:
            log.error(".git changed during the task: %s", ", ".join(changed))
            raise RuntimeError(
                f"⚠️ .git was changed while the task ran: {', '.join(changed)}. Something may have "
                f"planted code that git runs. devflow ran none of it. Inspect and restore these in the "
                f"main checkout before /retry; the task can't be approved until they match again.")

    def _notes(self, name: str, project: str) -> Path:
        """The notes Claude may read for this run: a copy of the project's notes, not the vault."""
        return tasks.copy_view(self.s.vault, RUNTIME_DIR / "notes" / name, project)

    def _unsafe(self) -> str:
        """Said on every gate while the worker's shell is unrestricted, so it is never forgotten."""
        if not self.s.unsafe_shell:
            return ""
        return "\n\n⚠️ UNSAFE_SHELL is on: the worker could run any shell command except git."

    async def _sync(self, pdir: Path) -> tuple[str, sync.Fetch]:
        """Fetch, then name the shared branch to build on (origin/development)."""
        fetched = await asyncio.to_thread(sync.fetch, self.s, pdir)
        return await asyncio.to_thread(sync.base_ref, self.s, pdir), fetched

    async def recover(self) -> None:
        """After a restart, tasks that were mid-phase have no job: park them."""
        for t in self.store.active():
            mid_phase = t.state in ("new", "coding") or (t.state == "review" and t.meta.get("review_verdict") != "approve")
            if mid_phase and t.id not in self.jobs:
                self.store.transition(t.id, "blocked", "bot", "bot restarted mid-phase")
                log.warning("%s was mid-%s at startup; parked as blocked", t.id, t.state)
                await self.n.text(f"⚠️ {t.id} was mid-{t.state} when the bot stopped. /retry {t.id}")

    # -- commands ----------------------------------------------------------

    async def new_task(self, project_query: str, text: str) -> tasks.Task:
        project = projects.resolve(self.s.vault, project_query)
        projects.directory(self.s, project)  # fail now, not inside the job
        request = await asyncio.to_thread(tasks.create_request, self.s.vault, project, text)
        t = await asyncio.to_thread(self.store.create, project, text.strip().splitlines()[0], request, text)
        self._spawn(t.id, self._plan(t.id))
        return t

    async def approve(self, task_id: str) -> str:
        t = self.store.get(task_id)
        if task_id in self.jobs:
            raise FlowError(f"{task_id} is still working ({t.state}); wait for it")
        if t.state == "planned":
            return await self._gate_plan(t)
        if t.state == "review":
            if t.meta.get("review_verdict") != "approve":
                raise FlowError(f"{task_id} has no approving review yet")
            pdir, wt = self._project_dir(t), Path(t.meta["worktree"])
            try:
                await self._check_git(t, wt)
            except RuntimeError as e:
                raise FlowError(f"{task_id}: {e}") from None
            ref, fetched = await self._sync(pdir)
            clean, conflicts = await asyncio.to_thread(sync.merge_check, wt, ref)
            if not clean:
                raise FlowError(f"{task_id} no longer merges cleanly into {ref} ({fetched.note()}). "
                                f"Conflicts: {', '.join(conflicts)}\n/refresh {task_id} first")
            self.store.transition(task_id, "done", "bot", f"diff approved; merges cleanly into {ref}")
            self._spawn(task_id, self._close(task_id))
            return f"🏁 {task_id} approved — merges cleanly into {ref} ({fetched.note()}). Cleaning up and recording."
        raise FlowError(f"{task_id} is {t.state}; nothing to approve")

    async def reject(self, task_id: str, reason: str) -> str:
        t = self.store.get(task_id)
        if task_id in self.jobs:
            raise FlowError(f"{task_id} is still working; /stop it first")
        if not reason.strip():
            raise FlowError("say why: /reject <id> <reason>  (or /answer <id> <answer>)")
        replan = t.state == "planned" or (t.state == "blocked" and t.meta.get("blocked_from") in ("new", "planned"))
        if replan:
            self.store.transition(task_id, "new", "bot", f"plan feedback: {reason}")
            self._spawn(task_id, self._plan(task_id, feedback=reason))
            return f"↩️ {task_id} sent back to the planner."
        if t.state == "review":
            self.store.transition(task_id, "coding", "bot", f"diff rejected: {reason}", review_verdict="")
            self._spawn(task_id, self._work(task_id, fixup=f"The human reviewer rejected the diff: {reason}"))
            return f"↩️ {task_id} sent back to the worker."
        raise FlowError(f"{task_id} is {t.state}; nothing to reject")

    async def stop(self, task_id: str) -> str:
        t = self.store.get(task_id)
        if t.state in tasks.FINAL:
            raise FlowError(f"{task_id} is already {t.state}")
        job = self.jobs.get(task_id)
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        if t.meta.get("opencode_session") and t.meta.get("worktree"):
            await self.oc.abort(t.meta["opencode_session"], Path(t.meta["worktree"]))
        leftovers = await self._cleanup(task_id)
        self.store.transition(task_id, "cancelled", "bot", "stopped from Telegram")
        branch = t.meta.get("branch")
        return f"🛑 {task_id} cancelled." + (f" Branch `{branch}` kept." if branch else "") + leftovers

    async def retry(self, task_id: str) -> str:
        t = self.store.get(task_id)
        if task_id in self.jobs:
            raise FlowError(f"{task_id} is still working; wait for it")
        if t.state == "done" and not t.meta.get("closed"):  # approved, but closing stopped part-way
            self._spawn(task_id, self._close(task_id))
            return f"🔁 {task_id} finishing the close."
        if t.state != "blocked":
            raise FlowError(f"{task_id} is {t.state}, not blocked")
        to = t.meta.get("blocked_from") or "new"
        self.store.transition(task_id, to, "bot", "retry")
        if to == "new":
            self._spawn(task_id, self._plan(task_id))
        elif to == "coding":
            self._spawn(task_id, self._work(task_id))
        elif to == "review":
            evidence = self.store.get(task_id).note.section("Verification") or "(none)"
            self._spawn(task_id, self._review(task_id, _clip(evidence, 3000), None))
        return f"🔁 {task_id} resumed at {to}."

    async def refresh(self, task_id: str) -> str:
        """Bring the task branch up to date with the shared branch, then verify and review again."""
        t = self.store.get(task_id)
        if task_id in self.jobs:
            raise FlowError(f"{task_id} is still working; wait for it")
        at_review = t.state == "review" or (t.state == "blocked" and t.meta.get("blocked_from") == "review")
        if not (at_review and t.meta.get("worktree")):
            raise FlowError(f"{task_id} is {t.state}; /refresh works on a task waiting at review")
        ref, fetched = await self._sync(self._project_dir(t))
        wt = Path(t.meta["worktree"])
        if worktree.run(wt, "merge-base", "--is-ancestor", ref, "HEAD").returncode == 0:
            raise FlowError(f"{task_id} already contains the latest {ref} ({fetched.note()})")
        self.store.transition(task_id, "coding", "bot", f"refresh onto {ref}", review_verdict="")
        self._spawn(task_id, self._refresh(task_id, ref))
        return f"🔄 {task_id} updating onto {ref} ({fetched.note()})…"

    def ask_target(self, query: str) -> Asked:
        """Where a question about `query` is answered from: one project, or all of them."""
        if query.lower() in ALL_PROJECTS:
            return Asked("", self.s.projects, "all projects",
                         f"It is about the projects under `{self.s.projects}` in general (any of them, or several).")
        project = projects.resolve(self.s.vault, query)
        path = projects.checkout(self.s, project)
        return Asked("", path, project, f"It is about project `{project}`, checked out at `{path}` "
                                        f"(its vault note is `01-Projects/{project}.md`).")

    async def ask(self, target: Asked, question: str) -> tuple[str, Asked]:
        """Answer a question read-only. Pass a previous answer's `Asked` (session set) to follow up."""
        if not question.strip():
            raise FlowError("usage: /ask <project|all> <question>")
        token = logs.task_id.set(f"ask:{target.label}")
        try:
            async with self.ask_slots:
                log.info("question%s: %s", " (follow-up)" if target.session else "", question[:300])
                scope = "*" if target.cwd == self.s.projects else target.label
                notes = await asyncio.to_thread(self._notes, f"ask-{'all' if scope == '*' else scope}", scope)
                prompt = question if target.session else _prompt(
                    "ask", scope=target.scope, vault=notes, question=question)
                res = await claude.run(self.s, prompt, target.cwd, claude.ASKER_TOOLS, resume=target.session,
                                       add_dirs=(notes,), timeout=ASK_TIMEOUT,
                                       bind=claude.Bind("asker", scope=scope))
            if not res.ok:
                raise RuntimeError(f"couldn't answer: {_clip(res.text, 500)}")
            log.info("answered (%d chars, $%.2f)", len(res.text), res.cost_usd)
            follow = dataclasses.replace(target, session=res.session_id or target.session)
            footer = f"_{target.label} · ${res.cost_usd:.2f} · reply to this message to follow up_"
            return f"{res.text.strip()}\n\n{footer}", follow
        finally:
            logs.task_id.reset(token)

    def status(self) -> str:
        active = self.store.active()
        if not active:
            return "No active tasks."
        lines = []
        for t in active:
            flag = " ⏳" if t.id in self.jobs else ""
            gate = " — awaiting /approve" if (t.state == "planned" or t.meta.get("review_verdict") == "approve") and not flag else ""
            lines.append(f"{t.id} · {t.project} · {t.state}{flag}{gate}\n   {t.meta.get('title', '')}")
        return "\n".join(lines)

    def show(self, task_id: str) -> str:
        t = self.store.get(task_id)
        n = t.note
        history = "\n".join((n.section("History") or "").splitlines()[-5:])
        return (f"{t.id} · {t.project} · {t.state}\n{t.meta.get('title_line') or t.meta.get('title')}\n"
                f"branch: {t.meta.get('branch') or '-'}   base: {t.meta.get('base_ref') or '-'} "
                f"{(t.meta.get('base') or '')[:8]}   vhost: {t.meta.get('vhost') or '-'}\n\n"
                f"SPEC\n{_clip(n.section('Spec') or '', 1800)}\n\nLAST REVIEW\n{_clip(n.section('Review') or '', 800)}\n\n"
                f"HISTORY\n{history}")

    def diff(self, task_id: str) -> str:
        t = self.store.get(task_id)
        base, branch = t.meta.get("base"), t.meta.get("branch")
        if not (base and branch):
            raise FlowError(f"{task_id} has no branch yet")
        return worktree.git(self._project_dir(t), "diff", f"{base}...{branch}")

    # -- phases --------------------------------------------------------------

    async def _plan(self, task_id: str, feedback: str = "") -> None:
        t = self.store.get(task_id)
        pdir = self._project_dir(t)
        ref, fetched = await self._sync(pdir)
        found = await asyncio.to_thread(sync.overlaps, self.s, pdir, ref)
        self.store.update(task_id, base_ref=ref)
        notes = await asyncio.to_thread(self._notes, task_id, t.project)
        prompt = _prompt(
            "plan", task_id=task_id, project=t.project, project_dir=pdir, vault=notes,
            request=t.note.section("Request") or "",
            feedback=f"\n## Your previous plan was rejected\n\n{feedback}\n" if feedback else "",
            base_ref=ref, sync_note=fetched.note(), overlap_days=self.s.overlap_days,
            overlaps=sync.describe(found) or "(none)",
        )
        await self.n.text(f"🧠 {task_id} planning…")
        res = await claude.run(self.s, prompt, pdir, claude.PLANNER_TOOLS, add_dirs=(notes,),
                               resume=t.meta.get("claude_session", "") if feedback else "",
                               bind=claude.Bind("planner", task_id))
        if not res.ok:
            raise RuntimeError(f"planner failed: {_clip(res.text, 500)}")
        t = self.store.get(task_id)
        title = _TITLE.search(res.text)
        log.info("planner done: state %s, $%.2f, title %r", t.state, res.cost_usd, title.group(0) if title else None)
        self.store.update(task_id, claude_session=res.session_id,
                          title_line=title.group(0).removeprefix("TITLE:").strip() if title else "")
        if t.state == "blocked":  # the planner gave up and asked a question
            await self.n.text(f"❓ {task_id} needs you:\n{_clip((t.note.section('History') or '-').splitlines()[-1], 800)}\n"
                              f"/answer {task_id} <answer> · /stop {task_id}")
            return
        if not (t.note.section("Spec") or "").strip():
            self.store.append(task_id, "Spec", res.text)
        if t.state == "new":
            self.store.transition(task_id, "planned", "bot", "spec written")
        t = self.store.get(task_id)
        hits = sync.mentioned(found, t.note.section("Spec") or "")
        if hits:
            log.warning("spec touches files on other unmerged branches: %s", ", ".join(o.branch for o in hits))
        warn = ("\n\n⚠️ The spec touches files another unmerged branch also changes:\n" + sync.describe(hits)) if hits else ""
        stale = "" if fetched.ok else f"\n\n{fetched.note()}"
        await self.n.text(
            f"📋 {task_id} planned — {t.meta.get('title_line') or t.meta.get('title')}\n\n"
            f"{_clip(t.note.section('Spec') or '', 2500)}{warn}{stale}{self._unsafe()}\n\n"
            f"/approve {task_id} · /reject {task_id} <why> · /show {task_id}"
        )

    async def _gate_plan(self, t: tasks.Task) -> str:
        busy = [x for x in self.store.active() if x.state in ("coding", "review") and x.meta.get("worktree")]
        if len(busy) >= self.s.max_workers:
            raise FlowError(f"{len(busy)} workers busy (max {self.s.max_workers}): {', '.join(x.id for x in busy)}")
        clash = [x.id for x in busy if x.project == t.project]
        if clash:
            raise FlowError(f"{t.project} already has a worker ({clash[0]}); one per project")
        pdir = self._project_dir(t)
        ref, fetched = await self._sync(pdir)
        base = await asyncio.to_thread(sync.sha, pdir, ref)
        kind, _, summary = _title_parts(t)
        branch = worktree.branch_name(kind, t.id, summary, self.s.branch_prefix,
                                      taken=lambda name: sync.branch_taken(self.s, pdir, name))
        wt = await asyncio.to_thread(worktree.create, self.s, pdir, t.id, branch, base)
        guard = await asyncio.to_thread(gitsafe.fingerprint, wt)
        host, note = "", ""
        vh = projects.find_vhost(self.s, pdir)
        if vh:
            try:
                host = await asyncio.to_thread(vhost.create, self.s, vh, t.id, wt)
            except vhost.VhostError as e:
                log.warning("no temporary vhost: %s", e)
                note = f"\n⚠️ no temporary vhost: {_clip(str(e), 300)}"
        else:
            note = "\n⚠️ no devcrate vhost serves this project; HTTP checks will be skipped"
        self.store.transition(t.id, "coding", "bot", f"plan approved; base {ref} {base[:8]}", branch=branch,
                              base=base, base_ref=ref, worktree=str(wt), vhost=host, bounces=0, review_verdict="",
                              git_guard=guard)
        log.info("worktree %s on %s from %s @ %s, vhost %s", wt, branch, ref, base[:8], host or "-")
        self._spawn(t.id, self._work(t.id))
        return (f"🔨 {t.id} approved — worker started on `{branch}` from {ref} @ {base[:8]} ({fetched.note()})"
                + (f", https://{host}" if host else "") + note)

    async def _work(self, task_id: str, fixup: str = "", commit_message: str = "",
                    conflicts: list[str] | None = None) -> None:
        t = self.store.get(task_id)
        wt = Path(t.meta["worktree"])
        await self.oc.ensure_server()
        session = t.meta.get("opencode_session") or await self.oc.create_session(wt, f"devflow {task_id}")
        self.store.update(task_id, opencode_session=session)
        prompt = _prompt("worker", task_id=task_id, project=t.project, branch=t.meta["branch"],
                         spec=t.note.section("Spec") or "",
                         fixup=f"## Fix-up requested (do this now)\n\n{fixup}\n" if fixup else "")
        await self.n.text(f"🔨 {task_id} worker {'fixing up' if fixup else 'coding'}…")
        async def denied(permission: str, patterns: list[str]) -> None:
            what = f"{permission} {', '.join(patterns)[:300]}".strip()
            log.warning("worker permission denied: %s", what)
            self.store.append(task_id, "Worker log", f"- {tasks.now()} denied: `{what}`")
            await self.n.text(f"🚫 {task_id} worker asked for `{what}` — rejected (unattended policy)")

        log.info("worker prompt sent (session %s, %s)", session, "fix-up" if fixup else "first pass")
        reply = await self.oc.prompt(session, wt, prompt, WORKER_TIMEOUT, on_denied=denied)
        log.info("worker replied (%d chars)", len(reply))

        t = self.store.get(task_id)
        if reply:
            self.store.append(task_id, "Worker log", f"#### {tasks.now()} worker reply\n\n{_clip(reply, 3000)}")
        gave_up = _BLOCKED.search(reply or "")
        if gave_up:
            why = gave_up.group(1).strip() or "no reason given"
            self.store.transition(task_id, "blocked", "bot", f"worker gave up: {why}")
            await self.n.text(f"❓ {task_id} worker gave up: {_clip(why, 600)}\n"
                              f"/show {task_id} · /retry {task_id} · /stop {task_id}")
            return

        if conflicts:
            left = await asyncio.to_thread(sync.markers_left, wt, conflicts)
            if left:
                raise RuntimeError(f"conflict markers still in {', '.join(left)}; resolve by hand in {wt} "
                                   f"or /stop {task_id}")
        kind, scope, summary = _title_parts(t)
        head = f"{kind}({scope})" if scope else kind
        message = commit_message or (
            (f"{head}: {summary}" if not fixup else f"{head}: address review of {task_id}") +
            f"\n\n- devflow task {task_id}")
        await self._check_git(t, wt)
        sha = await asyncio.to_thread(worktree.commit, self.s, wt, message)
        log.info("commit %s", sha[:10] if sha else "(nothing changed)")
        if sha is None and not fixup:
            raise RuntimeError("the worker changed nothing")
        await self._verify_and_review(task_id)

    async def _verify_and_review(self, task_id: str) -> None:
        t = self.store.get(task_id)
        wt, pdir = Path(t.meta["worktree"]), self._project_dir(t)
        changed = await asyncio.to_thread(worktree.changed_files, wt, t.meta["base"])
        vh = projects.find_vhost(self.s, pdir)
        php = projects.php_exe(self.s, vh.port) if vh else None
        ev = await verify.run(task_id, t.note.section("Spec") or "", wt, changed, php,
                              vh.host if vh else None, t.meta.get("vhost") or None)

        ref, fetched = await self._sync(pdir)
        clean, conflicts = await asyncio.to_thread(sync.merge_check, wt, ref)
        found = await asyncio.to_thread(sync.overlaps, self.s, pdir, ref, (t.meta["branch"],))
        touching = sync.describe(found, changed)
        shared = (f"**Shared branch** `{ref}` ({fetched.note()}): "
                  + ("merges cleanly ✅" if clean else f"❌ conflicts in {', '.join(f'`{c}`' for c in conflicts)} "
                     f"— /refresh {task_id}"))
        if touching:
            shared += f"\n\n**Other unmerged branches touching the same files** (yours included):\n{touching}"
        evidence = f"{ev.markdown}\n\n{shared}"
        log.log(logging.INFO if ev.ok and clean else logging.WARNING,
                "verification %s, %d files changed, merge into %s %s, screenshot %s",
                "passed" if ev.ok else "failed", len(changed), ref, "clean" if clean else f"conflicts {conflicts}",
                "yes" if ev.screenshot else "no")

        self.store.append(task_id, "Verification", f"{tasks.now()}\n\n{evidence}")
        self.store.transition(task_id, "review", "bot",
                              "worker done, verification " + ("passed" if ev.ok else "failed") +
                              ("" if clean else f", conflicts with {ref}"))
        await self._review(task_id, evidence, ev.screenshot)

    async def _refresh(self, task_id: str, ref: str) -> None:
        t = self.store.get(task_id)
        pdir, wt, branch = self._project_dir(t), Path(t.meta["worktree"]), t.meta["branch"]
        new_base = await asyncio.to_thread(sync.sha, pdir, ref)
        # A branch nobody else has seen is rebased (clean history). Once it is on the remote,
        # rewriting it would need a force-push, so it gets a merge instead.
        if not sync.on_remote(self.s, pdir, branch):
            if (await asyncio.to_thread(worktree.run, wt, "rebase", ref)).returncode == 0:
                self.store.update(task_id, base=new_base, base_ref=ref)
                await self.n.text(f"🔄 {task_id} rebased cleanly onto {ref}; verifying again…")
                await self._verify_and_review(task_id)
                return
            log.info("rebase onto %s conflicted; falling back to a merge", ref)
            await asyncio.to_thread(worktree.run, wt, "rebase", "--abort")
        merge = await asyncio.to_thread(worktree.run, wt, "merge", "--no-ff", "--no-commit", ref)
        self.store.update(task_id, base=new_base, base_ref=ref)
        message = f"chore(merge): merge {ref} into {branch}\n\n- devflow task {task_id}"
        if merge.returncode == 0:
            await asyncio.to_thread(worktree.commit, self.s, wt, message)
            await self.n.text(f"🔄 {task_id} merged {ref} cleanly; verifying again…")
            await self._verify_and_review(task_id)
            return
        files = await asyncio.to_thread(sync.unmerged, wt)
        log.warning("merge of %s conflicted in %s", ref, files or "(no files; see stderr)")
        if not files:
            raise RuntimeError(f"merge of {ref} failed: {_clip(merge.stderr or merge.stdout, 400)}")
        await self.n.text(f"⚔️ {task_id} conflicts with {ref} in {', '.join(files)} — worker resolving…")
        fix = (f"Merging `{ref}` (colleagues' work) into this branch conflicted in: "
               f"{', '.join(f'`{f}`' for f in files)}.\n\nResolve every conflict marker "
               f"(the <<<<<<<, ======= and >>>>>>> blocks) so that BOTH the change from `{ref}` and this "
               f"task's spec keep working. Change nothing else. Do not run git; the bot completes the merge.")
        await self._work(task_id, fixup=fix, commit_message=message, conflicts=files)

    async def _review(self, task_id: str, evidence: str, shot: Path | None) -> None:
        t = self.store.get(task_id)
        wt = Path(t.meta["worktree"])
        rnd = int(t.meta.get("bounces") or 0) + 1
        prompt = _prompt("review", task_id=task_id, project=t.project, branch=t.meta["branch"],
                         base=t.meta["base"], round=rnd, max_rounds=self.s.max_bounces + 1, evidence=evidence)
        await self.n.text(f"🔍 {task_id} reviewing (round {rnd})…")
        before = self.store.get(task_id).note.section("Review") or ""
        notes = await asyncio.to_thread(self._notes, task_id, t.project)
        res = await claude.run(self.s, prompt, wt, claude.REVIEWER_TOOLS, add_dirs=(notes,),
                               bind=claude.Bind("reviewer", task_id))
        if not res.ok:
            raise RuntimeError(f"reviewer failed: {_clip(res.text, 500)}")
        m = _VERDICT.search(res.text)
        log.info("review round %d: %s, $%.2f", rnd, m.group(1) if m else "no verdict", res.cost_usd)
        if not m:
            raise RuntimeError(f"reviewer gave no verdict: {_clip(res.text, 300)}")
        if (self.store.get(task_id).note.section("Review") or "") == before:  # reviewer wrote nothing via MCP
            self.store.append(task_id, "Review", f"#### Round {rnd}\n\n{res.text}")

        if m.group(1) == "APPROVE":
            self.store.update(task_id, review_verdict="approve")
            stat = await asyncio.to_thread(worktree.diff, wt, t.meta["base"], True)
            caption = (f"✅ {task_id} reviewed and ready — {t.meta.get('title_line') or t.meta.get('title')}\n"
                       f"branch `{t.meta['branch']}`" + (f" · https://{t.meta['vhost']}" if t.meta.get("vhost") else "") +
                       f"\n\n{_clip(stat, 1200)}\n\n{_clip(evidence, 1200)}{self._unsafe()}\n\n"
                       f"/approve {task_id} · /reject {task_id} <why> · /diff {task_id}")
            await self.n.text(caption)
            if shot:
                await self.n.photo(shot, f"{task_id} after the change")
            return

        fix = m.group(2).strip()
        if rnd > self.s.max_bounces:
            self.store.transition(task_id, "blocked", "bot", f"still failing review after {rnd} rounds",
                                  review_verdict="fixup")
            await self.n.text(f"⛔ {task_id} failed review {rnd} times.\n{_clip(fix, 1500)}\n"
                              f"/diff {task_id} · /retry {task_id} · /stop {task_id}")
            return
        self.store.transition(task_id, "coding", "bot", f"review round {rnd}: fix-up", bounces=rnd, review_verdict="fixup")
        await self.n.text(f"🔁 {task_id} review round {rnd} asked for fixes:\n{_clip(fix, 1200)}")
        await self._work(task_id, fixup=fix)

    async def _cleanup(self, task_id: str) -> str:
        """Remove the temp vhost and the worktree. A failure is logged and returned as a warning
        for the human, never raised: leftovers must not stop a task being closed or cancelled.
        Whatever couldn't be removed stays in the note, so the next try goes after it again."""
        t = self.store.get(task_id)
        problems, done = [], {}
        if t.meta.get("vhost"):
            try:
                await asyncio.to_thread(vhost.remove, self.s, task_id)
                done["vhost"] = ""
            except Exception as e:
                log.exception("could not remove the temp vhost")
                problems.append(f"temp vhost `{t.meta['vhost']}`: {_clip(str(e), 300)}")
        if t.meta.get("worktree"):
            wt = Path(t.meta["worktree"])
            await self.oc.dispose(wt)  # else opencode's file watcher keeps the folder locked
            try:
                await asyncio.to_thread(worktree.remove, self._project_dir(t), wt)
                done["worktree"] = ""
            except Exception as e:
                log.exception("could not remove the worktree")
                problems.append(f"worktree `{wt}`: {_clip(str(e), 300)}")
        if done:
            self.store.update(task_id, **done)
        if not problems:
            return ""
        return ("\n\n⚠️ Couldn't clean up:\n" + "\n".join(f"- {p}" for p in problems) +
                f"\n/retry {task_id} tries again; see Troubleshooting to do it by hand.")

    async def _close(self, task_id: str) -> None:
        """Clean up, record the result, let the closer note what's worth keeping. Each step is
        marked done in the note, so /retry after a failure picks up where it stopped."""
        leftovers = await self._cleanup(task_id)
        t = self.store.get(task_id)
        pdir = self._project_dir(t)
        if not t.meta.get("result_written"):
            commits = worktree.git(pdir, "log", "--oneline", f"{t.meta['base']}..{t.meta['branch']}")
            self.store.append(task_id, "Result",
                              f"Branch `{t.meta['branch']}` from `{t.meta.get('base_ref') or 'base'}` @ `{t.meta['base'][:10]}`"
                              f" — merged cleanly in simulation at approval; not merged, not pushed.\n\n"
                              f"```\n{commits}\n```")
            self.store.update(task_id, result_written=True)
        notes = await asyncio.to_thread(self._notes, task_id, t.project)
        note_path = self.s.vault / tasks.PROJECTS_DIR / f"{t.project}.md"
        before = note_path.read_text(encoding="utf-8") if note_path.is_file() else ""
        res = await claude.run(self.s, _prompt("close", task_id=task_id, project=t.project, branch=t.meta["branch"]),
                               pdir, claude.CLOSER_TOOLS, add_dirs=(notes,), timeout=600,
                               bind=claude.Bind("closer", task_id))
        if not res.ok:
            log.error("closer failed: %s", _clip(res.text, 500))
        self.store.update(task_id, closed=not leftovers)
        recorded = _clip(res.text, 400) if res.ok else "(closer failed; nothing recorded)"
        after = note_path.read_text(encoding="utf-8") if note_path.is_file() else ""
        added = _added(before, after)
        written = (f"\n\n📝 Added to the {t.project} project note (every later run reads it):\n{_clip(added, 1200)}"
                   if added else "")
        await self.n.text(f"🏁 {task_id} done. Branch `{t.meta['branch']}` is ready to merge.\n"
                          f"{recorded}{written}{leftovers}")

    async def close(self) -> None:
        for job in list(self.jobs.values()):
            job.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        await self.oc.close()


__all__ = ["Asked", "Flow", "FlowError", "Notifier", "VaultError"]
