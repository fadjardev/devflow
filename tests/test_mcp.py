import asyncio
import importlib

import pytest


@pytest.fixture
def server(vault, monkeypatch):
    monkeypatch.setenv("DEVFLOW_VAULT", str(vault))
    from devflow import mcp_server
    return importlib.reload(mcp_server)


def call(server, name, **args):
    result = asyncio.run(server.mcp.call_tool(name, args))
    return result.content[0].text


def test_lists_five_tools(server):
    tools = asyncio.run(server.mcp.list_tools())
    assert sorted(t.name for t in tools) == [
        "decision_add", "project_note_update", "task_append_log", "task_read", "task_update_state",
    ]
    params = next(t for t in tools if t.name == "task_update_state").input_schema["properties"]
    assert set(params) == {"task_id", "to", "note"}


def test_agent_flow_and_refusals(server):
    t = server.store.create("demo.app", "x", "r", "y")
    assert call(server, "task_update_state", task_id=t.id, to="planned").startswith("OK")
    out = call(server, "task_update_state", task_id=t.id, to="coding")
    assert out.startswith("REFUSED") and "only the bot" in out
    assert call(server, "task_append_log", task_id=t.id, section="Spec", text="do it").startswith("OK")
    assert call(server, "task_append_log", task_id=t.id, section="History", text="x").startswith("REFUSED")
    assert "do it" in call(server, "task_read", task_id=t.id)
    assert call(server, "task_read", task_id="../../Home").startswith("REFUSED")


def test_project_tools_refuse_paths(server):
    assert call(server, "decision_add", project="../Home", title="t", body="b").startswith("REFUSED")
    assert call(server, "project_note_update", project="nope", section="X", text="t").startswith("REFUSED")
    assert call(server, "decision_add", project="demo.app", title="t", body="b").startswith("OK")
