"""The dashboard's routes. Thin like the bot: read tasks and usage, call `Flow` for every action.

    devflow dashboard           the dashboard alone, no Telegram
    devflow run                 the bot, plus the dashboard when [dashboard] enabled = true
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import itertools
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import uvicorn
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import agents, config, gateway, logs, projects, redact, tasks
from ..config import Settings, runtime_dir
from ..pipeline import Asked, FlowError
from ..runtime import Runtime
from ..usage import budget
from ..usage.guard import money
from ..usage.guard import tokens as fmt_tokens
from ..vault import VaultError
from . import auth
from .events import WebNotifier

log = logging.getLogger("devflow.web")

HERE = Path(__file__).parent
HOST = "127.0.0.1"
BOARD = ("new", "planned", "coding", "review", "blocked")
ACTIONS = ("approve", "reject", "retry", "refresh", "stop")
AGENT_TTL = 30.0

_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


def markdown(text: str | None) -> Markup:
    """Markdown from task notes and agents, with raw HTML off and secrets masked."""
    return Markup(_md.render(redact.redact(text or "")))


def diff_lines(diff: str) -> list[tuple[str, str]]:
    out = []
    for line in redact.redact(diff).splitlines():
        kind = ("file" if line.startswith(("diff --git", "+++", "---", "index ")) else
                "hunk" if line.startswith("@@") else
                "add" if line.startswith("+") else "del" if line.startswith("-") else "")
        out.append((kind, line))
    return out


@dataclasses.dataclass
class Conversation:
    id: int
    label: str
    follow: Asked
    turns: list[list[str]] = dataclasses.field(default_factory=list)  # [question, answer or ""]
    busy: bool = False
    error: str = ""


class Dashboard:
    def __init__(self, rt: Runtime, events: WebNotifier, token: str):
        self.rt, self.flow, self.s, self.events, self.token = rt, rt.flow, rt.settings, events, token
        self.csrf = auth.csrf_for(token)
        self.env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
        self.env.filters.update(md=markdown, money=money, tokens=fmt_tokens)
        self.asks: dict[int, Conversation] = {}
        self._ask_ids = itertools.count(1)
        self._jobs: set[asyncio.Task[None]] = set()
        self.probes: list[gateway.Probe] = []
        self.probed_at: dt.datetime | None = None
        self._probing = asyncio.Lock()
        self._agents: tuple[float, list[agents.AgentStatus]] | None = None

    # -- plumbing ---------------------------------------------------------------------------

    def page(self, request: Request, name: str, code: int = 200, **ctx: Any) -> HTMLResponse:
        partial = request.headers.get("hx-request") == "true" and request.headers.get("hx-target") != "body"
        html = self.env.get_template(name).render(
            request=request, csrf=self.csrf, path=request.url.path, partial=partial,
            settings=self.s, waiting=self._waiting(), online=self.online(), **ctx)
        return HTMLResponse(html, code)

    def online(self) -> dict[str, Any] | None:
        """The last model check, summed up for the sidebar and the board; None before the first."""
        if self.probed_at is None:
            return None
        usable = [p for p in self.probes if p.state != "skipped"]
        return {"up": sum(1 for p in usable if p.state == "online"), "of": len(usable), "at": self.probed_at}

    async def agent_status(self, fresh: bool = False) -> list[agents.AgentStatus]:
        """The agents' state, kept AGENT_TTL seconds: the board refreshes every 10 s, and asking
        a stopped opencode server costs about 2 s on Windows."""
        now = asyncio.get_running_loop().time()
        if fresh or self._agents is None or now - self._agents[0] > AGENT_TTL:
            self._agents = (now, await agents.status(self.flow))
        return self._agents[1]

    def _waiting(self) -> int:
        return sum(1 for t in self.flow.store.active() if self.flow.at_gate(t))

    def card(self, t: tasks.Task) -> dict[str, Any]:
        return {"id": t.id, "project": t.project, "state": t.state, "title": t.meta.get("title", ""),
                "running": self.flow.running(t), "gate": self.flow.at_gate(t),
                "reason": t.meta.get("blocked_reason", ""), "branch": t.meta.get("branch", "")}

    def project_names(self) -> list[str]:
        return projects.names(self.s)

    # -- pages ------------------------------------------------------------------------------

    async def login(self, request: Request) -> Response:
        sent = request.query_params.get("token", "")
        if not auth.hmac.compare_digest(sent, self.token):
            return PlainTextResponse("wrong or missing token", 401)
        target = request.query_params.get("next", "/")
        if not target.startswith("/") or target.startswith("//") or "\\" in target:
            target = "/"  # only a path on this dashboard, never another site
        resp = RedirectResponse(target, 303)
        resp.set_cookie(auth.COOKIE, self.token, httponly=True, samesite="strict", path="/")
        return resp

    async def overview(self, request: Request) -> Response:
        everything = self.flow.store.all()
        board: dict[str, list[dict[str, Any]]] = {s: [] for s in BOARD}
        for t in everything:
            if t.state in board:
                board[t.state].append(self.card(t))
        finished = [self.card(t) for t in everything if t.state in tasks.FINAL][-10:][::-1]
        needs = [c for cards in board.values() for c in cards if c["gate"] or c["state"] == "blocked"]
        now = self.flow.spend.now()
        today = budget.windows(self.s.budget, now)["day"][0]
        rows = self.flow.spend.db.rows(since=today)
        spend = _totals(rows)
        return self.page(request, "overview.html", board=board, finished=finished, spend=spend, needs=needs,
                         agents=await self.agent_status(),
                         limits=self.flow.spend.db.limits(), events=self.events.recent(12),
                         meters=budget.meters(self.s.budget, self.flow.spend.db, now))

    async def task(self, request: Request) -> Response:
        t = self.flow.store.get(request.path_params["id"])
        stat = ""
        if t.meta.get("base") and t.meta.get("branch"):
            try:
                stat = await asyncio.to_thread(self.flow.diff_stat, t.id)
            except Exception as e:  # a missing branch must not hide the rest of the task
                stat = f"(no diff: {e})"
        shot = runtime_dir() / "shots" / f"{t.id}.png"
        sections = [(name, t.note.section(name) or "") for name in tasks.SECTIONS]
        return self.page(request, "task.html", t=self.card(t), meta=t.meta, sections=sections,
                         stat=redact.redact(stat), shot=shot.is_file(), usage=self.flow.spend.task_total(t.id),
                         events=self.events.recent(15, t.id), actions=self._actions(t))

    def _actions(self, t: tasks.Task) -> list[str]:
        if t.state in tasks.FINAL:
            return []
        acts = []
        if self.flow.at_gate(t):
            acts += ["approve", "reject"]
        if t.state == "blocked":
            acts.append("retry")
        if t.meta.get("branch") and not self.flow.running(t):
            acts.append("refresh")
        acts.append("stop")
        return acts

    async def diff(self, request: Request) -> Response:
        task_id = request.path_params["id"]
        text = await asyncio.to_thread(self.flow.diff, task_id)
        return self.page(request, "diff.html", task_id=task_id, lines=diff_lines(text))

    async def shot(self, request: Request) -> Response:
        task_id = tasks.check_id(request.path_params["id"])
        p = runtime_dir() / "shots" / f"{task_id}.png"
        if not p.is_file():
            return PlainTextResponse("no screenshot", 404)
        return FileResponse(p, media_type="image/png")

    async def act(self, request: Request) -> Response:
        task_id, action = request.path_params["id"], request.path_params["action"]
        if action not in ACTIONS:
            return PlainTextResponse("unknown action", 404)
        form = await request.form()
        reason = str(form.get("reason", "")).strip()
        calls: dict[str, Callable[[], Awaitable[str]]] = {
            "approve": lambda: self.flow.approve(task_id),
            "reject": lambda: self.flow.reject(task_id, reason),
            "retry": lambda: self.flow.retry(task_id),
            "refresh": lambda: self.flow.refresh(task_id),
            "stop": lambda: self.flow.stop(task_id),
        }
        if action == "reject" and not reason:
            return self._flash(request, "✋ say why: the reason goes to the planner or the worker", ok=False)
        log.info("dashboard: %s %s", action, task_id)
        token = logs.task_id.set(task_id)
        try:
            msg = await calls[action]()
        finally:
            logs.task_id.reset(token)
        return self._flash(request, msg, refresh=True)

    def _flash(self, request: Request, message: str, ok: bool = True, refresh: bool = False) -> Response:
        resp = self.page(request, "_flash.html", message=message, ok=ok)
        if refresh:
            resp.headers["HX-Trigger"] = "devflow-changed"
        return resp

    async def new_task(self, request: Request) -> Response:
        if request.method == "GET":
            return self.page(request, "new.html", names=self.project_names(),
                             preset=request.query_params.get("project", ""))
        form = await request.form()
        project, text = str(form.get("project", "")).strip(), str(form.get("request", "")).strip()
        if not project or not text:
            return self._flash(request, "✋ pick a project and describe the task", ok=False)
        t = await self.flow.new_task(project, text)
        resp = Response(status_code=204)
        resp.headers["HX-Redirect"] = f"/tasks/{t.id}"
        return resp

    async def ask_index(self, request: Request) -> Response:
        if request.method == "GET":
            convs = sorted(self.asks.values(), key=lambda c: -c.id)
            return self.page(request, "ask.html", names=self.project_names(), conversations=convs, conv=None)
        form = await request.form()
        query, question = str(form.get("project", "")).strip(), str(form.get("question", "")).strip()
        if not query or not question:
            return self._flash(request, "✋ pick a project (or all) and ask a question", ok=False)
        target = self.flow.ask_target(query)
        conv = Conversation(next(self._ask_ids), target.label, target)
        self.asks[conv.id] = conv
        self._start_answer(conv, question)
        resp = Response(status_code=204)
        resp.headers["HX-Redirect"] = f"/ask/{conv.id}"
        return resp

    async def ask_conv(self, request: Request) -> Response:
        conv = self.asks.get(int(request.path_params["cid"]))
        if conv is None:
            return PlainTextResponse("no such conversation (they are kept until devflow restarts)", 404)
        if request.method == "POST":
            question = str((await request.form()).get("question", "")).strip()
            if question and not conv.busy:
                self._start_answer(conv, question)
        if request.headers.get("hx-request") == "true" and request.method == "GET":
            return self.page(request, "_conversation.html", conv=conv)
        return self.page(request, "ask.html", names=self.project_names(), conv=conv,
                         conversations=sorted(self.asks.values(), key=lambda c: -c.id))

    def _start_answer(self, conv: Conversation, question: str) -> None:
        conv.turns.append([question, ""])
        conv.busy, conv.error = True, ""

        async def answer() -> None:
            try:
                text, conv.follow = await self.flow.ask(conv.follow, question)
                conv.turns[-1][1] = text
            except (FlowError, VaultError) as e:
                conv.error = f"✋ {e}"
            except Exception as e:
                log.exception("dashboard ask failed")
                conv.error = f"💥 {type(e).__name__}: {str(e)[:300]}"
            finally:
                conv.busy = False

        job = asyncio.create_task(answer())
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    async def usage(self, request: Request) -> Response:
        days = 30
        now = self.flow.spend.now()
        since = budget.windows(self.s.budget, now)["day"][0] - dt.timedelta(days=days - 1)
        rows = self.flow.spend.db.rows(since=since)
        zone = budget.tz(self.s.budget)
        per_day: dict[str, list[Any]] = {(since + dt.timedelta(days=i)).astimezone(zone).strftime("%m-%d"): []
                                         for i in range(days)}
        for r in rows:
            per_day.setdefault(r.ts.astimezone(zone).strftime("%m-%d"), []).append(r)
        daily = [(day, _totals(rs)) for day, rs in per_day.items()]
        peak = max((d[1]["tokens"] for d in daily), default=0) or 1
        groups = {}
        for key in ("backend", "role", "model"):
            g: dict[str, list[Any]] = defaultdict(list)
            for r in rows:
                g[getattr(r, key) or "?"].append(r)
            groups[key] = sorted(((k, _totals(v)) for k, v in g.items()), key=lambda kv: -kv[1]["tokens"])
        per_task: dict[str, list[Any]] = defaultdict(list)
        for r in rows:
            per_task[r.task].append(r)
        top = sorted(((k, _totals(v)) for k, v in per_task.items()), key=lambda kv: -kv[1]["tokens"])[:10]
        return self.page(request, "usage.html", daily=daily, peak=peak, groups=groups, top=top, days=days,
                         total=_totals(rows), meters=budget.meters(self.s.budget, self.flow.spend.db, now))

    async def projects_page(self, request: Request) -> Response:
        rows = []
        busy = {t.project: t.id for t in self.flow.store.active() if t.meta.get("worktree")}
        for name in self.project_names():
            ps = self.s.for_project(name)
            try:
                where, err = str(projects.directory(self.s, name)), ""
            except ValueError as e:
                where, err = "", str(e)
            checks = ps.checks
            rows.append({"name": name, "path": where, "error": err, "base": ps.base_branch or "(remote default)",
                         "model": ps.opencode_model or "(opencode default)", "task": busy.get(name, ""),
                         "checks": ([f"preset {checks.preset}"] if checks.preset else []) + checks.lint + checks.test})
        found = await asyncio.to_thread(projects.candidates, self.s) if self.s.registry is not None else []
        return self.page(request, "projects.html", rows=rows, candidates=[(p.name, p.as_posix()) for p in found],
                         editable=self.s.registry is not None)

    async def add_project(self, request: Request) -> Response:
        """Append `[projects.<name>]` to the config file and use it at once, no restart."""
        if self.s.registry is None:
            return self._flash(request, "✋ legacy mode: add a note in the vault's 01-Projects, or run "
                                        "`devflow migrate-env` to get a devflow.toml", ok=False)
        form = await request.form()
        raw = str(form.get("path", "")).strip().strip('"')
        if not raw:
            return self._flash(request, "✋ give the folder of the project's git checkout", ok=False)
        folder = Path(raw).expanduser()
        name = str(form.get("name", "")).strip() or folder.name
        if not tasks.PROJECT_RE.match(name) or ".." in name:
            return self._flash(request, f"✋ `{name}` can't be a project name: use letters, digits, `.`, `_` or `-`",
                               ok=False)
        if name in (self.s.registry or {}):
            return self._flash(request, f"✋ there is already a project named `{name}`; give this one another name",
                               ok=False)
        if not folder.is_absolute() or not folder.is_dir():
            return self._flash(request, f"✋ `{raw}` is not a folder on this computer (give the full path)", ok=False)
        if not (folder / ".git").exists():
            return self._flash(request, f"✋ `{folder}` is not a git checkout: devflow works on branches of a repository",
                               ok=False)
        if any(p.path.resolve() == folder.resolve() for p in (self.s.registry or {}).values()):
            return self._flash(request, f"✋ `{folder}` is already registered", ok=False)
        loaded = await asyncio.to_thread(config.append_project, Path(self.s.source), name, folder)
        fresh = dataclasses.replace(self.s, registry=loaded.registry)
        self.use_settings(fresh)
        log.info("dashboard: project %s added (%s)", name, folder)
        resp = self._flash(request, f"✅ Added **{name}**. It is in the New task and Ask lists now.")
        resp.headers["HX-Trigger"] = "devflow-changed"
        return resp

    def use_settings(self, settings: Settings) -> None:
        self.s = settings
        self.rt.settings = settings
        self.flow.use_settings(settings)

    async def models_page(self, request: Request) -> Response:
        gw = self.s.gateway
        status = await gateway.acheck(gw) if gw else None
        overrides = [(n, self.s.for_project(n).opencode_model) for n in self.project_names()
                     if self.s.for_project(n).opencode_model != self.s.opencode_model]
        combos = [await gateway.acheck_combo(gw, c) for c in gw.free_combos] if gw and gw.free_only else []
        if gw and self.probed_at is None:  # the first visit checks; after that, on request
            await self._probe()
        # Up when it lists its models or a model answered: a slow model list alone isn't "down".
        answering = any(p.state == "online" for p in self.probes)
        gw_up = bool(status and (status.up or answering))
        return self.page(request, "models.html", gw=gw, status=status, gw_up=gw_up, overrides=overrides,
                         combos=combos, probes=self.probes, probed_at=self.probed_at,
                         agents=await self.agent_status(fresh=True))

    async def models_check(self, request: Request) -> Response:
        if not self.s.gateway:
            return self._flash(request, "✋ no gateway configured", ok=False)
        await self._probe()
        return self.page(request, "_probes.html", probes=self.probes, probed_at=self.probed_at)

    async def _probe(self) -> None:
        gw = self.s.gateway
        if gw is None:
            return
        async with self._probing:  # two clicks don't double the requests
            self.probes = await gateway.probe_all(gw)
            self.probed_at = dt.datetime.now()
        log.info("models checked: %s", ", ".join(f"{p.model}={p.state}" for p in self.probes))

    async def help_page(self, request: Request) -> Response:
        return self.page(request, "help.html")

    async def logs_page(self, request: Request) -> Response:
        which = request.query_params.get("show", "errors")
        task = request.query_params.get("task", "")
        if task and not tasks.ID_RE.fullmatch(task):
            task = ""
        p = logs.path(errors=which == "errors")
        entries = [redact.redact(e) for e in logs.tail(p, 200 if which == "all" else 60, task=task)][::-1]
        return self.page(request, "logs.html", entries=entries, show=which, task=task)

    async def events_fragment(self, request: Request) -> Response:
        task = request.query_params.get("task", "")
        return self.page(request, "_events.html", events=self.events.recent(30 if not task else 15, task))

    # -- the app ----------------------------------------------------------------------------

    def app(self) -> Starlette:
        def safe(fn: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
            async def wrapped(request: Request) -> Response:
                try:
                    return await fn(request)
                except (FlowError, VaultError) as e:
                    if request.headers.get("hx-request") == "true":
                        return self._flash(request, f"✋ {e}", ok=False)
                    return self.page(request, "error.html", 400, message=str(e))
                except Exception as e:
                    log.exception("dashboard %s %s failed", request.method, request.url.path)
                    msg = f"💥 {type(e).__name__}: {str(e)[:300]}"
                    if request.headers.get("hx-request") == "true":
                        return self._flash(request, msg, ok=False)
                    return self.page(request, "error.html", 500, message=msg)
            return wrapped

        routes = [
            Route("/login", self.login),
            Route("/", safe(self.overview)),
            Route("/tasks/new", safe(self.new_task), methods=["GET", "POST"]),
            Route("/tasks/{id}", safe(self.task)),
            Route("/tasks/{id}/diff", safe(self.diff)),
            Route("/tasks/{id}/shot.png", safe(self.shot)),
            Route("/tasks/{id}/{action}", safe(self.act), methods=["POST"]),
            Route("/ask", safe(self.ask_index), methods=["GET", "POST"]),
            Route("/ask/{cid:int}", safe(self.ask_conv), methods=["GET", "POST"]),
            Route("/usage", safe(self.usage)),
            Route("/projects", safe(self.projects_page)),
            Route("/projects/add", safe(self.add_project), methods=["POST"]),
            Route("/models", safe(self.models_page)),
            Route("/models/check", safe(self.models_check), methods=["POST"]),
            Route("/gateway", lambda r: RedirectResponse("/models", 301)),
            Route("/help", safe(self.help_page)),
            Route("/logs", safe(self.logs_page)),
            Route("/fragments/events", safe(self.events_fragment)),
            Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
        ]
        app = Starlette(routes=routes)
        app.add_middleware(auth.Guard, token=self.token, port=self.s.dashboard.port)
        return app


def _totals(rows: list[Any]) -> dict[str, Any]:
    costs = [r.usage.cost_usd for r in rows if r.usage.cost_usd is not None]
    return {"tokens": sum(r.usage.tokens for r in rows), "cost": sum(costs) if costs else None, "runs": len(rows),
            "failed": sum(1 for r in rows if r.outcome != "ok")}


def login_url(settings: Settings, token: str | None = None) -> str:
    return f"http://{HOST}:{settings.dashboard.port}/login?token={token or auth.load_token()}"


# -- running it ---------------------------------------------------------------------------------

class _Embedded(uvicorn.Server):
    """Beside the Telegram bot, which owns the process's signals: leave them alone."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class Site:
    def __init__(self, server: uvicorn.Server, task: asyncio.Task[None]):
        self.server, self.task = server, task

    async def stop(self) -> None:
        self.server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(self.task, 10)


def _config(rt: Runtime, events: WebNotifier) -> uvicorn.Config:
    token = auth.load_token()
    redact.add(token)  # never into a log or a Telegram message
    dash = Dashboard(rt, events, token)
    return uvicorn.Config(dash.app(), host=HOST, port=rt.settings.dashboard.port, log_level="warning",
                          access_log=False, lifespan="off")


def _announce(settings: Settings) -> None:
    print(f"devflow dashboard: {login_url(settings)}", flush=True)  # stdout only, not the log file
    log.info("dashboard listening on http://%s:%d", HOST, settings.dashboard.port)


async def start_embedded(rt: Runtime) -> Site:
    """Start the dashboard inside an event loop that is already running (the bot's)."""
    events = WebNotifier()
    rt.notifier.add(events)
    server = _Embedded(_config(rt, events))
    task = asyncio.create_task(server.serve())
    _announce(rt.settings)
    return Site(server, task)


def run_standalone(settings: Settings) -> None:
    """`devflow dashboard`: the Flow with the dashboard as its only front end."""
    logs.setup("devflow")
    for name in ("mcp", "mcp-errors"):  # as the bot does: rotate while no MCP server runs
        logs.roll(name)
    async def main() -> None:
        logs.watch_loop(asyncio.get_running_loop())
        rt = Runtime(settings)
        events = WebNotifier()
        rt.notifier.add(events)
        server = uvicorn.Server(_config(rt, events))
        _announce(settings)
        await rt.start()
        await rt.flow.n.text("🟢 devflow up (dashboard only)")
        try:
            await server.serve()
        finally:
            await rt.stop()

    asyncio.run(main())

