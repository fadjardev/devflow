"""Markdown, as agents and the pipeline write it, turned into Telegram's HTML subset.

Narrow on purpose: fenced code, `inline code`, **bold**, `# headings`, `- bullets`,
and a line wrapped in `_underscores_` as italics. Nothing else is interpreted, so
snake_case names and `*` in code survive untouched. Telegram rejects a message
whose tags don't match, so messages are split on lines (never inside a code
block) before they are converted, and each piece is converted on its own.
"""

from __future__ import annotations

import html
import re

LIMIT = 3500  # of source text per message; the HTML grows by its tags and escapes, Telegram caps at 4096

_FENCE = re.compile(r"(```[^\n]*\n.*?```)", re.S)
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*]\s+(.*)$")
_ITALIC_LINE = re.compile(r"^_(\S.*\S)_$")
_CODE = re.compile(r"(`[^`\n]+`)")
_BOLD = re.compile(r"\*\*(\S(?:.*?\S)?)\*\*")


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _inline(text: str) -> str:
    out = []
    for piece in _CODE.split(text):
        if len(piece) > 2 and piece[0] == piece[-1] == "`":
            out.append(f"<code>{_esc(piece[1:-1])}</code>")
        else:
            out.append(_BOLD.sub(r"<b>\1</b>", _esc(piece)))
    return "".join(out)


def _line(line: str) -> str:
    if m := _HEADING.match(line):
        return f"<b>{_inline(m.group(1))}</b>"
    if m := _BULLET.match(line):
        return f"{m.group(1)}• {_inline(m.group(2))}"
    if m := _ITALIC_LINE.match(line):
        return f"<i>{_inline(m.group(1))}</i>"
    return _inline(line)


def to_html(md: str) -> str:
    out = []
    for part in _FENCE.split(md):
        if part.startswith("```") and part.endswith("```") and "\n" in part:
            body = part[part.index("\n") + 1:-3].rstrip("\n")
            out.append(f"<pre>{_esc(body)}</pre>")
        else:
            out.append("\n".join(_line(line) for line in part.split("\n")))
    return "".join(out)


def split(md: str, limit: int = LIMIT) -> list[str]:
    """Pieces of at most ~limit characters, cut between lines. A code block that has to
    be cut is closed at the end of one piece and reopened at the start of the next."""
    lines: list[str] = []
    for line in md.split("\n"):
        while len(line) > limit:  # a single giant line: hard-cut it
            lines.append(line[:limit])
            line = line[limit:]
        lines.append(line)

    chunks: list[str] = []
    cur: list[str] = []
    size, fence = 0, ""
    for line in lines:
        if cur and size + len(line) + 1 > limit:
            if fence:
                cur.append("```")
            chunks.append("\n".join(cur).strip("\n"))
            cur, size = ([fence] if fence else []), len(fence)
        cur.append(line)
        size += len(line) + 1
        if line.lstrip().startswith("```"):
            fence = "" if fence else line.strip()
    if cur:
        chunks.append("\n".join(cur).strip("\n"))
    return [c for c in chunks if c.strip()]
