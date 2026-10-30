# 7. What Claude Code reports about usage and limits, and what devflow uses

Date: 2026-10-30 · Status: accepted

## Context

PRD §5.7 asks devflow to record tokens and cost per run, detect usage and
rate limits with their reset time, and warn before a limit is hit, building
only on documented fields.

## What the documentation says (Agent SDK TypeScript reference and "Run Claude Code programmatically", checked 2026-10-30, Claude Code 2.1.312)

- The final `result` message (`--output-format json`, and the last line of
  `stream-json`) carries `total_cost_usd`, `usage`, `modelUsage` (per model:
  `inputTokens`, `outputTokens`, `cacheReadInputTokens`,
  `cacheCreationInputTokens`, `costUSD`, …), `is_error`, `api_error_status`
  (the HTTP status of the API error that ended the run) and
  `terminal_reason`. Costs are client-side estimates.
- With `--resume`, the result reports **the conversation's whole total**, not
  the run's.
- Only `stream-json` carries `rate_limit_event` messages:
  `rate_limit_info.status` (`allowed`, `allowed_warning`, `rejected`),
  `resetsAt` (a number; the unit is not documented), `utilization`, and
  `errorCode: "credits_required"` when a subscription's usage is exhausted.
- An assistant message can carry `error`, one of `rate_limit` (429 against the
  quota), `billing_error`, `overloaded` (529) and others.

## What a real run showed

`claude -p --output-format stream-json --verbose` printed `system` messages,
the `assistant` message, one `rate_limit_event` and the `result`. The event
had `status: "allowed"` and `resetsAt: 1793337000` (seconds since the epoch:
the end of the current five-hour window). It had no top-level `utilization`
while allowed. It also carried undocumented fields (`rateLimitType`,
`unifiedWindows` with per-window utilization, `overageStatus`).

## Decision

- devflow runs Claude with `--output-format stream-json --verbose` and reads
  the `result` message as before, plus every `rate_limit_event` and assistant
  `error`.
- Usage: `modelUsage` token counts summed over models, `total_cost_usd` as
  the cost, the model with the highest cost as the run's model. For a resumed
  session devflow records the difference from the session's previous total.
- A **limit hit** is a `rate_limit_event` with `status: "rejected"`, an
  assistant `error` of `rate_limit` or `billing_error`, or
  `api_error_status: 429`. The reset time is the last event's `resetsAt`;
  values above 10^11 are read as milliseconds, others as seconds. No reset
  time means no automatic resume.
- An **early warning** is a `rate_limit_event` with
  `status: "allowed_warning"` and a `utilization`, compared with the budget's
  `warn_at` thresholds.
- Undocumented fields (`unifiedWindows`, `rateLimitType`, …) are not used.

## Consequences

- If Claude Code changes these fields, detection degrades to a generic error;
  `tests/fixtures/claude_stream_*.jsonl` pin the shapes devflow expects.
- stream-json output is larger than json; devflow keeps only what it parses.
- opencode: devflow reads the documented session messages
  (`GET /session/{id}/message`: each assistant message's `cost` and `tokens`)
  before and after each prompt, and treats an `APIError` with `statusCode` 429
  or a quota/credit message as a limit hit, with `Retry-After` as the reset.
