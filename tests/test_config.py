"""devflow.toml, the project registry, and the legacy .env that must keep working."""

import dataclasses
import subprocess
from pathlib import Path

import pytest

from devflow import cli, config, projects, sync

LEGACY_KEYS = ["TELEGRAM_TOKEN", "ALLOWED_CHAT_ID", "ALLOWED_USER_IDS", "VAULT_PATH", "PROJECTS_PATH",
               "DEVCRATE_ROOT", "VHOST_PROVIDER", "MAX_WORKERS", "MAX_BOUNCES", "OPENCODE_URL", "OPENCODE_MODEL",
               "CLAUDE_MODEL", "BASE_BRANCH", "GIT_REMOTE", "BRANCH_PREFIX", "OVERLAP_DAYS", "LINK_PATHS",
               "DENY_READ", "WORKER_SHELL_ALLOW", "UNSAFE_SHELL", "BEFORE_UNSAFE_METHODS",
               "SHARE_CODE_VIA_TELEGRAM", "CHECK_ENV_ALLOW"]


@pytest.fixture
def clean_env(monkeypatch):
    for key in LEGACY_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_a_legacy_env_file_gives_the_same_settings_as_before(tmp_path, clean_env):
    """The .env from before v0.2 (as in .env.example) loads into exactly what it did then."""
    env = tmp_path / ".env"
    env.write_text(
        "TELEGRAM_TOKEN=123:abc\nALLOWED_CHAT_ID=4242\nVAULT_PATH=C:\\work\\vault\nPROJECTS_PATH=C:\\work\\projects\n"
        "DEVCRATE_ROOT=C:\\devcrate\nMAX_WORKERS=3\nMAX_BOUNCES=2\nOPENCODE_URL=http://127.0.0.1:4096/\n"
        "OPENCODE_MODEL=\nCLAUDE_MODEL=sonnet\nBASE_BRANCH=development\nGIT_REMOTE=origin\nBRANCH_PREFIX=devflow\n"
        "OVERLAP_DAYS=14\nLINK_PATHS=vendor,.env,application/config/database.php,application/config/config.php\n"
        "DENY_READ=.env,.env.*,application/config/database.php,**/*-key.pem\n", encoding="utf-8")
    s = config.from_env(env)
    before = {
        "vault": Path(r"C:\work\vault"), "projects": Path(r"C:\work\projects"),
        "devcrate_root": Path(r"C:\devcrate"), "telegram_token": "123:abc", "allowed_chat_id": 4242,
        "max_workers": 3, "max_bounces": 2, "opencode_url": "http://127.0.0.1:4096", "opencode_model": "",
        "claude_model": "sonnet", "base_branch": "development", "git_remote": "origin",
        "branch_prefix": "devflow", "overlap_days": 14,
        "link_paths": ["vendor", ".env", "application/config/database.php", "application/config/config.php"],
        "deny_read": [".env", ".env.*", "application/config/database.php", "**/*-key.pem"],
    }
    assert {k: getattr(s, k) for k in before} == before
    assert s.vhost_provider == "devcrate" and s.registry is None and not s.vault_is_own
    assert s.checks == config.CheckConfig(preset="php")  # php -l and phpunit, as always
    assert s.worktrees == Path(r"C:\work\projects") / ".worktrees"


def test_legacy_mode_names_what_is_missing_instead_of_guessing_a_path(tmp_path, clean_env):
    with pytest.raises(config.ConfigError, match="VAULT_PATH and PROJECTS_PATH"):
        config.from_env(tmp_path / "no.env")
    assert "E:\\dev" not in Path(config.__file__).read_text(encoding="utf-8")


FULL = """
[telegram]
token = "123:abc"
chat_id = -1001
user_ids = [42, 43]

[paths]
vault = "vault"
projects = "code"

[agents]
max_workers = 2
claude_model = "sonnet"

[git]
remote = "upstream"
branch_prefix = "bot"

[safety]
deny_read = [".env"]
worker_shell_allow = ["npm run lint"]
share_code_via_telegram = false

[vhost]
provider = "none"

[projects.shop]
path = "code/shop"
base_branch = "develop"
link_paths = ["node_modules"]
worker_shell_allow = ["npm run lint", "npx tsc --noEmit"]

[projects."docs.site"]
path = "/srv/docs"
"""


def test_toml_reads_globals_and_a_project_registry(tmp_path):
    f = tmp_path / "devflow.toml"
    f.write_text(FULL, encoding="utf-8")
    s = config.from_toml(f)
    assert s.telegram_token == "123:abc" and s.allowed_chat_id == -1001 and s.allowed_user_ids == [42, 43]
    assert s.vault == (tmp_path / "vault").resolve() and not s.vault_is_own
    assert s.projects == (tmp_path / "code").resolve() and s.max_workers == 2 and s.git_remote == "upstream"
    assert s.base_branch == "" and s.share_code_via_telegram is False and s.vhost_provider == "none"
    assert sorted(s.registry) == ["docs.site", "shop"]
    assert projects.names(s) == ["docs.site", "shop"]

    shop = s.for_project("shop")
    assert shop.base_branch == "develop" and shop.link_paths == ["node_modules"]
    assert shop.worker_shell_allow == ["npm run lint", "npx tsc --noEmit"]
    assert shop.deny_read == [".env"]  # not overridden: the global value
    assert s.for_project("docs.site").worker_shell_allow == ["npm run lint"]


def test_toml_defaults_are_generic(tmp_path):
    f = tmp_path / "devflow.toml"
    f.write_text("[projects.app]\npath = \"app\"\n", encoding="utf-8")
    s = config.from_toml(f)
    assert s.vault_is_own and s.base_branch == "" and s.vhost_provider == "none"
    assert s.link_paths == [] and s.worker_shell_allow == [] and ".env" in s.deny_read
    assert not any("application/config" in p for p in s.deny_read + s.link_paths)


@pytest.mark.parametrize("text, message", [
    ("[agents]\nmax_worker = 3\n", "unknown setting in agents: max_worker"),
    ("[agents]\nmax_workers = \"3\"\n", "agents.max_workers must be a whole number"),
    ("[projects.app]\nbase_branch = \"main\"\n", "projects.app.path is required"),
    ("[vhost]\nprovider = \"devcrate\"\n", "devcrate_root is required"),
    ("[vhost]\nprovider = \"apache\"\n", "none or devcrate"),
    ("telegram = 1\n", "telegram must be a table"),
    ("[agents\n", "devflow.toml"),
])
def test_toml_errors_name_the_setting(tmp_path, text, message):
    f = tmp_path / "devflow.toml"
    f.write_text(text, encoding="utf-8")
    with pytest.raises(config.ConfigError, match=message.replace(".", r"\.")):
        config.from_toml(f)


def test_load_prefers_the_toml_and_falls_back_to_legacy(tmp_path, monkeypatch):
    s = config.load()
    assert s.registry is None and "legacy" in s.source
    f = tmp_path / "devflow.toml"
    f.write_text("[projects.app]\npath = \".\"\n", encoding="utf-8")
    monkeypatch.setattr(config, "default_config", lambda: f)
    assert config.load().registry is not None
    with pytest.raises(config.ConfigError, match="not found"):
        config.load(tmp_path / "missing.toml")


def g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_migrate_env_keeps_the_projects_and_settings(tmp_path, clean_env, monkeypatch):
    vault, code = tmp_path / "vault", tmp_path / "code"
    (vault / "01-Projects").mkdir(parents=True)
    (code / "shop").mkdir(parents=True)
    (vault / "01-Projects" / "shop.md").write_text("---\ntitle: shop\n---\n# shop\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_TOKEN=123:abc\nALLOWED_CHAT_ID=7\nVAULT_PATH={vault}\nPROJECTS_PATH={code}\n"
                   f"WORKER_SHELL_ALLOW=php -l *,composer dump-autoload\n", encoding="utf-8")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    target = tmp_path / "conf" / "devflow.toml"
    cli.migrate_env(target)

    old, new = config.from_env(env), config.from_toml(target)
    same = ["telegram_token", "allowed_chat_id", "vault", "projects", "base_branch", "link_paths", "deny_read",
            "worker_shell_allow", "max_workers", "branch_prefix", "vhost_provider"]
    assert {k: getattr(new, k) for k in same} == {k: getattr(old, k) for k in same}
    assert new.registry is not None and new.registry["shop"].path == (code / "shop").resolve()
    with pytest.raises(SystemExit, match="already exists"):
        cli.migrate_env(target)


def test_init_writes_a_config_that_loads(tmp_path):
    repo = tmp_path / "shop"
    repo.mkdir()
    g(repo, "init", "-q")
    answers = iter(["", "4242", "", "", "shop", str(repo), "", ""])
    target = tmp_path / "devflow.toml"
    cli.init(target, ask=lambda prompt: next(answers))
    s = config.from_toml(target)
    assert s.allowed_chat_id == 4242 and s.vault_is_own and s.registry is not None
    assert s.registry["shop"].path == repo.resolve()


def test_an_empty_base_branch_means_the_remotes_default(tmp_path):
    origin = tmp_path / "origin.git"
    g(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(origin))
    seed = tmp_path / "seed"
    g(tmp_path, "clone", "-q", str(origin), str(seed))
    g(seed, "-c", "user.email=a@a", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "init")
    g(seed, "push", "-q", "origin", "HEAD:trunk")
    work = tmp_path / "work"
    g(tmp_path, "clone", "-q", str(origin), str(work))
    s = dataclasses.replace(config.load(), base_branch="", git_remote="origin")
    assert sync.base_ref(s, work) == "origin/trunk"


def test_the_example_config_loads():
    s = config.from_toml(config.REPO_ROOT / "devflow.example.toml")
    assert s.registry is not None and "shop" in s.registry and s.vhost_provider == "none"


def test_doctor_checks_the_config_file_it_read(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "app"
    repo.mkdir()
    g(repo, "init", "-q")
    f = tmp_path / "devflow.toml"
    f.write_text(f'[telegram]\nchat_id = 1\n[projects.app]\npath = "{repo.as_posix()}"\n', encoding="utf-8")
    monkeypatch.setenv("DEVFLOW_CONFIG", str(f))
    with pytest.raises(SystemExit):
        cli.main(["doctor"])
    out = capsys.readouterr().out
    assert f"settings: {f}" in out and "config file private" in out and ".env private" not in out
    assert "ok   app:" in out
