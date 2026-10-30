"""The whole pipeline against real git repos — a bare "origin", your checkout, and a
colleague's clone that pushes — with Claude and opencode faked."""

import asyncio
import dataclasses
import os
import subprocess
from pathlib import Path

import pytest

from devflow import claude, config, pipeline, sync, worktree
from devflow.pipeline import Flow, FlowError


def g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit_file(repo, name, text, msg):
    (Path(repo) / name).write_text(text)
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "-m", msg)


class Notes:
    def __init__(self):
        self.texts, self.photos = [], []

    async def text(self, message):
        self.texts.append(message)

    async def photo(self, path, caption):
        self.photos.append(path)


class FakeOpencode:
    """First prompt: the task's change. Conflict prompts: resolve markers in shared.txt."""

    def __init__(self):
        self.prompts, self.disposed = [], []

    async def ensure_server(self):
        pass

    async def create_session(self, directory, title):
        return "ses_fake"

    async def prompt(self, session, directory, text, timeout, on_denied=None):
        self.prompts.append(text)
        if "conflicted in" in text:
            (directory / "shared.txt").write_text("one\nCOLLEAGUE and TASK\nthree\n")
            return "resolved"
        n = len(self.prompts)
        if n == 1:
            (directory / "shared.txt").write_text("one\nTASK\nthree\n")
        (directory / f"change{n}.txt").write_text(f"round {n}\n")
        return f"wrote change{n}.txt"

    async def abort(self, session, directory):
        pass

    async def dispose(self, directory):
        self.disposed.append(Path(directory))

    async def close(self):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    g(tmp_path, "init", "-q", "--bare", "-b", "development", str(origin))

    seed = tmp_path / "seed"
    g(tmp_path, "clone", "-q", str(origin), str(seed))
    for args in (["config", "user.email", "s@s"], ["config", "user.name", "seed"]):
        g(seed, *args)
    g(seed, "checkout", "-q", "-b", "development")
    commit_file(seed, "shared.txt", "one\ntwo\nthree\n", "init")
    g(seed, "push", "-q", "origin", "development")

    projects_dir = tmp_path / "projects"
    repo = projects_dir / "demo.app"
    g(tmp_path, "clone", "-q", "-b", "development", str(origin), str(repo))
    for args in (["config", "user.email", "me@me"], ["config", "user.name", "me"]):
        g(repo, *args)

    colleague = tmp_path / "colleague"
    g(tmp_path, "clone", "-q", "-b", "development", str(origin), str(colleague))
    for args in (["config", "user.email", "c@c"], ["config", "user.name", "azid"]):
        g(colleague, *args)

    vault = tmp_path / "vault"
    (vault / "01-Projects").mkdir(parents=True)
    (vault / "01-Projects" / "demo.app.md").write_text(
        f"---\ntitle: demo.app\npath: \"{repo.as_posix()}\"\n---\n\n# demo.app\n\n## Decisions\n", encoding="utf-8")

    settings = dataclasses.replace(config.load(), vault=vault, projects=projects_dir,
                                   devcrate_root=tmp_path / "devcrate", link_paths=[], max_bounces=2,
                                   base_branch="development", git_remote="origin", branch_prefix="devflow")
    ctl = {"reviews": ["VERDICT: APPROVE"], "plan_prompts": []}
    calls = []

    async def fake_claude(s, prompt, cwd, tools, resume="", add_dirs=(), timeout=1800, bind=None):
        calls.append(tools)
        if tools is claude.PLANNER_TOOLS:
            ctl["plan_prompts"].append(prompt)
            return claude.Result("TITLE: fix(demo): make the demo work", "sess-plan", True)
        if tools is claude.REVIEWER_TOOLS:
            return claude.Result(ctl["reviews"].pop(0), "sess-rev", True)
        return claude.Result("recorded nothing", "sess-close", True)

    monkeypatch.setattr(pipeline.claude, "run", fake_claude)
    flow = Flow(settings, Notes())
    flow.oc = FakeOpencode()
    return flow, repo, colleague, ctl, calls


async def settle(flow, task_id):
    while task_id in flow.jobs:
        await asyncio.gather(flow.jobs[task_id], return_exceptions=True)


async def plan_and_code(flow, text="make the demo work"):
    t = await flow.new_task("demo", text)
    await settle(flow, t.id)
    await flow.approve(t.id)
    await settle(flow, t.id)
    return flow.store.get(t.id)


def test_full_task_with_fixup_round(setup):
    flow, repo, _, ctl, calls = setup
    ctl["reviews"] = ["VERDICT: FIXUP\n1. also add change2", "VERDICT: APPROVE"]

    async def scenario():
        t = await flow.new_task("demo", "make the demo work\nplease")
        await settle(flow, t.id)
        assert flow.store.get(t.id).state == "planned"
        with pytest.raises(FlowError, match="say why"):
            await flow.reject(t.id, "")
        msg = await flow.approve(t.id)
        assert f"devflow/fix/{t.id}-make-the-demo-work" in msg
        assert "from origin/development" in msg and "fetched just now" in msg
        await settle(flow, t.id)

        task = flow.store.get(t.id)
        assert task.state == "review" and task.meta["review_verdict"] == "approve"
        assert task.meta["bounces"] == 1
        log = g(task.meta["worktree"], "log", "--format=%s", f"{task.meta['base']}..HEAD").splitlines()
        assert log == [f"fix(demo): address review of {t.id}", "fix(demo): make the demo work"]
        assert "merges cleanly ✅" in task.note.section("Verification")
        assert task.note.section("Review").count("#### Round") == 2

        await flow.approve(t.id)
        await settle(flow, t.id)
        return flow.store.get(t.id)

    done = asyncio.run(scenario())
    assert done.state == "done" and done.meta["worktree"] == "" and done.meta["closed"] is True
    assert done.meta["branch"] in g(repo, "branch", "--list", done.meta["branch"])
    assert g(repo, "branch", "--show-current") == "development"
    assert calls[-1] is claude.CLOSER_TOOLS
    assert len(flow.oc.disposed) == 1  # opencode let go of the worktree before it was removed


def test_close_survives_a_locked_worktree_and_retry_finishes_it(setup, monkeypatch):
    """On Windows a process inside the worktree makes `git worktree remove` fail with
    "Permission denied". The close must still record the result, warn, and /retry must finish."""
    flow = setup[0]
    real_remove = worktree.remove
    attempts = []

    def flaky_remove(pdir, wt):
        attempts.append(wt)
        if len(attempts) == 1:
            raise worktree.GitError("git worktree remove --force x: error: failed to delete 'x': Permission denied")
        real_remove(pdir, wt)

    monkeypatch.setattr(pipeline.worktree, "remove", flaky_remove)

    async def scenario():
        t = await plan_and_code(flow)
        await flow.approve(t.id)
        await settle(flow, t.id)
        first = flow.store.get(t.id)
        assert first.state == "done" and first.meta["closed"] is False and first.meta["worktree"]
        assert "Couldn't clean up" in flow.n.texts[-1] and "Permission denied" in flow.n.texts[-1]
        assert first.note.section("Result").count("Branch `") == 1

        assert "finishing the close" in await flow.retry(t.id)
        await settle(flow, t.id)
        return flow.store.get(t.id)

    final = asyncio.run(scenario())
    assert final.meta["closed"] is True and final.meta["worktree"] == ""
    assert final.note.section("Result").count("Branch `") == 1  # not written twice
    assert "Couldn't clean up" not in flow.n.texts[-1]
    with pytest.raises(FlowError, match="is done"):
        asyncio.run(flow.retry(final.id))


def test_base_is_fetched_shared_branch_not_local_head(setup):
    flow, repo, colleague, ctl, _ = setup
    commit_file(colleague, "newer.txt", "x\n", "colleague: newer work")
    g(colleague, "push", "-q", "origin", "development")
    upstream = g(colleague, "rev-parse", "HEAD")
    # Your checkout is stale AND sitting on an unrelated feature branch.
    g(repo, "checkout", "-q", "-b", "feature/mine")
    commit_file(repo, "mine.txt", "mine\n", "my unrelated feature")

    task = asyncio.run(plan_and_code(flow))
    assert task.meta["base"] == upstream
    assert task.meta["base_ref"] == "origin/development"
    assert g(task.meta["worktree"], "merge-base", "--is-ancestor", upstream, "HEAD") == ""
    assert not (Path(task.meta["worktree"]) / "mine.txt").exists()


def test_colleague_overlap_is_shown_to_planner_and_reviewer(setup):
    flow, repo, colleague, ctl, _ = setup
    g(colleague, "checkout", "-q", "-b", "feature/survey")
    commit_file(colleague, "shared.txt", "one\nSURVEY\nthree\n", "survey work")
    g(colleague, "push", "-q", "origin", "feature/survey")

    task = asyncio.run(plan_and_code(flow))
    assert "`feature/survey` — azid" in ctl["plan_prompts"][0]
    assert "`shared.txt`" in ctl["plan_prompts"][0]
    ver = task.note.section("Verification")
    assert "Other unmerged branches touching the same files" in ver and "feature/survey" in ver


def test_conflict_blocks_gate2_until_refresh(setup):
    flow, repo, colleague, ctl, _ = setup
    ctl["reviews"] = ["VERDICT: APPROVE", "VERDICT: APPROVE"]

    async def scenario():
        task = await plan_and_code(flow)
        assert task.state == "review"
        # Meanwhile a colleague merges a conflicting change into the shared branch.
        commit_file(colleague, "shared.txt", "one\nCOLLEAGUE\nthree\n", "colleague edits line two")
        g(colleague, "push", "-q", "origin", "development")

        with pytest.raises(FlowError, match="no longer merges cleanly"):
            await flow.approve(task.id)
        msg = await flow.refresh(task.id)
        assert "updating onto origin/development" in msg
        await settle(flow, task.id)

        t = flow.store.get(task.id)
        assert t.state == "review" and t.meta["review_verdict"] == "approve"
        wt = t.meta["worktree"]
        assert (Path(wt) / "shared.txt").read_text() == "one\nCOLLEAGUE and TASK\nthree\n"
        assert g(wt, "log", "-1", "--format=%s").startswith("chore(merge): merge origin/development")
        assert len(g(wt, "log", "-1", "--format=%P").split()) == 2  # a real merge commit
        assert "merges cleanly ✅" in t.note.section("Verification").split("####")[-1]
        with pytest.raises(FlowError, match="already contains"):
            await flow.refresh(task.id)
        await flow.approve(task.id)
        await settle(flow, task.id)
        return flow.store.get(task.id)

    assert asyncio.run(scenario()).state == "done"
    assert any("⚔️" in m for m in flow.n.texts)


def test_refresh_rebases_when_no_conflict(setup):
    flow, repo, colleague, ctl, _ = setup
    ctl["reviews"] = ["VERDICT: APPROVE", "VERDICT: APPROVE"]

    async def scenario():
        task = await plan_and_code(flow)
        commit_file(colleague, "other.txt", "unrelated\n", "colleague unrelated")
        g(colleague, "push", "-q", "origin", "development")
        await flow.refresh(task.id)
        await settle(flow, task.id)
        return flow.store.get(task.id)

    t = asyncio.run(scenario())
    wt = t.meta["worktree"]
    parents = g(wt, "log", "--format=%P", f"{t.meta['base']}..HEAD").splitlines()
    assert all(len(p.split()) == 1 for p in parents)  # linear: rebased, no merge commit
    assert t.meta["base"] == g(colleague, "rev-parse", "HEAD")
    assert any("rebased cleanly" in m for m in flow.n.texts)


def test_pushed_branch_is_merged_not_rebased(setup):
    flow, repo, colleague, ctl, _ = setup
    ctl["reviews"] = ["VERDICT: APPROVE", "VERDICT: APPROVE"]

    async def scenario():
        task = await plan_and_code(flow)
        g(task.meta["worktree"], "push", "-q", "origin", task.meta["branch"])  # you opened a draft PR
        g(repo, "fetch", "-q", "origin")
        commit_file(colleague, "other.txt", "unrelated\n", "colleague unrelated")
        g(colleague, "push", "-q", "origin", "development")
        await flow.refresh(task.id)
        await settle(flow, task.id)
        return flow.store.get(task.id)

    t = asyncio.run(scenario())
    assert len(g(t.meta["worktree"], "log", "-1", "--format=%P").split()) == 2
    assert any("merged origin/development cleanly" in m for m in flow.n.texts)


def test_fetch_failure_is_reported_as_stale(setup):
    flow, repo, *_ = setup
    g(repo, "remote", "set-url", "origin", str(repo.parent / "does-not-exist.git"))
    task = asyncio.run(plan_and_code(flow))
    assert task.meta["base_ref"] == "origin/development"  # last-fetched ref still used
    assert any("base may be stale" in m for m in flow.n.texts)


def test_stop_cleans_up_and_keeps_branch(setup):
    flow, *_ = setup

    async def scenario():
        task = await plan_and_code(flow)
        msg = await flow.stop(task.id)
        assert "cancelled" in msg
        return flow.store.get(task.id), task.meta["worktree"]

    task, wt = asyncio.run(scenario())
    assert task.state == "cancelled"
    assert not os.path.exists(wt)


def test_restart_parks_mid_phase_tasks(setup):
    flow, *_ = setup

    async def scenario():
        t = flow.store.create("demo.app", "x", "r", "x")
        await flow.recover()
        return flow.store.get(t.id)

    t = asyncio.run(scenario())
    assert t.state == "blocked" and t.meta["blocked_from"] == "new"


def test_branch_names_are_namespaced_and_unique():
    taken = {"devflow/fix/261002-01-x", "devflow/fix/261002-01-x-2"}
    assert worktree.branch_name("fix", "261002-01", "X", "devflow", taken.__contains__) == "devflow/fix/261002-01-x-3"
    assert worktree.branch_name("weird", "261002-02", "Sort cols!", "devflow") == "devflow/fix/261002-02-sort-cols"
    assert worktree.branch_name("feat", "261002-03", "y", "") == "feat/261002-03-y"


def test_merge_check_reports_conflicting_files(tmp_path):
    r = tmp_path / "r"
    g(tmp_path, "init", "-q", "-b", "development", str(r))
    for args in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        g(r, *args)
    commit_file(r, "f.txt", "a\nb\nc\n", "init")
    g(r, "checkout", "-q", "-b", "feat")
    commit_file(r, "f.txt", "a\nFEAT\nc\n", "feat")
    assert sync.merge_check(r, "development") == (True, [])
    g(r, "checkout", "-q", "development")
    commit_file(r, "f.txt", "a\nDEV\nc\n", "dev")
    g(r, "checkout", "-q", "feat")
    assert sync.merge_check(r, "development") == (False, ["f.txt"])


def test_failed_phase_is_logged_with_its_task_and_parked(setup, logdir, monkeypatch):
    flow = setup[0]

    async def broken_claude(*a, **k):
        return claude.Result("API error: overloaded", "", False)

    monkeypatch.setattr(pipeline.claude, "run", broken_claude)

    async def scenario():
        t = await flow.new_task("demo", "make the demo work")
        await settle(flow, t.id)
        return t.id

    task_id = asyncio.run(scenario())
    assert flow.store.get(task_id).state == "blocked"
    assert f"/log {task_id}" in flow.n.texts[-1] and "blocked in _plan" in flow.n.texts[-1]

    from devflow import logs
    for h in __import__("logging").getLogger().handlers:
        h.flush()
    mine = "\n".join(logs.tail(logs.path(), 50, task=task_id))
    assert "_plan started" in mine and "_plan failed after" in mine
    assert "RuntimeError: planner failed: API error: overloaded" in mine
    assert f"{task_id}: new -> blocked (bot)" in mine
    errors = "\n".join(logs.tail(logs.path(errors=True), 10))
    assert f"[{task_id}]" in errors and "planner failed" in errors
