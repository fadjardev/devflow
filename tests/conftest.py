import pytest

from devflow.tasks import TaskStore

PROJECT_NOTE = """---
title: demo.app
type: project
status: active
path: "C:\\\\x\\\\demo.app"
tags: [project]
---

# demo.app

## Overview

Demo.

## Decisions

## Links
"""


@pytest.fixture(autouse=True)
def _runtime(tmp_path, monkeypatch):
    """Every test writes its runtime files (logs, MCP configs, locks) under its own tmp_path,
    and config.load() sees a legacy setup in tmp_path, never the developer's own config."""
    from devflow import config
    monkeypatch.setenv("DEVFLOW_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.delenv("DEVFLOW_CONFIG", raising=False)
    monkeypatch.setattr(config, "default_config", lambda: tmp_path / "no-such-devflow.toml")
    for key, sub in (("VAULT_PATH", "vault"), ("PROJECTS_PATH", "projects")):
        monkeypatch.setenv(key, str(tmp_path / sub))


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "01-Projects").mkdir()
    (tmp_path / "01-Projects" / "demo.app.md").write_text(PROJECT_NOTE, encoding="utf-8")
    (tmp_path / "00-Meta").mkdir()
    (tmp_path / "Home.md").write_text("# Home\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def store(vault):
    return TaskStore(vault)


@pytest.fixture
def task(store):
    return store.create("demo.app", "Fix login redirect", "Request demo.app 2026-10-02", "login loops\nsecond line")


@pytest.fixture
def logdir(tmp_path, monkeypatch):
    """devflow logging into tmp_path/runtime; the root logger is restored afterwards."""
    import logging
    import sys
    import threading

    from devflow import logs

    root = logging.getLogger()
    saved = (list(root.handlers), root.level, sys.excepthook, threading.excepthook)
    monkeypatch.setenv("DEVFLOW_RUNTIME", str(tmp_path / "runtime"))
    logs.setup("devflow", console=False)
    yield tmp_path / "runtime"
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in saved[0]:
        root.addHandler(h)
    root.setLevel(saved[1])
    sys.excepthook, threading.excepthook = saved[2], saved[3]
