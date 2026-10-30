"""Markdown notes with YAML frontmatter, written safely.

Every write in devflow goes through here: paths are confined to an allow-list
of vault folders, files are replaced atomically, and a lock (kept outside the
vault, so Obsidian never sees it) serialises the bot and the MCP server.
"""

from __future__ import annotations

import contextlib
import copy
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

# Folders anything in devflow may write to, relative to the vault root.
WRITABLE = ("05-Tasks", "04-Requests", "01-Projects")

_LOCK_DIR = Path(tempfile.gettempdir()) / "devflow-locks"
_FM = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)
_H2 = re.compile(r"^## (.+?)\s*$", re.M)


class VaultError(ValueError):
    """A write that devflow refuses to make."""


def safe_path(vault: Path, relative: str | Path) -> Path:
    """Resolve `relative` under the vault and refuse anything outside WRITABLE."""
    root = vault.resolve()
    target = (root / relative).resolve()
    try:
        rel = target.relative_to(root)
    except ValueError:
        raise VaultError(f"path escapes the vault: {relative}") from None
    if not rel.parts or rel.parts[0] not in WRITABLE:
        raise VaultError(f"not a writable vault folder: {relative}")
    if target.suffix != ".md":
        raise VaultError(f"only .md notes may be written: {relative}")
    return target


@contextlib.contextmanager
def locked(path: Path, timeout: float = 15.0):
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", str(path.resolve()))[-150:]
    lock = _LOCK_DIR / f"{name}.lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            # A lock older than a minute belongs to a crashed process.
            with contextlib.suppress(FileNotFoundError):
                if time.time() - lock.stat().st_mtime > 60:
                    lock.unlink()
                    continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"note is locked: {path}") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            lock.unlink()


_DQ = re.compile(r'"([^"\n]*)"')


def _load_yaml(text: str) -> dict:
    """Obsidian tolerates `path: "E:\\dev\\x"`; strict YAML reads `\\d` as an escape. Accept both."""
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError:
        fixed = _DQ.sub(lambda m: '"' + m.group(1).replace("\\", "\\\\") + '"', text)
        return yaml.safe_load(fixed) or {}


@dataclass
class Note:
    meta: dict
    body: str
    raw_fm: str | None = None   # frontmatter exactly as read
    _snapshot: dict | None = None

    @classmethod
    def parse(cls, text: str) -> Note:
        m = _FM.match(text)
        if not m:
            return cls({}, text)
        meta = _load_yaml(m.group(1))
        return cls(meta, text[m.end():], m.group(1), copy.deepcopy(meta))

    def render(self) -> str:
        if not self.meta and self.raw_fm is None:
            return self.body
        if self.raw_fm is not None and self.meta == self._snapshot:
            fm = self.raw_fm  # untouched: write it back byte for byte, quirks and all
        else:
            fm = yaml.safe_dump(self.meta, sort_keys=False, allow_unicode=True).strip()
        return f"---\n{fm}\n---\n{self.body}"

    # -- sections ----------------------------------------------------------

    def _span(self, heading: str) -> tuple[int, int] | None:
        """(start of content, end of content) for `## heading`."""
        for m in _H2.finditer(self.body):
            if m.group(1).strip().lower() == heading.lower():
                nxt = _H2.search(self.body, m.end())
                return m.end(), (nxt.start() if nxt else len(self.body))
        return None

    def section(self, heading: str) -> str | None:
        span = self._span(heading)
        return None if span is None else self.body[span[0]:span[1]].strip()

    def set_section(self, heading: str, text: str, append: bool = False, sep: str = "\n\n") -> None:
        text = text.strip()
        span = self._span(heading)
        if span is None:
            self.body = self.body.rstrip() + f"\n\n## {heading}\n\n{text}\n"
            return
        start, end = span
        current = self.body[start:end].strip()
        new = f"{current}{sep}{text}" if append and current else text
        tail = self.body[end:]
        self.body = self.body[:start] + f"\n\n{new}\n" + ("\n" + tail if tail else "")


def read(path: Path) -> Note:
    return Note.parse(path.read_text(encoding="utf-8"))


def write(path: Path, note: Note) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Dot-prefixed temp file: Obsidian ignores it during the brief moment it exists.
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(note.render(), encoding="utf-8", newline="\n")
    os.replace(tmp, path)
