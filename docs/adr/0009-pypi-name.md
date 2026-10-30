# 9. Published as devflow-bot; the command stays devflow

Date: 2026-10-30 · Status: accepted

## Context

`devflow` on PyPI is taken by an unrelated project (git-flow helpers from
synnefo, last released years ago). Asking PyPI for a dormant name (PEP 541)
takes months and may fail.

## Decision

The distribution is `devflow-bot`. The command (`devflow`) and the import
package (`devflow`) keep their names, so docs, config and `python -m devflow`
are unchanged.

## Consequences

- `pip install devflow` installs the other project. The README and the
  release notes say `pipx install devflow-bot`.
- Installing both distributions into one environment would clash on the
  `devflow` import package; pipx keeps each tool in its own environment.
