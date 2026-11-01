"""The worker's models through an AI gateway (ADR 0010): config, opencode provider, health check."""

import asyncio
import dataclasses
import json

import httpx
import pytest

from devflow import config, gateway, opencode

GATEWAY = """
[gateway]
base_url = "http://127.0.0.1:20128/v1/"
api_key_env = "DEVFLOW_GATEWAY_KEY"
name = "OmniRoute"
models = ["devflow-worker", "qwen/qwen3-coder:free"]

[projects.shop]
path = "shop"
opencode_model = "gateway/qwen/qwen3-coder:free"

[projects.blog]
path = "blog"
"""


def load(tmp_path, text):
    path = tmp_path / "devflow.toml"
    path.write_text(text, encoding="utf-8")
    return config.from_toml(path)


def test_the_first_gateway_model_is_the_worker_default_and_projects_can_override_it(tmp_path):
    s = load(tmp_path, GATEWAY)
    assert s.gateway == config.GatewayConfig(base_url="http://127.0.0.1:20128/v1", name="OmniRoute",
                                             models=["devflow-worker", "qwen/qwen3-coder:free"],
                                             api_key_env="DEVFLOW_GATEWAY_KEY")
    assert s.opencode_model == "gateway/devflow-worker"
    assert s.for_project("shop").opencode_model == "gateway/qwen/qwen3-coder:free"
    assert s.for_project("blog").opencode_model == "gateway/devflow-worker"


def test_an_explicit_worker_model_beats_the_gateway_default(tmp_path):
    s = load(tmp_path, '[agents]\nopencode_model = "openrouter/x"\n' + GATEWAY)
    assert s.opencode_model == "openrouter/x"


def test_without_a_gateway_nothing_changes(tmp_path):
    s = load(tmp_path, '[projects.shop]\npath = "shop"\n')
    assert s.gateway is None and s.opencode_model == ""
    assert "provider" not in json.loads(opencode.config_file(s).read_text(encoding="utf-8"))


@pytest.mark.parametrize("bad, message", [
    ('base_url = "127.0.0.1:20128"\nmodels = ["m"]', "http"),
    ('base_url = "http://x/v1"\nmodels = []', "at least one model"),
    ('base_url = "http://x/v1"\nmodels = ["m"]\napi_key_env = "sk-or-v1-abc"', "NAME of an environment"),
    ('base_url = "http://x/v1"\nmodels = ["m"]\nprovider_id = "My Gateway"', "provider_id"),
    ('base_url = "http://x/v1"\nmodels = ["m"]\nkey = "x"', "unknown setting"),
])
def test_a_bad_gateway_table_is_an_error_that_names_the_setting(tmp_path, bad, message):
    with pytest.raises(config.ConfigError, match=message):
        load(tmp_path, f"[gateway]\n{bad}\n")


def test_the_runtime_opencode_config_registers_the_gateway_with_a_key_reference_only(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVFLOW_GATEWAY_KEY", "sk-secret-value")
    s = load(tmp_path, GATEWAY)
    text = opencode.config_file(s).read_text(encoding="utf-8")
    assert "sk-secret-value" not in text
    provider = json.loads(text)["provider"]["gateway"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {"baseURL": "http://127.0.0.1:20128/v1", "apiKey": "{env:DEVFLOW_GATEWAY_KEY}"}
    assert list(provider["models"]) == ["devflow-worker", "qwen/qwen3-coder:free"]


def test_the_worker_prompt_names_provider_and_model_split_at_the_first_slash(tmp_path):
    s = load(tmp_path, GATEWAY)
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"info": {}, "parts": [{"type": "text", "text": "done"}]})

    async def run():
        client = opencode.Client(s)
        client.http = httpx.AsyncClient(base_url="http://oc", transport=httpx.MockTransport(handler))
        client.api = 1  # a v1 server (tests/test_opencode_v2.py covers v2)
        client._watch_permissions = lambda *a: _forever()
        return await client.prompt("ses", tmp_path, "go", 30, model_name=s.for_project("shop").opencode_model)

    async def _forever():
        await asyncio.Event().wait()

    assert asyncio.run(run()) == "done"
    assert sent["model"] == {"providerID": "gateway", "modelID": "qwen/qwen3-coder:free"}


GW = config.GatewayConfig(base_url="http://gw/v1", models=["a", "b"], api_key_env="DEVFLOW_GATEWAY_KEY")


@pytest.mark.parametrize("response, up, missing, detail", [
    (httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}), True, [], "3 models served"),
    (httpx.Response(200, json={"data": [{"id": "a"}]}), True, ["b"], "not served: b"),
    (httpx.Response(401, json={}), False, [], "set DEVFLOW_GATEWAY_KEY"),
    (httpx.Response(200, text="<html>"), False, [], "OpenAI-style"),
])
def test_the_health_check_reads_the_model_list(monkeypatch, response, up, missing, detail):
    monkeypatch.delenv("DEVFLOW_GATEWAY_KEY", raising=False)
    st = gateway._parse(GW, response)
    assert (st.up, st.missing) == (up, missing) and detail in st.detail


def test_the_health_check_sends_the_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("DEVFLOW_GATEWAY_KEY", "k1")
    seen = {}

    def fake_get(url, headers, timeout):
        seen.update(url=url, headers=headers)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(gateway.httpx, "get", fake_get)
    st = gateway.check(GW)
    assert not st.up and "no answer" in st.detail
    assert seen == {"url": "http://gw/v1/models", "headers": {"Authorization": "Bearer k1"}}


# -- free_only (ADR 0012) -----------------------------------------------------------------------

FREE = """
[gateway]
base_url = "http://gw/v1"
models = ["devflow-worker", "openrouter/cohere/north-mini-code:free"]
free_only = true
free_combos = ["devflow-worker"]

[projects.shop]
path = "shop"
"""


def test_free_only_accepts_free_ids_and_listed_combos(tmp_path):
    s = load(tmp_path, FREE)
    assert s.gateway.free_only and s.gateway.free_combos == ["devflow-worker"]
    assert s.gateway.allowed("gateway/devflow-worker")
    assert s.gateway.allowed("gateway/openrouter/cohere/north-mini-code:free")
    assert not s.gateway.allowed("gateway/openai/gpt-5")
    assert not s.gateway.allowed("openrouter/x:free")  # another provider is outside the gateway


@pytest.mark.parametrize("change, message", [
    ('models = ["devflow-worker", "openai/gpt-5"]', "not a free model.*openai/gpt-5"),
    ('free_combos = []', "not a free model.*devflow-worker"),
])
def test_free_only_refuses_a_paid_gateway_model_at_load(tmp_path, change, message):
    key = change.split(" = ")[0]
    text = "\n".join(change if line.startswith(key + " =") else line for line in FREE.splitlines())
    with pytest.raises(config.ConfigError, match=message):
        load(tmp_path, text)


@pytest.mark.parametrize("override", ['[agents]\nopencode_model = "openrouter/openai/gpt-5"\n',
                                      'X'])
def test_free_only_refuses_a_paid_worker_model_anywhere(tmp_path, override):
    text = (FREE.replace('path = "shop"', 'path = "shop"\nopencode_model = "gateway/openai/gpt-5"')
            if override == "X" else override + FREE)
    with pytest.raises(config.ConfigError, match="not allowed with gateway.free_only"):
        load(tmp_path, text)


def test_free_combos_without_free_only_is_a_mistake(tmp_path):
    with pytest.raises(config.ConfigError, match="free_combos only means"):
        load(tmp_path, FREE.replace("free_only = true\n", ""))


FGW = config.GatewayConfig(base_url="http://gw/v1", models=["devflow-worker"], free_only=True,
                           free_combos=["devflow-worker"])


def combos(*models, kind="model"):
    return httpx.Response(200, json={"object": "list", "data": [
        {"name": "other", "models": [{"kind": "model", "model": "openai/gpt-5", "providerId": "openai"}]},
        {"name": "devflow-worker", "models": [{"kind": kind, "model": m, "providerId": "openrouter"}
                                              for m in models]}]})


@pytest.mark.parametrize("response, free, detail", [
    (combos("a:free", "b:free"), True, "2 free models"),
    (combos("a:free", "openai/gpt-5"), False, "not free: openrouter/openai/gpt-5"),
    (combos("a:free", kind="combo"), False, "not free"),  # a nested combo can't be checked
    (combos(), False, "lists no models"),
    (httpx.Response(200, json={"data": []}), False, "no combo named devflow-worker"),
    (httpx.Response(404, json={}), False, "HTTP 404"),
])
def test_a_combo_is_free_only_if_every_entry_is_a_free_model(response, free, detail):
    c = gateway._combo(FGW, "devflow-worker", response)
    assert c.free is free and detail in c.detail


def test_the_worker_check_fails_closed(monkeypatch):
    async def unreachable(gw, name, timeout=10):
        return gateway.ComboCheck(name, False, "no answer from http://gw/v1: ConnectError")

    monkeypatch.setattr(gateway, "acheck_combo", unreachable)
    assert "no answer" in asyncio.run(gateway.ensure_free(FGW, "gateway/devflow-worker"))
    assert asyncio.run(gateway.ensure_free(FGW, "gateway/x:free")) is None  # no lookup needed
    assert "not a free model" in asyncio.run(gateway.ensure_free(FGW, "gateway/openai/gpt-5"))
    off = dataclasses.replace(FGW, free_only=False, free_combos=[])
    assert asyncio.run(gateway.ensure_free(off, "gateway/openai/gpt-5")) is None


def test_the_worker_is_never_prompted_with_a_paid_combo(tmp_path, monkeypatch):
    s = load(tmp_path, FREE)
    sent = []

    async def paid(gw, name, timeout=10):
        return gateway.ComboCheck(name, False, "combo devflow-worker routes to models that are not free: x")

    monkeypatch.setattr(gateway, "acheck_combo", paid)

    async def run():
        client = opencode.Client(s)
        client.http = httpx.AsyncClient(base_url="http://oc", transport=httpx.MockTransport(
            lambda r: sent.append(r) or httpx.Response(200, json={"parts": []})))
        await client.prompt("ses", tmp_path, "go", 30)

    with pytest.raises(opencode.OpencodeError, match="free_only: combo devflow-worker routes"):
        asyncio.run(run())
    assert sent == []  # nothing reached opencode, so nothing reached a model



# -- which models answer ----------------------------------------------------------------------

@pytest.mark.parametrize("response, state", [
    (httpx.Response(200, json={"model": "a:free", "choices": []}), "online"),
    (httpx.Response(429, json={"error": {"message": "slow down"}}), "limited"),
    (httpx.Response(503, json={"error": {"message": "All credentials for model x are cooling down"}}), "limited"),
    (httpx.Response(403, json={"error": {"message": "only available to paid accounts"}}), "refused"),
    (httpx.Response(500, text="boom"), "offline"),
])
def test_a_probe_reply_is_classified(response, state):
    p = gateway._classify("openrouter/a:free", response, 1.5, "c")
    assert p.state == state and p.combo == "c"


def test_probe_all_expands_combos_and_skips_paid_models_under_free_only(monkeypatch):
    gw = config.GatewayConfig(base_url="http://gw/v1", models=["devflow-worker", "x/solo:free"], free_only=True,
                              free_combos=["devflow-worker"])
    sent = []

    async def acombos(g, timeout=10):
        return {"devflow-worker": ["openrouter/a:free", "openai/gpt-5", "x/solo:free"]}

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers, json):
            sent.append(json["model"])
            assert json["max_tokens"] == gateway.PROBE_TOKENS
            return httpx.Response(200, json={"model": json["model"]})

    monkeypatch.setattr(gateway, "acombos", acombos)
    monkeypatch.setattr(gateway.httpx, "AsyncClient", FakeClient)
    probes = asyncio.run(gateway.probe_all(gw))
    assert [(p.model, p.state, p.combo) for p in probes] == [
        ("openrouter/a:free", "online", "devflow-worker"),
        ("openai/gpt-5", "skipped", "devflow-worker"),
        ("x/solo:free", "online", "devflow-worker"),  # listed twice, checked once
    ]
    assert sorted(sent) == ["openrouter/a:free", "x/solo:free"]  # the paid model was never called
