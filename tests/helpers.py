"""Shared fixture builders for deterministic Git target-repository tests."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

PSTACK_SHA = "7a583913eb1b8d1a0ee76753d1a2cf072e7b54f9"
GARDEN_SHIM = Path(__file__).resolve().parent.parent / "garden.py"


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def init_target_repo(root: Path, remote: str = "https://example.invalid/app.git") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "-c", "init.templateDir=", "init", "--quiet", "-b", "main")
    git(root, "config", "user.name", "Garden Test")
    git(root, "config", "user.email", "tests@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "remote", "add", "origin", remote)
    return root


def commit_all(root: Path, message: str = "commit") -> str:
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", message)
    return git(root, "rev-parse", "HEAD")


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GARDEN_SHIM), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def sample_spec(status: str = "approved") -> str:
    return f"""---
spec_id: sample-app
version: 1
status: {status}
title: Sample App Spec
owner: "@tester"
---

## 受け入れ条件
- **AC-001**: 正常系で成功する。
- **AC-002**: 必須証拠を出力する。

## 不変条件
- **INV-001**: 重複通知でも1回のみ確定する。
"""
