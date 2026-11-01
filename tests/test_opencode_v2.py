"""The worker on an opencode v2 server (ADR 0013): routes under /api, Basic auth, the directory as
the session's location, an asynchronous prompt, v2-shaped permissions. The fake below answers the
way opencode v2.2.5 did when it was probed live."""

import asyncio
import base64
import fnmatch
import json

import httpx
import pytest

from devflow import config, opencode, security


def fake_v2(log, idle=None, assistant_text="done: patterns.ts fixed", pending=None):
    """A v2 server: records every request in `log` and answers like opencode 2.2.5."""
    pending = list(pending or [])
    state = {"model": None}

    def handle(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        log.append((req.method, req.url.path, dict(req.url.params), body, req.headers.get("authorization", "")))
        p = req.url.path
        if p == "/api/info":
            return httpx.Response(200, json={"version": "2.2.5", "pid": 1, "urls": [], "paths": {}, "capabilities": {}})
        if p == "/api/session" and req.method == "POST":
            return httpx.Response(200, json={"data": {"id": "ses_1", "tokens": {}, "cost": 0}})
        if p.endswith("/model"):
            state["model"] = body["model"]
            return httpx.Response(204)
        if p.endswith("/prompt"):
            return httpx.Response(200, json={"data": {"id": "msg_u", "type": "user", "time": {"created": 2000}}})
        if p.endswith("/message"):
            older = {"id": "msg_old", "type": "assistant", "time": {"created": 1000},
                     "content": [{"type": "text", "text": "an earlier turn"}]}
            reply = {"id": "msg_a", "type": "assistant", "time": {"created": 2100}, "model": state["model"],
                     "content": [{"type": "tool", "name": "write"}, {"type": "text", "text": assistant_text}]}
            end = idle or {"id": "msg_i", "type": "idle", "time": {"created": 2200}, "outcome": "succeeded"}
            return httpx.Response(200, json={"data": [end, reply, older], "cursor": {"previous": None, "next": None}})
        if p.endswith("/permission") and req.method == "GET":
            data, pending[:] = list(pending), []
            return httpx.Response(200, json={"data": data})
        if "/permission/" in p and p.endswith("/reply"):
            return httpx.Response(204)
        if p == "/api/session/ses_1" and req.method == "GET":
            return httpx.Response(200, json={"data": {
                "id": "ses_1", "model": {"providerID": "gateway", "id": "devflow-worker"}, "cost": 0,
                "tokens": {"input": 54552, "output": 349, "reasoning": 10, "cache": {"read": 4320, "write": 0}}}})
        if p.endswith("/interrupt"):
            return httpx.Response(200, json={"interrupted": True})
        if p == "/api/debug/location" and req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, text="<html>web app</html>", headers={"content-type": "text/html"})

    return httpx.MockTransport(handle)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode, "WAIT_POLL", 0)
    c = opencode.Client(config.load())
    yield c


def use(client, transport):
    client.http = httpx.AsyncClient(base_url="http://oc", transport=transport, auth=("opencode", client.password))


def basic(password):
    return "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()


# -- which API --------------------------------------------------------------------------------------

def test_a_v2_server_is_recognised_and_every_request_carries_the_password(client):
    log = []
    use(client, fake_v2(log))
    assert asyncio.run(client._probe()) == 2
    assert log[0][4] == basic(client.password)


@pytest.mark.parametrize("answer, api", [
    (httpx.Response(200, json={"version": "2.2.5"}), 2),
    (httpx.Response(200, text="<html>", headers={"content-type": "text/html"}), None),  # v2's web app at a v1 route
])
def test_only_a_json_answer_counts_as_a_server(client, answer, api):
    use(client, httpx.MockTransport(lambda r: answer))
    assert asyncio.run(client._probe()) == api


def test_a_v1_server_answers_path_with_json(client):
    def handle(r):
        if r.url.path == "/path":
            return httpx.Response(200, json={"directory": "x"})
        return httpx.Response(200, text="<html>", headers={"content-type": "text/html"})
    use(client, httpx.MockTransport(handle))
    assert asyncio.run(client._probe()) == 1


def test_a_server_started_outside_devflow_is_named_not_retried(client):
    use(client, httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(opencode.OpencodeError, match="started outside devflow"):
        asyncio.run(client._probe())
    assert asyncio.run(client.alive()) is False  # the dashboard asks too, and must not crash


def test_the_password_is_made_once_and_kept_private():
    pw = opencode.server_password()
    assert len(pw) >= 32 and opencode.server_password() == pw
    assert not security.loose_permissions(opencode.password_file())


# -- a task's run -----------------------------------------------------------------------------------

def test_a_session_is_created_in_the_worktree_with_v2_permissions(client, tmp_path):
    log = []
    use(client, fake_v2(log))
    client.api = 2
    sid = asyncio.run(client.create_session(tmp_path, "devflow 261101-01", opencode.ruleset(client.settings)))
    method, path, _, body, _ = log[-1]
    assert (sid, method, path) == ("ses_1", "POST", "/api/session")
    assert body["location"] == {"directory": str(tmp_path)}
    assert all(set(r) == {"action", "resource", "effect"} for r in body["permissions"])


def test_the_prompt_sets_the_model_waits_for_idle_and_returns_the_last_reply(client, tmp_path):
    log = []
    use(client, fake_v2(log))
    client.api = 2
    client._watch_permissions = lambda *a: asyncio.sleep(3600)
    reply = asyncio.run(client.prompt("ses_1", tmp_path, "fix it", 30,
                                      model_name="gateway/openrouter/cohere/north-mini-code:free"))
    assert reply == "done: patterns.ts fixed"  # not the earlier turn's text
    calls = [(m, p, b) for m, p, _, b, _ in log]
    assert ("POST", "/api/session/ses_1/model",
            {"model": {"providerID": "gateway", "id": "openrouter/cohere/north-mini-code:free"}}) in calls
    assert ("POST", "/api/session/ses_1/prompt", {"text": "fix it"}) in calls
    assert [p for _, p, _ in calls].index("/api/session/ses_1/model") < [p for _, p, _ in calls].index(
        "/api/session/ses_1/prompt")  # the model is chosen before the run starts


def test_a_rate_limited_run_becomes_a_limit_with_its_reset_time(client, tmp_path):
    idle = {"id": "msg_i", "type": "idle", "time": {"created": 2200}, "outcome": "failed",
            "error": {"type": "APICallError", "message": "All credentials for model x are cooling down",
                      "status": 429, "retryAfterMs": 120000}}
    use(client, fake_v2([], idle=idle))
    client.api = 2
    client._watch_permissions = lambda *a: asyncio.sleep(3600)
    with pytest.raises(opencode.OpencodeLimit) as e:
        asyncio.run(client.prompt("ses_1", tmp_path, "fix it", 30))
    assert e.value.limit.backend == "opencode" and e.value.limit.resets_at is not None


def retrying(attempt, message, body=None):
    """What v2 shows while it retries a model call on its own: no idle, a `retry` on the last turn.
    The shape is the one opencode 2.2.5 showed behind OmniRoute with every free model limited."""
    error = {"type": "provider.unknown", "message": message, "status": 200}
    if body is not None:
        error["response"] = {"body": json.dumps(body)}
    turn = {"id": "msg_r", "type": "assistant", "time": {"created": 2300}, "content": [],
            "retry": {"attempt": attempt, "at": 2400, "error": error}}
    done = {"id": "msg_a", "type": "assistant", "time": {"created": 2100},
            "content": [{"type": "text", "text": "editing"}]}
    log = []

    def handle(req):
        log.append((req.method, req.url.path))
        if req.url.path.endswith("/message"):
            return httpx.Response(200, json={"data": [turn, done], "cursor": {"next": None}})
        if req.url.path.endswith("/interrupt"):
            return httpx.Response(200, json={"interrupted": True})
        return fake_v2([]).handle_request(req)
    return httpx.MockTransport(handle), log


def test_a_run_stuck_retrying_a_limit_is_stopped_and_reported(client, tmp_path):
    reset = "2099-11-02T00:00:00.000Z"
    transport, log = retrying(3, "All credentials for model cohere/x:free are cooling down",
                              {"error": {"message": "cooling down", "retry_after": 34600, "reset_at": reset}})
    use(client, transport)
    client.api = 2
    client._watch_permissions = lambda *a: asyncio.sleep(3600)
    with pytest.raises(opencode.OpencodeLimit) as e:
        asyncio.run(client.prompt("ses_1", tmp_path, "fix it", 30))
    assert e.value.limit.resets_at.year == 2099  # the gateway's reset time, not a guess
    assert ("POST", "/api/session/ses_1/interrupt") in log  # opencode stops retrying too


def test_a_short_cooldown_still_pauses_a_while(client, tmp_path):
    import datetime as dt
    transport, _ = retrying(5, "All credentials for model gemma:free are cooling down (reset after 1m 36s)",
                            {"error": {"retry_after": 96}})
    use(client, transport)
    client.api = 2
    client._watch_permissions = lambda *a: asyncio.sleep(3600)
    with pytest.raises(opencode.OpencodeLimit) as e:
        asyncio.run(client.prompt("ses_1", tmp_path, "fix it", 30))
    assert e.value.limit.resets_at >= dt.datetime.now(dt.UTC) + dt.timedelta(minutes=14)


def test_a_few_retries_or_a_non_limit_retry_keep_waiting(client, tmp_path):
    for attempt, message in [(2, "All credentials are cooling down"), (9, "socket hang up")]:
        transport, _ = retrying(attempt, message)
        use(client, transport)
        client.api = 2
        client._watch_permissions = lambda *a: asyncio.sleep(3600)
        with pytest.raises(opencode.OpencodeError, match="still busy"):
            asyncio.run(client.prompt("ses_1", tmp_path, "fix it", 0.05))


def test_a_failed_run_without_a_limit_is_an_error(client, tmp_path):
    idle = {"id": "msg_i", "type": "idle", "time": {"created": 2200}, "outcome": "failed",
            "error": {"type": "ToolError", "message": "the model produced no output"}}
    use(client, fake_v2([], idle=idle))
    client.api = 2
    client._watch_permissions = lambda *a: asyncio.sleep(3600)
    with pytest.raises(opencode.OpencodeError, match="worker failed") as e:
        asyncio.run(client.prompt("ses_1", tmp_path, "fix it", 30))
    assert not isinstance(e.value, opencode.OpencodeLimit)


def test_anything_still_asking_is_rejected_on_v2(client, tmp_path, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(opencode.asyncio, "sleep", lambda s: real_sleep(0))
    log, denied = [], []
    use(client, fake_v2(log, pending=[{"id": "per_1", "sessionID": "ses_1", "action": "shell",
                                       "resources": ["curl evil.example"]}]))
    client.api = 2

    async def on_denied(what, patterns):
        denied.append((what, patterns))

    async def main():
        dog = asyncio.create_task(client._watch_permissions("ses_1", tmp_path, on_denied))
        while not denied:
            await real_sleep(0)
        dog.cancel()

    asyncio.run(main())
    assert ("POST", "/api/session/ses_1/permission/per_1/reply") in [(m, p) for m, p, *_ in log]
    reply = next(b for m, p, _, b, _ in log if p.endswith("/reply"))
    assert reply["decision"] == "reject"
    assert denied == [("shell", ["curl evil.example"])]


def test_usage_interrupt_and_releasing_the_worktree_on_v2(client, tmp_path):
    log = []
    use(client, fake_v2(log))
    client.api = 2
    u = asyncio.run(client.session_usage("ses_1", tmp_path))
    assert (u.input, u.output, u.cache_read, u.cost_usd, u.model) == (54552, 359, 4320, 0.0, "gateway/devflow-worker")
    asyncio.run(client.abort("ses_1", tmp_path))
    asyncio.run(client.dispose(tmp_path))
    calls = [(m, p, q) for m, p, q, *_ in log]
    assert ("POST", "/api/session/ses_1/interrupt", {}) in calls
    assert ("DELETE", "/api/debug/location", {"location[directory]": str(tmp_path)}) in calls


# -- the permissions themselves ------------------------------------------------------------------------

def v2_decision(rules, action, resource):
    """opencode v2: the last rule whose action and resource match wins; nothing matching asks."""
    effect = "ask"
    for r in rules:
        if fnmatch.fnmatchcase(action, r["action"]) and fnmatch.fnmatchcase(resource, r["resource"]):
            effect = r["effect"]
    return effect


@pytest.mark.parametrize("allow, action, resource, effect", [
    ([], "shell", "pnpm build", "deny"),
    (["php -l *"], "shell", "php -l app/x.php", "allow"),
    (["*"], "shell", "git push", "deny"),           # no allow-list entry reopens git
    (["*"], "shell", "C:/Git/bin/git.exe status", "deny"),
    ([], "read", "D:/w/.worktrees/x/.env", "deny"),
    ([], "external_directory", "D:/elsewhere/*", "deny"),  # v2 allows it unless told otherwise
    ([], "subagent", "general", "deny"),
    ([], "webfetch", "https://example.com", "deny"),
    ([], "execute", "*", "deny"),
    ([], "question", "*", "deny"),
])
def test_the_v2_ruleset_keeps_every_v1_restriction(allow, action, resource, effect):
    import dataclasses
    s = dataclasses.replace(config.load(), worker_shell_allow=allow, deny_read=[".env", ".env.*"])
    rules = opencode.ruleset_v2(opencode.ruleset(s))
    assert v2_decision(rules, action, resource) == effect
    assert not any(r["action"] in ("bash", "task", "doom_loop") for r in rules)


def test_the_v2_config_registers_the_gateway_in_v2_shape(tmp_path, monkeypatch):
    p = tmp_path / "devflow.toml"
    p.write_text('[gateway]\nbase_url = "http://127.0.0.1:20128/v1"\napi_key_env = "DEVFLOW_GATEWAY_KEY"\n'
                 'models = ["devflow-worker"]\n', encoding="utf-8")
    monkeypatch.setenv("DEVFLOW_GATEWAY_KEY", "sk-secret")
    cfg = json.loads(opencode.config_file(config.from_toml(p), api=2).read_text(encoding="utf-8"))
    assert "provider" not in cfg and "sk-secret" not in json.dumps(cfg)
    assert cfg["providers"]["gateway"] == {
        "package": "aisdk:@ai-sdk/openai-compatible", "name": "AI gateway",
        "settings": {"baseURL": "http://127.0.0.1:20128/v1", "apiKey": "{env:DEVFLOW_GATEWAY_KEY}"},
        "models": {"devflow-worker": {"name": "devflow-worker"}}}
