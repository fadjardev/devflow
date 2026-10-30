"""devflow without a vault: projects from the registry, notes in devflow's own folder."""

import asyncio
import dataclasses

from test_pipeline import Notes, g, settle, setup  # noqa: F401  (setup is a fixture)

from devflow import claude, config
from devflow.pipeline import Flow


def test_a_task_runs_end_to_end_without_a_vault(setup, tmp_path):
    flow, repo = setup[0], setup[1]
    own = tmp_path / "runtime" / "vault"
    s = dataclasses.replace(flow.s, vault=own, vault_is_own=True,
                            registry={"demo.app": config.Project("demo.app", repo)})
    f = Flow(s, Notes())
    f.oc = flow.oc

    async def scenario():
        t = await f.new_task("demo", "make the demo work")
        await settle(f, t.id)
        await f.approve(t.id)
        await settle(f, t.id)
        await f.approve(t.id)
        await settle(f, t.id)
        return f.store.get(t.id)

    done = asyncio.run(scenario())
    assert done.state == "done"
    assert done.path.is_relative_to(own) and (own / "01-Projects" / "demo.app.md").is_file()
    assert not (tmp_path / "vault" / "05-Tasks").exists()  # the old vault in the fixture was never used
    assert done.meta["branch"] in g(repo, "branch", "--list", done.meta["branch"])


def test_registry_projects_need_no_vault_note(setup, tmp_path):
    flow, repo = setup[0], setup[1]
    s = dataclasses.replace(flow.s, registry={"demo.app": config.Project("demo.app", repo)})
    (s.vault / "01-Projects" / "demo.app.md").unlink()
    f = Flow(s, Notes())
    f.oc = flow.oc
    async def scenario():
        t = await f.new_task("demo", "make the demo work")
        await settle(f, t.id)
        return f.store.get(t.id)

    assert asyncio.run(scenario()).state == "planned"


def test_project_overrides_reach_the_worker_and_claude(setup, tmp_path, monkeypatch):
    flow, repo = setup[0], setup[1]
    proj = config.Project("demo.app", repo, worker_shell_allow=["npm test"], deny_read=["secrets.json"])
    s = dataclasses.replace(flow.s, registry={"demo.app": proj})
    f = Flow(s, Notes())
    sessions, runs = [], []

    class Recording(type(flow.oc)):
        async def create_session(self, directory, title, rules=None):
            sessions.append(rules)
            return "ses"

    f.oc = Recording()
    from devflow import pipeline
    real = pipeline.claude.run

    async def spy(settings, prompt, cwd, tools, resume="", add_dirs=(), timeout=1800, bind=None):
        runs.append(settings)
        return await real(settings, prompt, cwd, tools, resume, add_dirs, timeout, bind)

    monkeypatch.setattr(pipeline.claude, "run", spy)

    async def scenario():
        t = await f.new_task("demo", "make the demo work")
        await settle(f, t.id)
        await f.approve(t.id)
        await settle(f, t.id)

    asyncio.run(scenario())
    assert {"permission": "bash", "pattern": "npm test", "action": "allow"} in sessions[0]
    assert {"permission": "read", "pattern": "*secrets.json", "action": "deny"} in sessions[0]
    assert all(r.deny_read == ["secrets.json"] for r in runs)
    assert claude.secret_rules(runs[0]) == ["Read(**/secrets.json)"]
