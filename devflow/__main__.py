"""`python -m devflow` is the `devflow` command (see cli.py)."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    main(sys.argv[1:])
