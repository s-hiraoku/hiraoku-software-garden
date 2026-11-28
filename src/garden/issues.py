"""GitHub Issue context loader for issue-driven Garden implementation flows."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any


GARDEN_REPORT_MARKER = "<!-- garden-report:"


def _normalize_issue_payload(data: dict[str, Any]) -> dict[str, Any]:
    number_raw = data.get("number")
    number: int | None = None
    if number_raw is not None:
        if (
            not isinstance(number_raw, int)
            or isinstance(number_raw, bool)
            or number_raw <= 0
        ):
            raise ValueError("issue payload 'number' must be a positive integer")
        number = int(number_raw)
    title = str(data.get("title") or "").strip()
    if not title:
        raise ValueError("issue payload must include a non-empty 'title'")
    body = str(data.get("body") or "")
    url = str(data.get("url") or "")
    state = str(data.get("state") or "OPEN")

    raw_labels = data.get("labels") or []
    labels: list[str] = []
    if isinstance(raw_labels, list):
        for item in raw_labels:
            if isinstance(item, str) and item.strip():
                labels.append(item.strip())
            elif isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
                labels.append(item["name"].strip())

    raw_comments = data.get("comments") or []
    comments: list[dict[str, str]] = []
    if isinstance(raw_comments, list):
        for item in raw_comments:
            if not isinstance(item, dict):
                continue
            cbody = str(item.get("body") or "").strip()
            if not cbody or GARDEN_REPORT_MARKER in cbody:
                continue
            author_raw = item.get("author")
            if isinstance(author_raw, dict):
                author = str(author_raw.get("login") or "unknown")
            else:
                author = str(author_raw or "unknown")
            created_at = str(item.get("createdAt") or item.get("created_at") or "")
            comments.append(
                {
                    "author": author,
                    "body": cbody,
                    "created_at": created_at,
                }
            )

    return {
        "number": number,
        "title": title,
        "body": body,
        "url": url,
        "state": state,
        "labels": labels,
        "comments": comments,
    }


def load_issue_context_file(path: Path) -> dict[str, Any]:
    """Load an offline JSON Issue context file (for tests or pre-fetched workflows)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: issue file must contain a JSON object")
    return _normalize_issue_payload(raw)


def fetch_github_issue(issue_number: int, *, cwd: Path) -> dict[str, Any]:
    """Fetch a GitHub Issue (title, body, labels, non-bot comments) via `gh issue view`."""
    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0:
        raise ValueError(f"issue_number must be a positive integer, got {issue_number!r}")
    if not shutil.which("gh"):
        raise RuntimeError("gh CLI is required to fetch GitHub issue context")
    proc = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--json",
            "number,title,body,labels,url,state,comments",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh issue view #{issue_number} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"gh issue view #{issue_number} returned invalid JSON: {error}"
        ) from error
    if not isinstance(data, dict):
        raise RuntimeError(f"gh issue view #{issue_number} returned non-object JSON")
    return _normalize_issue_payload(data)
