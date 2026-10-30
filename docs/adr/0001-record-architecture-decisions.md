# 1. Record architecture decisions

Date: 2026-10-30 · Status: accepted

## Context

devflow is becoming a public project. Decisions about the safety model, config
and storage need to survive the conversation they were made in, so
contributors can see why something is the way it is before changing it.

## Decision

Each significant decision gets a short record in `docs/adr/NNNN-title.md`:
context, decision, consequences. Records are append-only; a later record can
supersede an earlier one and says so.

## Consequences

A pull request that changes a recorded decision adds a new record instead of
editing history.
