# 12. Free-tier-only worker models

Date: 2026-11-01 · Status: accepted

## Context

With a gateway (ADR 0010) the worker can reach paid and free models alike.
Someone who wants the worker to cost nothing needs a guarantee that holds
when a config or a gateway combo is edited later, not a convention.

A free model is recognisable by its id: OpenRouter, and gateways that pass
its ids on, end them in `:free`. A gateway combo (e.g. OmniRoute's
`devflow-worker`) has a name of its own that says nothing, and OmniRoute
does not allow `:` in combo names.

## Decision

`[gateway] free_only = true` makes devflow refuse any worker model that is
not `<provider_id>/<id>` where `<id>` ends in `:free` or is a combo listed in
`free_combos`.

- **At load:** every `gateway.models` entry, `agents.opencode_model` and
  every project's `opencode_model` must pass, or devflow does not start.
- **Before every worker run:** a combo's entries are read from the gateway
  (`GET <base_url>/combos`, OmniRoute's format) and every entry must be a
  plain model whose id ends in `:free`. A nested combo, an unknown entry, a
  missing combo or an unreachable gateway refuses the run (fail closed); no
  prompt reaches opencode, and the task is blocked with the reason.
- `devflow doctor` and the dashboard's Gateway page show the same check.

## Consequences

- The check trusts model ids. A gateway that maps a `:free` id to a paid
  model, or an OpenRouter account that pays for a "free" model, is not
  detected. The account-side guarantee is a credit limit of 0 on the
  provider key; the docs say so.
- Combos are checked only on gateways that list them at `/combos` like
  OmniRoute. Elsewhere, list `:free` model ids directly instead of a combo.
- Planning, review, closing and `/ask` run on Claude Code and are never free
  tier; `free_only` covers the worker only. Their cost stays under `[budget]`.
