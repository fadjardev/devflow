# 8. Config and data folders from platformdirs

Date: 2026-10-30 · Status: accepted

## Context

devflow kept its runtime files (logs, locks, MCP configs, screenshots) in
`runtime/` beside the source, which doesn't exist for an installed package and
is often world-readable.

## Decision

In TOML mode the config file defaults to the user config folder and the
runtime folder to the user data folder, as `platformdirs` names them
(`%APPDATA%` / `%LOCALAPPDATA%` on Windows, XDG folders on Linux,
`~/Library/Application Support` on macOS). `paths.runtime` overrides it.
Legacy mode keeps `runtime/` beside the package.

## Consequences

One small, dependency-free package (MIT) is added. The runtime folder is
created owner-only (see `docs/security.md`).
