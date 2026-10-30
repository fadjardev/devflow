"""What each agent run used, and whether a provider limit stopped it (PRD §5.7).

Only documented fields are read (ADR 0007):

- Claude Code (`claude -p --output-format stream-json --verbose`): the final
  `result` message's `total_cost_usd`, `modelUsage` (per model: inputTokens,
  outputTokens, cacheReadInputTokens, cacheCreationInputTokens, costUSD) and
  `api_error_status`; `rate_limit_event` messages (`rate_limit_info.status`,
  `resetsAt`, `utilization`); and an assistant message's `error`
  (`rate_limit`, `billing_error`).
- opencode: each assistant message's `cost` and `tokens` (input, output,
  reasoning, cache.read, cache.write), and its `error` (`statusCode` 429, or a
  quota/credit message).

A resumed Claude session reports the conversation's whole total, so callers
record the difference from the session's previous total (`UsageDB.delta`).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float | None = None  # None: the backend reported no cost (e.g. a subscription)
    model: str = ""
    models: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def __sub__(self, other: Usage) -> Usage:
        cost = None if self.cost_usd is None else max(0.0, self.cost_usd - (other.cost_usd or 0.0))
        return Usage(max(0, self.input - other.input), max(0, self.output - other.output),
                     max(0, self.cache_read - other.cache_read), max(0, self.cache_write - other.cache_write),
                     cost, self.model, self.models)


@dataclass(frozen=True)
class Limit:
    """A provider refused (`hit`) or warned (`utilization`) about a usage or rate limit."""
    backend: str
    hit: bool
    reason: str
    resets_at: dt.datetime | None = None
    utilization: float | None = None  # 0..1, when the provider says


def _epoch(value: Any) -> dt.datetime | None:
    """`resetsAt` is a number; the docs don't name the unit. Seconds and milliseconds since
    the epoch are told apart by size (ADR 0007)."""
    if not isinstance(value, int | float) or value <= 0:
        return None
    seconds = value / 1000 if value > 1e11 else value
    return dt.datetime.fromtimestamp(seconds, tz=dt.UTC)


def _fraction(value: Any) -> float | None:
    if not isinstance(value, int | float):
        return None
    return value / 100 if value > 1 else float(value)


# -- Claude Code ----------------------------------------------------------------------------

def claude_usage(result: dict[str, Any]) -> Usage:
    models = result.get("modelUsage") or {}
    total = Usage()
    for m in models.values():
        total = Usage(total.input + int(m.get("inputTokens") or 0), total.output + int(m.get("outputTokens") or 0),
                      total.cache_read + int(m.get("cacheReadInputTokens") or 0),
                      total.cache_write + int(m.get("cacheCreationInputTokens") or 0))
    main = max(models, key=lambda n: float(models[n].get("costUSD") or 0), default="")
    cost = result.get("total_cost_usd")
    return Usage(total.input, total.output, total.cache_read, total.cache_write,
                 float(cost) if isinstance(cost, int | float) else None, main,
                 {n: dict(m) for n, m in models.items()})


def claude_limit(result: dict[str, Any], events: list[dict[str, Any]], errors: list[str]) -> Limit | None:
    """A refused run (rate_limit_event "rejected", an assistant `rate_limit`/`billing_error`, or
    HTTP 429) is a hit; an "allowed_warning" with utilization is an early warning."""
    infos = [e.get("rate_limit_info") or {} for e in events]
    rejected = [i for i in infos if i.get("status") == "rejected"]
    if rejected or result.get("api_error_status") == 429 or {"rate_limit", "billing_error"} & set(errors):
        info = rejected[-1] if rejected else (infos[-1] if infos else {})
        reason = ("usage credits required" if info.get("errorCode") == "credits_required"
                  else "billing error" if "billing_error" in errors else "usage or rate limit reached")
        return Limit("claude", True, reason, _epoch(info.get("resetsAt")), _fraction(info.get("utilization")))
    warned = [i for i in infos if i.get("status") == "allowed_warning"]
    if warned:
        i = warned[-1]
        return Limit("claude", False, "approaching the usage limit", _epoch(i.get("resetsAt")),
                     _fraction(i.get("utilization")))
    return None


# -- opencode -------------------------------------------------------------------------------

def opencode_usage(messages: list[dict[str, Any]]) -> Usage:
    """Totals over a session's assistant messages (GET /session/{id}/message)."""
    total, cost, model, seen_cost = Usage(), 0.0, "", False
    for msg in messages:
        info = msg.get("info") or {}
        if info.get("role") != "assistant":
            continue
        t = info.get("tokens") or {}
        cache = t.get("cache") or {}
        total = Usage(total.input + int(t.get("input") or 0),
                      total.output + int(t.get("output") or 0) + int(t.get("reasoning") or 0),
                      total.cache_read + int(cache.get("read") or 0), total.cache_write + int(cache.get("write") or 0))
        if isinstance(info.get("cost"), int | float):
            cost += float(info["cost"])
            seen_cost = True
        model = "/".join(x for x in (info.get("providerID"), info.get("modelID")) if x) or model
    return Usage(total.input, total.output, total.cache_read, total.cache_write,
                 cost if seen_cost else None, model)


_QUOTA = re.compile(r"(?i)\b(quota|insufficient[_ ](?:quota|credit|funds)|credit balance|rate.?limit|too many requests)")


def opencode_limit(error: dict[str, Any] | None) -> Limit | None:
    """A provider error on the session: HTTP 429, or a quota/credit message."""
    if not error:
        return None
    data = error.get("data") or {}
    status = data.get("statusCode")
    message = str(data.get("message") or error.get("message") or "")
    if status == 429 or _QUOTA.search(message):
        headers = {str(k).lower(): v for k, v in (data.get("responseHeaders") or {}).items()}
        resets = None
        retry = headers.get("retry-after")
        if retry and str(retry).isdigit():
            resets = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=int(retry))
        return Limit("opencode", True, f"provider limit: {message[:120] or 'HTTP 429'}", resets)
    return None
