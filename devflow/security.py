"""File permissions for devflow's own secrets and state (PRD SR11).

`.env` holds the bot token; the runtime folder holds logs, task notes copies
and MCP configs. On a shared machine, other users must not read them. POSIX
uses mode bits; Windows uses the file's ACL, read with `icacls /save` so the
check works with any display language (it compares SIDs, not names).
"""

from __future__ import annotations

import functools
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

# Everyone, Authenticated Users, BUILTIN\Users, Guests: anyone else who can log in.
_BROAD = {"WD", "AU", "BU", "BG", "S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-32-546"}
_ACE = re.compile(r"\((A|OA);[^;]*;[^;]*;[^;]*;[^;]*;([^)]+)\)")


@functools.cache
def _my_sid() -> str:
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout
    return out.strip().rsplit(",", 1)[-1].strip('"')


def _sddl(path: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        saved = Path(tmp) / "acl.txt"
        subprocess.run(["icacls", str(path), "/save", str(saved)], capture_output=True, check=True)
        raw = saved.read_bytes()
    return raw.decode("utf-16-le", errors="replace") if raw[:2] != b"\xef\xbb" else raw.decode()


def loose_permissions(path: Path) -> str:
    """Why someone other than the owner can read `path`, or "" if nobody can."""
    if not path.exists():
        return ""
    if sys.platform == "win32":
        broad = sorted({sid for _, sid in _ACE.findall(_sddl(path)) if sid in _BROAD})
        return f"readable by {', '.join(broad)} (Windows ACL)" if broad else ""
    mode = stat.S_IMODE(path.stat().st_mode)
    return f"mode {mode:o}: readable by group or others" if mode & 0o077 else ""


def restrict(path: Path) -> None:
    """Make `path` readable and writable by its owner only (0600 / 0700, or a user-only ACL)."""
    if sys.platform == "win32":
        grant = f"*{_my_sid()}:(OI)(CI)F" if path.is_dir() else f"*{_my_sid()}:F"
        broad = [f"*{sid}" for sid in sorted(_BROAD) if sid.startswith("S-")]
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant, "/remove:g", *broad],
                       capture_output=True, check=True)
    else:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


def private_dir(path: Path) -> Path:
    """Create `path` if needed, owner-only from the start."""
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
        restrict(path)
    return path
