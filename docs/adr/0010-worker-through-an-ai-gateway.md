# 10. The worker's models through an AI gateway

Date: 2026-11-01 · Status: accepted

## Context

The worker writes most of the tokens. Free model tiers (OpenRouter `:free`,
Gemini AI Studio, Groq, Cerebras and others) can carry that work, but each has
small daily quotas: OpenRouter's free models allow about 50 requests a day
without credit, and an agentic worker makes one request per tool call.
Self-hosted gateways (OmniRoute, 9Router, LiteLLM) put many providers behind
one OpenAI-compatible endpoint and fall back to the next provider when one is
exhausted.

## Decision

A `[gateway]` table names the endpoint, the models and the environment
variable holding the key. devflow writes it into the runtime `opencode.json`
as an `@ai-sdk/openai-compatible` provider, with the key as an `{env:NAME}`
reference, and the first model becomes the worker default unless
`agents.opencode_model` or a project's `opencode_model` says otherwise.

Only the worker goes through the gateway. Planning and review stay on Claude
Code: the judgment is where a strong model pays for itself, and the reviewer
still reads every diff a free model writes.

devflow does not pick a gateway or manage its fallback order; that stays in
the gateway's own config.

## Consequences

- No key is ever written to a file by devflow; `doctor` says when the
  variable is unset.
- The provider is registered only in the `opencode serve` that devflow starts.
  An opencode server you started yourself must have the provider in its own
  config; `doctor` points this out.
- A gateway out of every fallback answers 429, which the existing limit
  handling (`usage.opencode_limit`) already turns into a parked task with a
  reset time.
- Free models report no cost, so token caps are the meaningful budget for
  them.
