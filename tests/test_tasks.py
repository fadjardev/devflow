import pytest

from devflow import tasks
from devflow import vault as vaultmod
from devflow.tasks import TransitionError
from devflow.vault import VaultError


def test_create_writes_note_with_sections(task, vault):
    assert task.path == vault / "05-Tasks" / "demo.app" / f"{task.id}.md"
    note = vaultmod.read(task.path)
    assert note.meta["state"] == "new"
    assert note.meta["project"] == "[[demo.app]]"
    assert "> login loops" in note.section("Request")
    for s in tasks.SECTIONS:
        assert note.section(s) is not None


def test_ids_increment(store, task):
    second = store.create("demo.app", "Another", "r", "x")
    assert int(second.id[-2:]) == int(task.id[-2:]) + 1


def test_frontmatter_round_trip(store, task):
    store.update(task.id, branch="fix/x", bounces=1)
    again = store.get(task.id)
    assert again.meta["branch"] == "fix/x"
    assert again.meta["bounces"] == 1
    assert again.meta["request"] == "[[Request demo.app 2026-10-02]]"
    assert again.note.section("Request") == task.note.section("Request")


def test_happy_path(store, task):
    store.transition(task.id, "planned", "agent")
    store.transition(task.id, "coding", "bot", "approved")
    store.transition(task.id, "review", "agent")
    store.transition(task.id, "coding", "bot", "fix-up")
    store.transition(task.id, "review", "bot")
    done = store.transition(task.id, "done", "bot")
    assert done.state == "done"
    history = done.note.section("History")
    assert "planned -> coding (bot): approved" in history
    assert history.count("\n- ") == 6
    assert "\n\n" not in history  # a tight list


@pytest.mark.parametrize("frm,to", [("planned", "coding"), ("review", "done"), ("review", "coding")])
def test_agents_cannot_pass_gates(store, task, frm, to):
    store.update(task.id, state=frm)
    with pytest.raises(TransitionError, match="only the bot"):
        store.transition(task.id, to, "agent")


def test_illegal_transition(store, task):
    with pytest.raises(TransitionError, match="nobody"):
        store.transition(task.id, "done", "bot")
    with pytest.raises(TransitionError):
        store.transition(task.id, "nonsense", "bot")


def test_blocked_remembers_and_retries(store, task):
    store.transition(task.id, "planned", "agent")
    store.transition(task.id, "coding", "bot")
    b = store.transition(task.id, "blocked", "agent", "tests explode")
    assert b.meta["blocked_from"] == "coding"
    assert store.transition(task.id, "coding", "bot").state == "coding"


def test_cancel_is_bot_only(store, task):
    with pytest.raises(TransitionError):
        store.transition(task.id, "cancelled", "agent")
    assert store.transition(task.id, "cancelled", "bot").state == "cancelled"


def test_agent_sections(store, task):
    store.append(task.id, "Worker log", "did a thing", actor="agent")
    store.append(task.id, "worker log", "and another", actor="agent")
    log = store.get(task.id).note.section("Worker log")
    assert log == "did a thing\n\nand another"
    with pytest.raises(VaultError, match="may not write"):
        store.append(task.id, "History", "forged", actor="agent")
    with pytest.raises(VaultError):
        store.append(task.id, "Request", "forged", actor="agent")


def test_no_agent_writes_after_done(store, task):
    store.update(task.id, state="done")
    with pytest.raises(VaultError, match="no longer"):
        store.append(task.id, "Result", "late", actor="agent")


@pytest.mark.parametrize("bad", ["../x", "261002", "261002-1", "*", "261002-01/../../Home", ""])
def test_bad_ids(store, bad):
    with pytest.raises(VaultError):
        store.get(bad)


@pytest.mark.parametrize("bad", ["nope", "../01-Projects/demo.app", "demo.app/../x", "", "a b"])
def test_unknown_projects_refused(vault, bad):
    with pytest.raises(VaultError):
        tasks.add_decision(vault, bad, "t", "b")
    with pytest.raises(VaultError):
        tasks.create_request(vault, bad, "x")


def test_safe_path(vault):
    for bad in ["../outside.md", "Home.md", "00-Meta/Conventions.md", "05-Tasks/x.txt", "05-Tasks/../../x.md"]:
        with pytest.raises(VaultError):
            vaultmod.safe_path(vault, bad)
    assert vaultmod.safe_path(vault, "05-Tasks/demo.app/x.md").name == "x.md"


def test_decision_goes_under_decisions(vault):
    tasks.add_decision(vault, "demo.app", "Use worktrees", "**Context:** x")
    note = vaultmod.read(vault / "01-Projects" / "demo.app.md")
    assert "— Use worktrees" in note.section("Decisions")
    assert note.section("Links") == ""
    assert note.meta["status"] == "active"


def test_project_section_update(vault):
    tasks.update_project_section(vault, "demo.app", "Overview", "Replaced.", mode="replace")
    tasks.update_project_section(vault, "demo.app", "Gotchas", "New section.")
    note = vaultmod.read(vault / "01-Projects" / "demo.app.md")
    assert note.section("Overview") == "Replaced."
    assert note.section("Gotchas") == "New section."
    with pytest.raises(VaultError):
        tasks.update_project_section(vault, "demo.app", "Decisions", "x")
    with pytest.raises(VaultError):
        tasks.update_project_section(vault, "demo.app", "## Evil\n---", "x")


def test_request_names_do_not_collide(vault):
    import datetime as dt
    day = dt.date(2026, 10, 2)
    a = tasks.create_request(vault, "demo.app", "one", today=day)
    b = tasks.create_request(vault, "demo.app", "two", today=day)
    assert a == "Request demo.app 2026-10-02"
    assert b == "Request demo.app 2026-10-02 (2)"
    text = (vault / "04-Requests" / "demo.app" / f"{a}.md").read_text(encoding="utf-8")
    assert text.startswith("one")


OBSIDIAN_FM = r'''---
title: x
path: "E:\dev\projects\x"
tags: [project, "status/active"]
---
'''


def test_obsidian_frontmatter_survives_byte_for_byte(vault):
    p = vault / "01-Projects" / "x.md"
    p.write_text(OBSIDIAN_FM + "\n# x\n\n## Decisions\n", encoding="utf-8")
    note = vaultmod.read(p)
    assert note.meta["path"] == r"E:\dev\projects\x"
    tasks.add_decision(vault, "x", "Keep it", "body")
    text = p.read_text(encoding="utf-8")
    assert text.startswith(OBSIDIAN_FM)
    assert "— Keep it" in text
