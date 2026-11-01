"""The AI gateway the worker's models go through (ADR 0010): is it up, which models does it serve.

devflow never sends a prompt here itself; opencode does. This only asks the gateway's
OpenAI-compatible `GET <base_url>/models`, for `devflow doctor` and the dashboard, and, with
`free_only`, `GET <base_url>/combos` (OmniRoute) to check that a combo holds free models only.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

import httpx

from .config import FREE_SUFFIX, GatewayConfig


@dataclass(frozen=True)
class Status:
    up: bool
    detail: str
    served: list[str] = field(default_factory=list)  # model ids the gateway lists
    missing: list[str] = field(default_factory=list)  # configured models it does not list


def _headers(gw: GatewayConfig) -> dict[str, str]:
    key = os.environ.get(gw.api_key_env, "") if gw.api_key_env else ""
    return {"Authorization": f"Bearer {key}"} if key else {}


def _parse(gw: GatewayConfig, r: httpx.Response) -> Status:
    if r.status_code in (401, 403):
        hint = f"set {gw.api_key_env}" if gw.api_key_env and not os.environ.get(gw.api_key_env) else "check the key"
        return Status(False, f"HTTP {r.status_code} from {gw.base_url}/models: {hint}")
    if r.status_code != 200:
        return Status(False, f"HTTP {r.status_code} from {gw.base_url}/models")
    try:
        data = r.json().get("data") or []
        served = sorted({str(m["id"]) for m in data if isinstance(m, dict) and m.get("id")})
    except (ValueError, AttributeError, TypeError):
        return Status(False, f"{gw.base_url}/models did not answer with an OpenAI-style model list")
    missing = [m for m in gw.models if m not in served]
    detail = f"{len(served)} models served" + (f"; not served: {', '.join(missing)}" if missing else "")
    return Status(True, detail, served, missing)


def check(gw: GatewayConfig, timeout: float = 20) -> Status:
    try:
        r = httpx.get(f"{gw.base_url}/models", headers=_headers(gw), timeout=timeout)
    except httpx.HTTPError as e:
        return Status(False, f"no answer from {gw.base_url}: {type(e).__name__}")
    return _parse(gw, r)


async def acheck(gw: GatewayConfig, timeout: float = 20) -> Status:
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            r = await http.get(f"{gw.base_url}/models", headers=_headers(gw))
    except httpx.HTTPError as e:
        return Status(False, f"no answer from {gw.base_url}: {type(e).__name__}")
    return _parse(gw, r)


# -- free_only: what is inside a combo (ADR 0012) ------------------------------------------------

@dataclass(frozen=True)
class ComboCheck:
    name: str
    free: bool
    detail: str
    models: list[str] = field(default_factory=list)


def _combo(gw: GatewayConfig, name: str, r: httpx.Response) -> ComboCheck:
    if r.status_code != 200:
        return ComboCheck(name, False, f"HTTP {r.status_code} from {gw.base_url}/combos: can't check what {name} "
                                       f"routes to (the gateway must list its combos there, as OmniRoute does)")
    try:
        found = [c for c in r.json().get("data") or [] if isinstance(c, dict) and c.get("name") == name]
    except (ValueError, AttributeError, TypeError):
        return ComboCheck(name, False, f"{gw.base_url}/combos did not answer with a combo list")
    if not found:
        return ComboCheck(name, False, f"the gateway has no combo named {name}")
    entries = found[0].get("models") or []
    if not entries:
        return ComboCheck(name, False, f"combo {name} lists no models")
    models: list[str] = []
    paid: list[str] = []
    for e in entries:
        model = str(e.get("model", "")) if isinstance(e, dict) else ""
        kind = e.get("kind", "model") if isinstance(e, dict) else "?"
        label = f"{e.get('providerId', '?')}/{model}" if isinstance(e, dict) else repr(e)
        models.append(label)
        # Anything but a plain model (a nested combo, an unknown entry) can't be checked: not free.
        if kind != "model" or not model.endswith(FREE_SUFFIX):
            paid.append(label)
    if paid:
        return ComboCheck(name, False, f"combo {name} routes to models that are not free: {', '.join(paid)}", models)
    return ComboCheck(name, True, f"combo {name}: {len(models)} free models", models)


def check_combo(gw: GatewayConfig, name: str, timeout: float = 10) -> ComboCheck:
    try:
        r = httpx.get(f"{gw.base_url}/combos", headers=_headers(gw), timeout=timeout)
    except httpx.HTTPError as e:
        return ComboCheck(name, False, f"no answer from {gw.base_url}: {type(e).__name__}")
    return _combo(gw, name, r)


async def acheck_combo(gw: GatewayConfig, name: str, timeout: float = 10) -> ComboCheck:
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            r = await http.get(f"{gw.base_url}/combos", headers=_headers(gw))
    except httpx.HTTPError as e:
        return ComboCheck(name, False, f"no answer from {gw.base_url}: {type(e).__name__}")
    return _combo(gw, name, r)


async def ensure_free(gw: GatewayConfig, worker_model: str) -> str | None:
    """None when this worker model may run under free_only, else why not. Fails closed."""
    if not gw.free_only:
        return None
    if not gw.allowed(worker_model):
        return f"{worker_model} is not a free model of the gateway (free_only)"
    model_id = worker_model.partition("/")[2]
    if model_id.endswith(FREE_SUFFIX):
        return None
    combo = await acheck_combo(gw, model_id)
    return None if combo.free else combo.detail


# -- which models answer right now -------------------------------------------------------------

PROBE_TOKENS = 32


@dataclass(frozen=True)
class Probe:
    model: str              # the id sent to the gateway, e.g. openrouter/cohere/north-mini-code:free
    state: str              # online | limited | refused | offline | skipped
    detail: str
    seconds: float = 0.0
    served_by: str = ""     # the model that answered, as the gateway reports it
    combo: str = ""         # the combo this model belongs to, if any


def _classify(model: str, r: httpx.Response, seconds: float, combo: str) -> Probe:
    if r.status_code == 200:
        try:
            served = str(r.json().get("model") or "")
        except (ValueError, AttributeError):
            served = ""
        return Probe(model, "online", f"answered in {seconds:.1f}s", seconds, served, combo)
    try:
        err = r.json().get("error") or {}
        msg = str(err.get("message") or "") if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        msg = r.text[:200]
    low = msg.lower()
    if r.status_code == 429 or any(w in low for w in ("rate limit", "rate_limit", "cooling down", "quota")):
        return Probe(model, "limited", msg[:200] or f"HTTP {r.status_code}", seconds, combo=combo)
    if r.status_code in (401, 402, 403):
        return Probe(model, "refused", msg[:200] or f"HTTP {r.status_code}", seconds, combo=combo)
    return Probe(model, "offline", msg[:200] or f"HTTP {r.status_code}", seconds, combo=combo)


async def probe(gw: GatewayConfig, model: str, combo: str = "", timeout: float = 60) -> Probe:
    """One tiny request to see whether the model answers now. The reply is capped at PROBE_TOKENS:
    a reasoning model spends its first tokens thinking, and with one token it answers nothing."""
    if gw.free_only and not model.endswith(FREE_SUFFIX):
        return Probe(model, "skipped", "not a free model: not called under free_only", combo=combo)
    body = {"model": model, "max_tokens": PROBE_TOKENS, "messages": [{"role": "user", "content": "Reply with: ok"}]}
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            r = await http.post(f"{gw.base_url}/chat/completions", headers=_headers(gw), json=body)
    except httpx.HTTPError as e:
        return Probe(model, "offline", f"no answer: {type(e).__name__}", loop.time() - started, combo=combo)
    return _classify(model, r, loop.time() - started, combo)


async def acombos(gw: GatewayConfig, timeout: float = 10) -> dict[str, list[str]]:
    """Combo name -> its models as "<provider>/<model>" ids; {} when the gateway lists none."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            r = await http.get(f"{gw.base_url}/combos", headers=_headers(gw))
        data = r.json().get("data") if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return {}
    out: dict[str, list[str]] = {}
    for c in data or []:
        if isinstance(c, dict) and c.get("name"):
            out[str(c["name"])] = [f"{e.get('providerId')}/{e.get('model')}" if e.get("providerId") else str(e.get("model"))
                                   for e in c.get("models") or [] if isinstance(e, dict) and e.get("model")]
    return out


async def probe_all(gw: GatewayConfig, parallel: int = 4) -> list[Probe]:
    """Every model the worker can reach: each configured model, a combo expanded into its models."""
    combos = await acombos(gw)
    targets: list[tuple[str, str]] = []
    for m in gw.models:
        if m in combos:
            targets += [(x, m) for x in combos[m]]
        else:
            targets.append((m, ""))
    unique: dict[str, str] = {}  # model -> combo, first mention wins
    for m, c in targets:
        unique.setdefault(m, c)
    slots = asyncio.Semaphore(parallel)

    async def one(model: str, combo: str) -> Probe:
        async with slots:
            return await probe(gw, model, combo)

    return list(await asyncio.gather(*(one(m, c) for m, c in unique.items())))
