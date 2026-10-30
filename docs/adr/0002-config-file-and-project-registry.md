# 2. devflow.toml with a project registry; .env stays as legacy mode

Date: 2026-10-30 · Status: accepted

## Context

Until v0.2 devflow read `.env` beside the package. Projects were the notes in
an Obsidian vault's `01-Projects` folder, so there was no devflow without a
vault, and missing settings silently fell back to the author's own paths
(`E:\dev\...`). Settings that differ per project (base branch, linked folders,
secret files, the worker's allowed commands) could only be set once for all.

## Decision

- Settings live in `devflow.toml` (stdlib `tomllib`, hence Python 3.11+):
  global tables plus `[projects.<name>]`, the **registry**, which is the only
  authority on which projects exist. Each project can override `base_branch`,
  `link_paths`, `deny_read`, `worker_shell_allow` and `check_env_allow`.
- Lookup: `--config`, `DEVFLOW_CONFIG`, then the user config folder. With none
  of these, devflow reads `.env` and the environment exactly as before
  (**legacy mode**), including projects from the vault notes. A test pins that
  a pre-0.2 `.env` produces the same settings.
- No machine-specific defaults. A missing required setting is an error that
  names it; unknown keys are errors (they are usually typos).
- The vault is optional. Without one, devflow keeps the same note layout in
  its runtime folder, so the task store, devflow-mcp and the notes copy Claude
  reads work unchanged.
- `devflow migrate-env` writes the TOML from a `.env`; it keeps the runtime
  folder where it is, so logs and history don't move.

## Consequences

- Legacy mode is a second code path to keep working; it is deprecated and can
  go no earlier than v0.4.
- In TOML mode `.env` is not read, so `migrate-env` writes the bot token into
  the TOML, which devflow keeps owner-only.
