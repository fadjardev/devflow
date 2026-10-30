"""Token usage, budgets and provider limits (PRD §5.7, §10 v0.2), with fixture outputs from
both backends and a fake clock."""

import asyncio
import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path

import pytest
from test_pipeline import FakeOpencode, Notes, settle, setup  # noqa: F401  (setup is a fixture)

from devflow import claude, cli, config, pipeline, usage
from devflow.config import BudgetConfig, Caps, CheckConfig
from devflow.pipeline import Flow, FlowError
from devflow.usage import Usage, budget
from devflow.usage.db import UsageDB
from devflow.usage.guard import Spending

FIX = Path(__file__).parent / "fixtures"
UTC = dt.UTC


class Clock:
    def __init__(self, at: dt.datetime):
        self.at = at

    def __call__(self) -> dt.datetime:
        return self.at

    def advance(self, **kw) -> None:
        self.at += dt.timedelta(**kw)


def stream(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# -- parsing the backends' own output ------------------------------------------------------

def test_claude_stream_fixture_gives_tokens_cost_and_model():
    result, events, errors = claude.parse_stream(stream("claude_stream_ok.jsonl"))
    u = usage.claude_usage(result)
    assert u.model == "claude-haiku-5-5" and u.cost_usd == pytest.approx(0.00434172)
    assert (u.input, u.output, u.cache_read, u.cache_write) == (2, 4, 14252, 20985)
    assert usage.claude_limit(result, events, errors) is None  # "allowed" is not a warning


def test_claude_usage_limit_fixture_is_a_hit_with_the_reset_time():
    result, events, errors = claude.parse_stream(stream("claude_stream_limit.jsonl"))
    lim = usage.claude_limit(result, events, errors)
    assert lim is not None and lim.hit and lim.backend == "claude"
    assert lim.resets_at == dt.datetime.fromtimestamp(1793337000, UTC)


def test_reset_times_in_milliseconds_are_understood_too():
    lim = usage.claude_limit({}, [{"rate_limit_info": {"status": "rejected", "resetsAt": 1793337000123}}], [])
    assert lim.resets_at == dt.datetime.fromtimestamp(1793337000.123, UTC)


def test_opencode_fixture_sums_the_sessions_assistant_messages():
    u = usage.opencode_usage(json.loads((FIX / "opencode_messages.json").read_text(encoding="utf-8")))
    assert (u.input, u.output, u.cache_read, u.cache_write) == (15000, 1400, 72000, 1500)
    assert u.cost_usd == pytest.approx(0.02) and u.model == "openrouter/qwen/qwen3-coder"
    assert usage.opencode_usage([{"info": {"role": "assistant", "tokens": {"input": 5}}}]).cost_usd is None


def test_opencode_429_is_a_limit_with_retry_after():
    lim = usage.opencode_limit(json.loads((FIX / "opencode_error_429.json").read_text(encoding="utf-8")))
    assert lim is not None and lim.hit and lim.backend == "opencode" and lim.resets_at is not None
    assert usage.opencode_limit({"name": "ProviderAuthError", "data": {"message": "bad key"}}) is None


def test_a_resumed_session_records_only_what_the_run_added(tmp_path):
    db = UsageDB(tmp_path / "u.db")
    first = db.delta("claude", "s1", Usage(100, 10, 0, 0, 0.10))
    second = db.delta("claude", "s1", Usage(250, 30, 0, 0, 0.25))
    assert (first.input, first.cost_usd) == (100, 0.10)
    assert (second.input, second.output, second.cost_usd) == (150, 20, pytest.approx(0.15))


# -- the pipeline records both backends --------------------------------------------------

class MeteredWorker(FakeOpencode):
    """Each prompt adds 1000 input / 100 output tokens and $0.02 to the session."""

    def __init__(self):
        super().__init__()
        self.total = Usage(cost_usd=0.0)

    async def prompt(self, session, directory, text, timeout, on_denied=None):
        self.total = Usage(self.total.input + 1000, self.total.output + 100, 0, 0, (self.total.cost_usd or 0) + 0.02)
        return await super().prompt(session, directory, text, timeout, on_denied)

    async def session_usage(self, session, directory):
        return self.total


def metered_claude(monkeypatch, cost=0.5, limit_on=None):
    """Claude runs that cost `cost` each; the role named in `limit_on` hits the fixture's limit."""
    base = pipeline.claude.run
    seen = {}
    ctl = {"limit_on": limit_on}

    async def run(s, prompt, cwd, tools, resume="", add_dirs=(), timeout=1800, bind=None):
        role = bind.role if bind else "?"
        if role == ctl["limit_on"]:
            result, events, errors = claude.parse_stream(stream("claude_stream_limit.jsonl"))
            return claude.Result(result["result"], "sess-limit", False, 0.01, usage.claude_usage(result),
                                 usage.claude_limit(result, events, errors))
        res = await base(s, prompt, cwd, tools, resume, add_dirs, timeout, bind)
        session = res.session_id or f"sess-{role}"
        prev = seen.get(session, 0.0) + cost  # Claude reports a session's running total
        seen[session] = prev
        return dataclasses.replace(res, session_id=session, cost_usd=prev,
                                   usage=Usage(int(prev * 10000), int(prev * 1000), 0, 0, prev, "claude-sonnet-5-5"))

    monkeypatch.setattr(pipeline.claude, "run", run)
    return ctl


def budgeted(flow, cfg: BudgetConfig, clock: Clock, db: UsageDB) -> Flow:
    flow.s = dataclasses.replace(flow.s, budget=cfg)
    flow.spend = Spending(cfg, db, flow.notify, clock)
    return flow


NOON = dt.datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def test_usage_rows_for_claude_and_opencode_and_usage_by_task(setup, monkeypatch, tmp_path):
    flow = budgeted(setup[0], BudgetConfig(timezone="UTC"), Clock(NOON), UsageDB(tmp_path / "u.db"))
    flow.oc = MeteredWorker()
    metered_claude(monkeypatch)

    async def scenario():
        t = await flow.new_task("demo", "make the demo work")
        await settle(flow, t.id)
        await flow.approve(t.id)
        await settle(flow, t.id)
        return t.id

    task = asyncio.run(scenario())
    rows = flow.spend.db.rows(task=task)
    assert {(r.role, r.backend) for r in rows} == {("planner", "claude"), ("worker", "opencode"),
                                                   ("reviewer", "claude")}
    worker = next(r for r in rows if r.backend == "opencode")
    assert (worker.usage.input, worker.usage.output, worker.usage.cost_usd) == (1000, 100, pytest.approx(0.02))

    report = flow.spend.usage_report(task)
    total = sum(r.usage.cost_usd or 0 for r in rows)
    assert f"${total:.2f}" in report.splitlines()[-1] and len(report.splitlines()) == len(rows) + 2
    assert "💰 so far" in flow.n.texts[-1]  # the gate-2 footer
    assert "planner" in flow.spend.usage_report("today") and "Top tasks" in flow.spend.usage_report("week")


# -- budgets ------------------------------------------------------------------------------

def test_crossing_80_percent_alerts_once_even_after_a_restart(tmp_path):
    clock, db, sent = Clock(NOON), UsageDB(tmp_path / "u.db"), []

    async def notify(m):
        sent.append(m)

    cfg = BudgetConfig(Caps(daily_usd=5.0), timezone="UTC")
    spend = Spending(cfg, db, notify, clock)

    async def run(cost):
        await spend.after("261030-01", "planner", "claude", "", Usage(100, 10, 0, 0, cost), 1.0, True, None)

    asyncio.run(run(2.0))
    assert sent == []
    asyncio.run(run(1.0))  # 60%
    assert len(sent) == 1 and "50%" in sent[0]
    asyncio.run(run(1.2))  # 84%
    assert len(sent) == 2 and "80% of the daily budget" in sent[1] and "$4.20 / $5.00" in sent[1]
    assert "resets 00:00" in sent[1]

    restarted = Spending(cfg, UsageDB(tmp_path / "u.db"), notify, clock)
    asyncio.run(restarted.after("261030-02", "planner", "claude", "", Usage(1, 1, 0, 0, 0.1), 1.0, True, None))
    assert len(sent) == 2  # 86%: no second 80% alert

    clock.advance(days=1)  # a new day: a new window
    asyncio.run(restarted.after("261031-01", "planner", "claude", "", Usage(1, 1, 0, 0, 4.5), 1.0, True, None))
    assert len(sent) == 3 and "90%" not in sent[2] and "80%" in sent[2]


def test_pause_new_refuses_task_and_ask_but_running_tasks_finish(setup, monkeypatch, tmp_path):
    clock = Clock(NOON)
    flow = budgeted(setup[0], BudgetConfig(Caps(daily_usd=1.0), on_exceed="pause-new", timezone="UTC"),
                    clock, UsageDB(tmp_path / "u.db"))
    metered_claude(monkeypatch, cost=0.6)

    async def scenario():
        t = await flow.new_task("demo", "make the demo work")  # the planner spends $0.60
        await settle(flow, t.id)
        await flow.approve(t.id)  # gate 1: the reviewer takes it to $1.20, past the cap
        await settle(flow, t.id)
        assert flow.store.get(t.id).state == "review"  # the running task finished its phases
        with pytest.raises(FlowError, match=r"daily budget is used up \(\$1\.20 / \$1\.00\), resets 00:00"):
            await flow.new_task("demo", "another one")
        with pytest.raises(FlowError, match="Paused"):
            await flow.ask(flow.ask_target("demo"), "where is login?")
        assert any("used up" in m and "paused" in m for m in flow.n.texts)

    asyncio.run(scenario())


def test_stop_blocks_a_running_task_at_its_next_phase_boundary(setup, monkeypatch, tmp_path):
    clock = Clock(NOON)
    flow = budgeted(setup[0], BudgetConfig(Caps(daily_usd=0.5), on_exceed="stop", timezone="UTC"),
                    clock, UsageDB(tmp_path / "u.db"))
    metered_claude(monkeypatch, cost=0.6)

    async def scenario():
        t = await flow.new_task("demo", "make the demo work")  # the planner runs and goes over
        await settle(flow, t.id)
        assert flow.store.get(t.id).state == "planned"  # a phase is never stopped midway
        await flow.approve(t.id)
        await settle(flow, t.id)
        return flow.store.get(t.id)

    t = asyncio.run(scenario())
    assert t.state == "blocked" and t.meta["blocked_reason"] == "limit" and t.meta["blocked_from"] == "coding"
    assert t.meta["resume_after"] == "2026-10-31T00:00:00+00:00"
    assert "paused before work" in flow.n.texts[-1]


def test_a_task_past_its_own_cap_blocks_and_retry_allows_one_more(setup, monkeypatch, tmp_path):
    flow = budgeted(setup[0], BudgetConfig(Caps(per_task_usd=0.5), timezone="UTC"), Clock(NOON),
                    UsageDB(tmp_path / "u.db"))
    metered_claude(monkeypatch, cost=0.6)

    async def scenario():
        t = await flow.new_task("demo", "make the demo work")
        await settle(flow, t.id)
        await flow.approve(t.id)
        await settle(flow, t.id)
        blocked = flow.store.get(t.id)
        assert blocked.state == "blocked" and blocked.meta["blocked_reason"] == "budget"
        await flow.retry(t.id)
        await settle(flow, t.id)
        return flow.store.get(t.id)

    t = asyncio.run(scenario())
    assert t.state == "review" and t.meta["budget_rounds"] == 1


# -- provider limits -----------------------------------------------------------------------

def test_a_claude_limit_blocks_with_the_reset_time_and_auto_resumes(setup, monkeypatch, tmp_path):
    reset = dt.datetime.fromtimestamp(1793337000, UTC)
    clock = Clock(reset - dt.timedelta(hours=2))
    flow = budgeted(setup[0], BudgetConfig(timezone="UTC", auto_resume=True), clock, UsageDB(tmp_path / "u.db"))
    ctl = metered_claude(monkeypatch, limit_on="reviewer")

    async def scenario():
        t = await flow.new_task("demo", "make the demo work")
        await settle(flow, t.id)
        await flow.approve(t.id)
        await settle(flow, t.id)
        blocked = flow.store.get(t.id)
        assert blocked.state == "blocked" and blocked.meta["blocked_reason"] == "limit"
        assert dt.datetime.fromisoformat(blocked.meta["resume_after"]) == reset
        assert any("🛑 claude: usage or rate limit reached" in m and "resumes on its own" in m for m in flow.n.texts)
        with pytest.raises(FlowError, match="Not now: claude"):
            await flow.ask(flow.ask_target("demo"), "anything?")

        assert await flow.resume_due() == []  # not yet
        clock.advance(hours=2, minutes=1)
        ctl["limit_on"] = None  # the reviewer works again after the reset
        assert await flow.resume_due() == [t.id]
        await settle(flow, t.id)
        return flow.store.get(t.id)

    t = asyncio.run(scenario())
    assert t.state == "review" and t.meta["blocked_reason"] == ""
    assert any("resumed" in m and t.id in m for m in flow.n.texts)


def test_limits_shows_budgets_with_percent_and_reset(tmp_path):
    db = UsageDB(tmp_path / "u.db")
    cfg = BudgetConfig(Caps(daily_usd=5.0, monthly_tokens=1_000_000), timezone="UTC", on_exceed="pause-new")
    spend = Spending(cfg, db, lambda m: asyncio.sleep(0), Clock(NOON))
    db.record(budget_row(2.5, 250_000))
    text = spend.limits_report()
    assert "daily budget: $2.50 / $5.00 (50%) · resets 00:00" in text
    assert "monthly token budget: 250,000 / 1,000,000 tokens (25%) · resets 01 Nov 00:00" in text
    assert "when used up: pause-new" in text and "Provider limits" in text


def budget_row(cost, toks):
    from devflow.usage.db import Row
    return Row(NOON, "261030-01", "planner", "claude", "m", Usage(toks, 0, 0, 0, cost), 1.0, "ok")


def test_budget_windows_follow_the_configured_time_zone():
    cfg = BudgetConfig(timezone="Asia/Jakarta")
    day, nxt = budget.windows(cfg, dt.datetime(2026, 10, 30, 20, 0, tzinfo=UTC))["day"]  # 03:00 the next day there
    assert day.isoformat() == "2026-10-31T00:00:00+07:00" and nxt.isoformat() == "2026-11-01T00:00:00+07:00"


def test_budget_config_from_toml(tmp_path):
    f = tmp_path / "devflow.toml"
    f.write_text('[budget]\ndaily_usd = 5\nper_task_usd = 2.5\nwarn_at = [0.8, 0.5]\non_exceed = "stop"\n'
                 'timezone = "UTC"\n[budget.backend.claude]\ndaily_usd = 4.0\n[limits]\nauto_resume = true\n',
                 encoding="utf-8")
    b = config.from_toml(f).budget
    assert b.caps == Caps(daily_usd=5.0, per_task_usd=2.5) and b.backends == {"claude": Caps(daily_usd=4.0)}
    assert b.warn_at == [0.5, 0.8] and b.on_exceed == "stop" and b.auto_resume
    f.write_text('[budget]\non_exceed = "panic"\n', encoding="utf-8")
    with pytest.raises(config.ConfigError, match="on_exceed"):
        config.from_toml(f)


# -- other v0.2 acceptance criteria -------------------------------------------------------

def test_a_project_test_command_without_a_vhost_reaches_gate_2_with_its_result(setup):
    flow = setup[0]
    probe = f'"{sys.executable}" -c "print(\'3 passed\')"'
    flow.s = dataclasses.replace(flow.s, vhost_provider="none", checks=CheckConfig(test=[probe]))

    async def scenario():
        t = await flow.new_task("demo", "make the demo work")
        await settle(flow, t.id)
        await flow.approve(t.id)
        await settle(flow, t.id)
        return flow.store.get(t.id)

    t = asyncio.run(scenario())
    assert t.state == "review"
    assert "**test `" in (t.note.section("Verification") or "") and "passed" in t.note.section("Verification")
    assert "reviewed and ready" in flow.n.texts[-1]


def test_ask_works_without_a_vault(setup, tmp_path):
    flow, repo = setup[0], setup[1]
    s = dataclasses.replace(flow.s, vault=tmp_path / "own", vault_is_own=True,
                            registry={"demo.app": config.Project("demo.app", repo)})
    f = Flow(s, Notes())
    text, _ = asyncio.run(f.ask(f.ask_target("demo"), "where is login?"))
    assert text.startswith("recorded nothing")  # the fake answer
    text, _ = asyncio.run(f.ask(f.ask_target("all"), "which projects use php?"))
    assert "all projects" in text


def test_no_config_at_all_names_the_missing_setting(monkeypatch, capsys):
    for key in ("VAULT_PATH", "PROJECTS_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "REPO_ROOT", Path("/nonexistent-devflow"))
    with pytest.raises(SystemExit, match="VAULT_PATH and PROJECTS_PATH not set"):
        cli.main(["doctor"])
