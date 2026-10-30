"""Configurable lint/test commands, the php preset, and the devflow-checks schema."""

import asyncio
import json
import sys

import pytest

from devflow import checks, config, verify
from devflow.checks.schema import SCHEMA, BadChecks
from devflow.config import CheckConfig

PY = f'"{sys.executable}"'


def spec(items) -> str:
    return f"## Spec\n\n```devflow-checks\n{json.dumps(items)}\n```\n"


def test_project_commands_run_in_the_worktree(tmp_path):
    (tmp_path / "ok.txt").write_text("x")
    cfg = CheckConfig(lint=[f'{PY} -c "import os; assert os.path.exists(\'ok.txt\')"'],
                      test=[f'{PY} -c "import sys; print(\'2 failed\'); sys.exit(1)"'])
    lint, test = checks.run(cfg, tmp_path, [], None)
    assert lint.ok and lint.text == "passed" and lint.title.startswith("lint `")
    assert not test.ok and "2 failed" in test.text and "exit 1" in test.text


def test_a_command_that_hangs_is_killed_at_its_timeout(tmp_path):
    cfg = CheckConfig(test=[f'{PY} -c "import time; time.sleep(60)"'], timeout=2)
    (result,) = checks.run(cfg, tmp_path, [], None)
    assert not result.ok and "killed after 2s" in result.text


def test_commands_see_only_the_allowed_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:tg-canary")
    monkeypatch.setenv("APP_ENV", "testing")
    probe = f'{PY} -c "import os, sys; sys.exit(0 if \'TELEGRAM_TOKEN\' not in os.environ and os.environ.get(\'APP_ENV\') == \'testing\' else 1)"'
    (result,) = checks.run(CheckConfig(test=[probe]), tmp_path, [], None, env_allow=["APP_ENV"])
    assert result.ok


def test_the_php_preset_reports_like_before(tmp_path):
    """Without PHP files the preset says so, in the same words devflow always used."""
    ev = asyncio.run(verify.run("261030-01", "", tmp_path, ["readme.md"], None, None, None,
                                check_config=CheckConfig(preset="php")))
    assert "**php -l**: no PHP files changed" in ev.markdown and ev.ok


def test_no_checks_configured_is_said_plainly(tmp_path):
    ev = asyncio.run(verify.run("261030-01", "", tmp_path, ["a.ts"], None, None, None, check_config=CheckConfig()))
    assert "none configured for this project" in ev.markdown and ev.ok


def test_a_failing_command_fails_verification(tmp_path):
    cfg = CheckConfig(test=[f'{PY} -c "raise SystemExit(3)"'])
    ev = asyncio.run(verify.run("261030-01", "", tmp_path, [], None, None, None, check_config=cfg))
    assert not ev.ok and ev.markdown.startswith("#### Verification FAIL")


def test_the_planner_is_told_what_will_run():
    text = checks.describe(CheckConfig(preset="php", test=["vendor/bin/pest"]))
    assert "php -l" in text and "`vendor/bin/pest`" in text
    assert "none" in checks.describe(CheckConfig())


@pytest.mark.parametrize("items, message", [
    ("[{\"path\": \"/a\",}]", "not valid JSON"),
    ({"path": "/a"}, "must be array"),
    ([{"method": "GET"}], "needs 'path'"),
    ([{"path": "/a", "expect": 200}], "unknown field 'expect'"),
    ([{"path": "/a", "method": "FETCH"}], "must be one of"),
    ([{"path": "/a", "expect_status": "200"}], "expect_status must be integer or null"),
    ([{"path": "/a", "headers": {"X-A": 1}}], "headers.X-A must be string"),
    ([{"path": "//evil.example/"}], "doesn't match"),
])
def test_malformed_checks_are_refused_with_the_reason(items, message):
    text = f"```devflow-checks\n{items}\n```" if isinstance(items, str) else spec(items)
    with pytest.raises(BadChecks, match=message):
        verify.parse_checks(text)


def test_a_refused_check_block_fails_verification_with_the_reason(tmp_path):
    ev = asyncio.run(verify.run("261030-01", spec([{"path": "/a", "expect": 200}]), tmp_path, [], None,
                                "before.test", "after.test", check_config=CheckConfig()))
    assert not ev.ok and "refused" in ev.markdown and "unknown field" in ev.markdown


def test_valid_checks_still_parse():
    got = verify.parse_checks(spec([{"method": "POST", "path": "/login", "expect_status": 302,
                                     "headers": {"Accept": "text/html"}, "data": {"u": "x"}},
                                    {"path": "/health", "contains": "ok", "expect_status": None}]))
    assert [(c.method, c.path, c.expect_status) for c in got] == [("POST", "/login", 302), ("GET", "/health", None)]


def test_the_schema_matches_what_the_planner_is_told():
    prompt = (config.prompts_dir() / "plan.md").read_text(encoding="utf-8")
    for field in SCHEMA["items"]["properties"]:
        assert f"`{field}`" in prompt


def test_checks_config_from_toml(tmp_path):
    f = tmp_path / "devflow.toml"
    f.write_text('[checks]\nlint = ["ruff check ."]\n\n[projects.app]\npath = "."\n'
                 '[projects.app.checks]\npreset = "php"\ntimeout = 120\n', encoding="utf-8")
    s = config.from_toml(f)
    assert s.checks == CheckConfig(lint=["ruff check ."])
    assert s.for_project("app").checks == CheckConfig(preset="php", timeout=120)
    f.write_text('[checks]\npreset = "node"\n', encoding="utf-8")
    with pytest.raises(config.ConfigError, match="checks.preset"):
        config.from_toml(f)
