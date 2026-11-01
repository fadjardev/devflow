"""The local dashboard (ADR 0011): who gets in, what it shows, and that every action goes through Flow."""

import asyncio
import dataclasses
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from devflow import config, opencode, redact
from devflow.runtime import FanoutNotifier, Runtime
from devflow.web import auth, server
from devflow.web.events import WebNotifier

BASE = "http://127.0.0.1:8765"


@pytest.fixture(autouse=True)
def _no_real_cli(monkeypatch):
    """Pages show the agents' versions; tests never run the real claude or opencode for them."""
    from devflow import agents

    async def version(exe, timeout=15):
        return "test"

    async def alive(self):
        return False

    monkeypatch.setattr(agents, "version", version)
    monkeypatch.setattr(opencode.Client, "alive", alive)  # a refused connection takes ~2 s on Windows


@pytest.fixture
def rt(vault):
    settings = dataclasses.replace(config.load(), vault=vault)
    return Runtime(settings)


@pytest.fixture
def dash(rt):
    events = WebNotifier()
    rt.notifier.add(events)
    return server.Dashboard(rt, events, "t" * 43)


@pytest.fixture
def client(dash):
    c = TestClient(dash.app(), base_url=BASE)
    c.cookies.set(auth.COOKIE, dash.token)
    return c


def csrf(dash):
    return {"X-CSRF": dash.csrf}


# -- who gets in ------------------------------------------------------------------------------

def test_a_request_with_another_host_name_is_refused(dash):
    """A DNS-rebinding page reaches 127.0.0.1 under its own host name."""
    c = TestClient(dash.app(), base_url="http://evil.example:8765")
    c.cookies.set(auth.COOKIE, dash.token)
    assert c.get("/").status_code == 400


def test_without_the_cookie_only_login_and_static_files_answer(dash):
    c = TestClient(dash.app(), base_url=BASE)
    assert c.get("/").status_code == 401
    assert c.get("/static/app.css").status_code == 200
    assert c.get("/login?token=wrong").status_code == 401
    r = c.get(f"/login?token={dash.token}", follow_redirects=False)
    assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert c.get("/").status_code == 200


def test_a_post_needs_the_csrf_token_and_the_same_origin(client, dash, task, monkeypatch):
    called = []

    async def approve(task_id):
        called.append(task_id)
        return f"🔨 {task_id} approved"

    monkeypatch.setattr(dash.flow, "approve", approve)
    assert client.post(f"/tasks/{task.id}/approve").status_code == 403
    assert client.post(f"/tasks/{task.id}/approve", headers={"X-CSRF": "nope"}).status_code == 403
    assert client.post(f"/tasks/{task.id}/approve",
                       headers={**csrf(dash), "Origin": "http://evil.example"}).status_code == 403
    assert called == []
    r = client.post(f"/tasks/{task.id}/approve", headers={**csrf(dash), "Origin": BASE})
    assert r.status_code == 200 and "approved" in r.text and r.headers["HX-Trigger"] == "devflow-changed"
    r = client.post(f"/tasks/{task.id}/approve", data={"csrf": dash.csrf})  # a plain form works too
    assert r.status_code == 200
    assert called == [task.id, task.id]


def test_the_token_is_made_once_kept_private_and_masked():
    tok = auth.load_token()
    assert len(tok) >= 32 and auth.load_token() == tok
    assert auth.csrf_for(tok) != tok
    redact.add(tok)
    assert tok not in redact.redact(f"login with {tok}")


# -- what it shows ------------------------------------------------------------------------------

def test_the_board_shows_a_planned_task_waiting_at_gate_1(client, dash, store, task):
    store.transition(task.id, "planned", "bot", "spec written")
    assert dash.flow.at_gate(store.get(task.id)) == 1
    html = client.get("/").text
    assert task.id in html and "gate 1" in html and 'class="card at-gate"' in html


def test_agent_text_is_rendered_as_markdown_without_raw_html(client, store, task):
    store.append(task.id, "Spec", "### Goal\n\nUse **bold**.\n\n<script>alert(1)</script>", actor="agent")
    html = client.get(f"/tasks/{task.id}").text
    assert "<strong>bold</strong>" in html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_send_back_needs_a_reason_and_passes_it_on(client, dash, task, monkeypatch):
    got = []

    async def reject(task_id, reason):
        got.append((task_id, reason))
        return "↩ sent back"

    monkeypatch.setattr(dash.flow, "reject", reject)
    r = client.post(f"/tasks/{task.id}/reject", data={"csrf": dash.csrf, "reason": "  "})
    assert "say why" in r.text and got == []
    client.post(f"/tasks/{task.id}/reject", data={"csrf": dash.csrf, "reason": "keep the old route"})
    assert got == [(task.id, "keep the old route")]


def test_a_refused_action_comes_back_as_a_message_not_an_error_page(client, dash, task):
    r = client.post(f"/tasks/{task.id}/approve", headers={**csrf(dash), "HX-Request": "true"})
    assert r.status_code == 200 and "✋" in r.text  # a new task is not at a gate


def test_a_new_task_goes_through_flow_and_redirects_to_it(client, dash, store, monkeypatch):
    async def new_task(project, text):
        return store.create(project, text[:40], "Request x", text)

    monkeypatch.setattr(dash.flow, "new_task", new_task)
    r = client.post("/tasks/new", data={"csrf": dash.csrf, "project": "demo.app", "request": "add sorting"})
    assert r.status_code == 204 and r.headers["HX-Redirect"].startswith("/tasks/")


@pytest.mark.parametrize("path", ["/", "/tasks/new", "/ask", "/usage", "/projects", "/models", "/gateway", "/help",
                                  "/logs", "/logs?show=all", "/fragments/events"])
def test_every_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]


def test_the_projects_page_lists_the_registry(client):
    assert "demo.app" in client.get("/projects").text


def test_an_unknown_task_is_a_clean_400(client):
    r = client.get("/tasks/991231-99")
    assert r.status_code == 400 and "no such task" in r.text


def test_a_screenshot_is_served_only_for_a_task_id(client, task, tmp_path):
    shots = config.runtime_dir() / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    (shots / f"{task.id}.png").write_bytes(b"\x89PNG fake")
    assert client.get(f"/tasks/{task.id}/shot.png").content == b"\x89PNG fake"
    assert client.get("/tasks/..%2F..%2Fsecret/shot.png").status_code in (400, 404)


def test_diff_lines_are_classified_and_masked():
    redact.add("sk-ant-api03-SECRETSECRETSECRET")
    lines = server.diff_lines("diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+key = sk-ant-api03-SECRETSECRETSECRET\n ctx")
    assert [k for k, _ in lines] == ["file", "hunk", "del", "add", ""]
    assert "SECRETSECRETSECRET" not in lines[3][1]


# -- notifications ------------------------------------------------------------------------------

def test_the_web_notifier_keeps_messages_with_their_task():
    n = WebNotifier()
    asyncio.run(n.text("📝 261101-01 created for shop. Planning…"))
    asyncio.run(n.photo(Path("261101-01.png"), "261101-01 after the change"))
    asyncio.run(n.text("🟢 devflow up"))
    assert [e.task for e in n.recent()] == ["", "261101-01", "261101-01"]
    assert n.recent(task="261101-01")[0].photo == "261101-01.png"


def test_one_failing_front_end_does_not_silence_the_others():
    class Broken:
        async def text(self, message):
            raise RuntimeError("telegram down")

        async def photo(self, path, caption):
            raise RuntimeError("telegram down")

    web = WebNotifier()
    fan = FanoutNotifier(Broken(), web)
    asyncio.run(fan.text("hello"))
    asyncio.run(fan.photo(Path("x.png"), "shot"))
    assert [e.text for e in web.recent()] == ["shot", "hello"]


def test_login_returns_only_to_a_path_on_the_dashboard(dash):
    c = TestClient(dash.app(), base_url=BASE)
    for target, expect in (("/usage", "/usage"), ("https://evil.example/", "/"), ("//evil.example", "/"),
                           ("/\\evil.example", "/")):
        r = c.get("/login", params={"token": dash.token, "next": target}, follow_redirects=False)
        assert r.headers["location"] == expect, target


def test_the_models_page_checks_once_then_on_request(client, dash, monkeypatch):
    from devflow import gateway
    calls = []

    async def probe_all(gw, parallel=4):
        calls.append(gw.base_url)
        return [gateway.Probe("openrouter/a:free", "online", "answered", 0.8, "a:free", "devflow-worker"),
                gateway.Probe("openrouter/b:free", "limited", "All credentials are cooling down", 0.2, combo="devflow-worker")]

    async def acheck(gw, timeout=5):
        return gateway.Status(True, "2 models served", ["devflow-worker"], [])

    monkeypatch.setattr(gateway, "probe_all", probe_all)
    monkeypatch.setattr(gateway, "acheck", acheck)
    dash.s = dataclasses.replace(dash.s, gateway=config.GatewayConfig(base_url="http://gw/v1", models=["devflow-worker"]))
    html = client.get("/models").text
    assert "1 of 2 models answered" in html and "rate limited" in html and "cooling down" in html
    client.get("/models")
    assert len(calls) == 1  # the second visit uses the last check
    r = client.post("/models/check", headers=csrf(dash))
    assert r.status_code == 200 and len(calls) == 2
    assert "1/2" in client.get("/").text  # the sidebar count


def test_the_usage_page_breaks_runs_down_by_backend_role_and_model(client, dash):
    """Regression: a template variable in the layout once hid these tables' data."""
    import datetime as dt

    from devflow.usage import Usage
    from devflow.usage.db import Row
    dash.flow.spend.db.record(Row(dt.datetime.now(dt.UTC), "261101-01", "worker", "opencode",
                                  "gateway/devflow-worker", Usage(1200, 300, 0, 0, None, ""), 5.0, "ok"))
    html = client.get("/usage").text
    assert "No runs yet" not in html
    assert ">opencode<" in html and ">worker<" in html and "gateway/devflow-worker" in html


# -- projects added from the dashboard ------------------------------------------------------------

@pytest.fixture
def toml_dash(tmp_path):
    """A dashboard on a devflow.toml with one project, and a workspace with two more repositories."""
    ws = tmp_path / "ws"
    for repo in ("Github/shop", "Github/blog", "Gitlab/api"):
        (ws / repo / ".git").mkdir(parents=True)
    (ws / "Github" / "notes").mkdir()  # not a repository: never offered
    cfg = tmp_path / "devflow.toml"
    cfg.write_text(f'# my settings\n[paths]\nprojects = "{(ws / "Github").as_posix()}"\n\n'
                   f'[projects.shop]\npath = "{(ws / "Github" / "shop").as_posix()}"\n', encoding="utf-8")
    rt = Runtime(config.from_toml(cfg))
    events = WebNotifier()
    rt.notifier.add(events)
    d = server.Dashboard(rt, events, "t" * 43)
    c = TestClient(d.app(), base_url=BASE)
    c.cookies.set(auth.COOKIE, d.token)
    return d, c, cfg, ws


def test_unregistered_repositories_are_offered_and_one_can_be_added_without_a_restart(toml_dash):
    d, c, cfg, ws = toml_dash
    page = c.get("/projects").text
    assert (ws / "Github" / "blog").as_posix() in page and (ws / "Gitlab" / "api").as_posix() in page
    assert (ws / "Github" / "notes").as_posix() not in page and 'value="' + (ws / "Github" / "shop").as_posix() not in page

    r = c.post("/projects/add", data={"csrf": d.csrf, "path": str(ws / "Gitlab" / "api")})
    assert "Added **api**" in r.text or "Added <strong>api</strong>" in r.text
    text = cfg.read_text(encoding="utf-8")
    assert text.startswith("# my settings") and '[projects.api]\npath = "' in text  # appended, nothing rewritten
    assert config.from_toml(cfg).registry["api"].path == (ws / "Gitlab" / "api").resolve()
    assert "api" in projects_in(c, "/tasks/new") and "api" in projects_in(c, "/ask")
    assert d.flow.store.known("api")  # tasks for it are accepted at once


def projects_in(c, path):
    import re
    return re.findall(r"<option(?:\s[^>]*)?>([^<]+)</option>", c.get(path).text)


@pytest.mark.parametrize("form, message", [
    ({"path": ""}, "give the folder"),
    ({"path": "relative/dir"}, "not a folder"),
    ({"path": "{ws}/Github/notes"}, "not a git checkout"),
    ({"path": "{ws}/Github/shop", "name": "shop2"}, "already registered"),
    ({"path": "{ws}/Github/blog", "name": "shop"}, "already a project named"),
    ({"path": "{ws}/Github/blog", "name": "../x"}, "can't be a project name"),
])
def test_a_project_that_cannot_work_is_refused_and_the_config_is_left_alone(toml_dash, form, message):
    d, c, cfg, ws = toml_dash
    before = cfg.read_text(encoding="utf-8")
    data = {k: v.format(ws=ws.as_posix()) for k, v in form.items()}
    r = c.post("/projects/add", data={"csrf": d.csrf, **data})
    assert message in r.text
    assert cfg.read_text(encoding="utf-8") == before


def test_legacy_mode_cannot_add_projects(client, dash):
    r = client.post("/projects/add", data={"csrf": dash.csrf, "path": "C:/x"})
    assert "legacy mode" in r.text


# -- agents ---------------------------------------------------------------------------------------

def test_the_agents_show_install_state_limits_and_todays_use(client, dash, monkeypatch):
    import datetime as dt

    from devflow import agents
    from devflow.usage import Limit, Usage
    from devflow.usage.db import Row

    monkeypatch.setattr(agents.shutil, "which", lambda exe: "C:/bin/claude.exe" if exe == "claude" else None)

    async def version(exe, timeout=15):
        return "2.5.0 (Claude Code)"

    async def alive():
        return False

    monkeypatch.setattr(agents, "version", version)
    monkeypatch.setattr(dash.flow.oc, "alive", alive)
    now = dt.datetime.now(dt.UTC)
    dash.flow.spend.db.record(Row(now, "261101-01", "planner", "claude", "sonnet", Usage(1000, 200, 0, 0, 0.1, ""),
                                  3.0, "ok"))
    dash.flow.spend.db.set_limit(Limit("claude", True, "5-hour limit reached", now + dt.timedelta(hours=2)), now)
    html = client.get("/models").text
    assert "Claude Code" in html and "2.5.0 (Claude Code)" in html and "5-hour limit reached" in html
    assert ">limited<" in html and ">missing<" in html  # opencode isn't on PATH here
    assert "1 runs, 1k tokens" in html
    assert "Claude Code: limited" in client.get("/").text
