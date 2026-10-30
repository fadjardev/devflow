# 5. Linked folders are watched, not copied

Date: 2026-10-30 · Status: accepted

## Context

`LINK_PATHS` folders (typically `vendor`, `node_modules`) are gitignored but
needed to run the app, so each worktree links them to the main checkout
(junctions on Windows, symlinks elsewhere). A write inside a linked folder
lands in the main checkout, outside the diff and the review (finding S3).

Options considered:

1. **Copy** the folders into each worktree. Safe, but `vendor` and
   `node_modules` are often hundreds of megabytes and tens of thousands of
   files; copying on every gate 1 is slow and doubles disk use per task.
2. **Read-only links.** Neither junctions nor symlinks can be made read-only
   for one process; changing the target's permissions would also block the
   developer.
3. **Watch**: record a snapshot of each linked folder (every file's path,
   size and modification time) when the worktree is made, and compare it
   before devflow commits and at gate 2.

## Decision

Option 3. A change blocks the task before the commit and refuses approval at
gate 2, naming the folder. The snapshot stores one hash per folder, so the
message names the folder, not the file.

## Consequences

- Writes are **detected, not prevented**. The planted file stays in the main
  checkout until the developer removes it (`docs/security.md` says so).
- A developer who runs `composer install` in the main checkout while a task is
  open stops that task too; the message explains how to continue.
- Walking a large `vendor` takes about a second per check.
- Copying stays possible later as a per-project option for small folders.
