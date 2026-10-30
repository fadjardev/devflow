# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: a minor version may break
things, and the changelog says how).

## [Unreleased]

### Added

- CI on Windows, Linux and macOS with Python 3.11 and the latest release:
  ruff, mypy and the test suite.
- `LICENSE` (MIT), `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`,
  issue and pull request templates.

### Changed

- Python 3.11 or newer is required (was 3.10).
- The README states what the safety model enforces and what it doesn't yet.

### Fixed

- Removing a worktree with a linked folder failed on Python 3.11.

## 0.1.0

The first version, used by its author on Windows: Telegram bot, plan and review
by Claude Code, coding by opencode in git worktrees, task notes in an Obsidian
vault.
