"""Backwards-compatible entry point delegating to src/garden/cli.py.

When invoked via `python3 garden.py` or imported from repository root (e.g.
`python3 -m unittest`), repository root sits ahead of `src/` on `sys.path`.
Setting `__path__` exposes `src/garden/` as a package seamlessly so both
`python3 garden.py ...` and `from garden.<module> import ...` work reliably.
"""

from __future__ import annotations

from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parent
_SRC_DIR = (_REPO_ROOT / "src").resolve()
_PKG_DIR = (_SRC_DIR / "garden").resolve()

__path__ = [str(_PKG_DIR)]
__version__ = "0.1.0"


def _ensure_src_on_path() -> None:
    src_str = str(_SRC_DIR)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


def main() -> int:
    _ensure_src_on_path()
    from garden.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
