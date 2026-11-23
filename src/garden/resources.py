"""Locate Nursery resources (templates/, harnesses/) for both checkouts and installed wheels."""

from __future__ import annotations

from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent


def nursery_root() -> Path:
    """Return the directory that holds ``templates/`` and ``harnesses/``.

    In a repository checkout (or an editable install) this is the repository root.
    When installed from a wheel (e.g. ``uvx --from git+...``), the same directories
    are shipped inside the package under ``garden/_nursery/``.
    """
    checkout_root = _PKG_DIR.parents[1]
    # 無関係な templates/ を誤って拾わないよう、ナーサリー固有のファイルも合わせて確認する
    if (checkout_root / "templates").is_dir() and (
        checkout_root / "harnesses" / "pstack.toml"
    ).is_file():
        return checkout_root
    return _PKG_DIR / "_nursery"
