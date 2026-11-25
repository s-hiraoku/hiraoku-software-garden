"""Allow running the Software Garden CLI via `python3 -m garden`."""

from __future__ import annotations

import sys

from garden.cli import main

if __name__ == "__main__":
    sys.exit(main())
