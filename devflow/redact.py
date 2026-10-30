"""Keep secrets out of log files and Telegram messages (PRD SR9).

Agents print what they find, and an exception can carry a connection string.
Everything devflow writes to a log or sends to Telegram goes through
`redact()`: known token formats, `NAME=value` assignments whose name says
secret, and literal values devflow knows are secret (the bot token, secret
environment variables, values from the projects' DENY_READ files).
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from collections.abc import Iterable
from pathlib import Path

MASK = "[redacted]"

PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),                 # Anthropic
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}"),  # OpenAI and similar
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"),                # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),               # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                   # Google API key
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),             # Slack
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),              # Telegram bot token
]
# NAME=value, NAME: value, 'name' => 'value' where the name says it is secret. The value is
# masked, the name kept, so the reader still knows what was there. Code like
# `$password = $this->input->post(...)` is left alone: values starting with $ or ( aren't literals.
_NAMED = re.compile(
    r"""(?ix)
    (\b[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credentials?)[\w.-]*
     ['"]?\s*(?:=>|=|:)\s*)
    (['"]?)([^\s'"$(,;][^\s'",;]{3,})\2
    """)
_SECRET_ENV = re.compile(r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL)")

_literals: set[str] = set()


def add(*values: str) -> None:
    """Treat these exact values as secret from now on. Short values are ignored: masking
    every "1234" would wreck the text without protecting anything."""
    for v in values:
        v = (v or "").strip()
        if len(v) >= 6:
            _literals.add(v)


def add_env(environ: dict[str, str] | None = None) -> None:
    """Values of environment variables whose names say they are secret."""
    for name, value in (environ if environ is not None else os.environ).items():
        if _SECRET_ENV.search(name):
            add(value)


def _values(text: str) -> Iterable[str]:
    """Values assigned in a .env-style or config file."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(?:export\s+)?[\w.-]+\s*=\s*(.*)$", line)
        if m:
            yield m.group(1).strip().strip("'\"")
        for named in _NAMED.finditer(line):
            yield named.group(3)


def add_files(root: Path, patterns: list[str]) -> int:
    """Learn the values in the DENY_READ files at the top of a project (`.env`, a DB config),
    so an agent that prints one doesn't put it in a log. Returns how many files were read."""
    read = 0
    for pattern in patterns:
        pat = pattern.replace("\\", "/").removeprefix("**/")
        if any(ch in pat for ch in "*?[") and "/" not in pat:
            candidates = [p for p in root.iterdir() if p.is_file() and fnmatch.fnmatch(p.name, pat)] \
                if root.is_dir() else []
        else:
            candidates = [root / pat] if "*" not in pat else []
        for path in candidates:
            try:
                if path.is_file() and path.stat().st_size < 1_000_000:
                    add(*_values(path.read_text(encoding="utf-8", errors="replace")))
                    read += 1
            except OSError:
                continue
    return read


def redact(text: str) -> str:
    if not text:
        return text
    for literal in sorted(_literals, key=len, reverse=True):
        if literal in text:
            text = text.replace(literal, MASK)
    for rx in PATTERNS:
        text = rx.sub(MASK, text)
    return _NAMED.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}{m.group(2)}", text)


class Formatter(logging.Formatter):
    """A log formatter whose output, traceback included, is redacted."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
