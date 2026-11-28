"""GitHub Issue context loader and repository-identity helpers for Garden flows."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
from urllib import parse as urllib_parse

from garden.report import parse_garden_markers


TRUSTED_AUTHOR_ASSOCIATIONS: frozenset[str] = frozenset(
    {"OWNER", "MEMBER", "COLLABORATOR"}
)

_SCP_REMOTE_RE = re.compile(
    r"^(?:[^@\s/]+@)?(?P<host>[A-Za-z0-9._-]+):(?P<path>[^/\s].*)$"
)


def parse_repo_identity(url_or_slug: str | None) -> str | None:
    """Normalize a GitHub/GHE remote or Issue/PR URL into lowercase 'host/owner/repo'.

    Returns None for local filesystem paths, empty strings, or unparseable URLs.
    """
    raw = str(url_or_slug or "").strip()
    if not raw:
        return None

    host = ""
    path_str = ""
    if "://" in raw:
        parsed = urllib_parse.urlparse(raw)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https", "ssh", "git"):
            return None
        host = (parsed.hostname or "").lower()
        path_str = parsed.path or ""
    else:
        scp = _SCP_REMOTE_RE.match(raw)
        if not scp:
            return None
        host = scp.group("host").lower()
        path_str = scp.group("path")

    if not host or "." not in host:
        return None
    segments = [seg for seg in path_str.strip("/").split("/") if seg]
    if len(segments) < 2:
        return None
    owner = segments[0].lower()
    repo = segments[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    repo = repo.lower()
    if not owner or not repo:
        return None
    return f"{host}/{owner}/{repo}"


def _is_bot_author(author_raw: Any) -> bool:
    if isinstance(author_raw, dict):
        if author_raw.get("is_bot") is True:
            return True
        for key in ("type", "__typename"):
            val = author_raw.get(key)
            if isinstance(val, str) and val.strip().lower() == "bot":
                return True
        login = str(author_raw.get("login") or "").strip().lower()
        if login.endswith("[bot]"):
            return True
        return False
    if isinstance(author_raw, str):
        return author_raw.strip().lower().endswith("[bot]")
    return False


def _author_login(author_raw: Any) -> str:
    if isinstance(author_raw, dict):
        return str(author_raw.get("login") or "unknown").strip() or "unknown"
    return str(author_raw or "unknown").strip() or "unknown"


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
    title = " ".join(str(data.get("title") or "").split())
    if not title:
        raise ValueError("issue payload must include a non-empty 'title'")
    body = str(data.get("body") or "")
    url = str(data.get("url") or "").strip()
    state = str(data.get("state") or "OPEN").strip() or "OPEN"
    updated_at = str(data.get("updatedAt") or data.get("updated_at") or "").strip()

    explicit_repo = data.get("repository")
    repository = (
        parse_repo_identity(url)
        or (
            parse_repo_identity(explicit_repo)
            if isinstance(explicit_repo, str)
            else None
        )
    )
    if (
        repository is None
        and isinstance(explicit_repo, str)
        and explicit_repo.strip().count("/") == 2
        and "." in explicit_repo.strip().split("/", 1)[0]
    ):
        repository = explicit_repo.strip().lower()

    raw_labels = data.get("labels") or []
    labels: list[str] = []
    if isinstance(raw_labels, list):
        for item in raw_labels:
            if isinstance(item, str) and item.strip():
                labels.append(item.strip())
            elif (
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item["name"].strip()
            ):
                labels.append(item["name"].strip())

    raw_comments = data.get("comments") or []
    comments: list[dict[str, str]] = []
    if isinstance(raw_comments, list):
        for item in raw_comments:
            if not isinstance(item, dict):
                continue
            cbody = str(item.get("body") or "").strip()
            if not cbody:
                continue
            # Exclude automated Garden reports using the canonical marker parser
            if parse_garden_markers(cbody):
                continue
            author_raw = item.get("author")
            if _is_bot_author(author_raw):
                continue
            assoc_raw = item.get("authorAssociation") or item.get("author_association")
            assoc = str(assoc_raw or "").strip().upper()
            if assoc not in TRUSTED_AUTHOR_ASSOCIATIONS:
                continue
            author = _author_login(author_raw)
            created_at = str(item.get("createdAt") or item.get("created_at") or "").strip()
            comments.append(
                {
                    "author": author,
                    "author_association": assoc,
                    "body": cbody,
                    "created_at": created_at,
                }
            )

    return {
        "number": number,
        "title": title,
        "body": body,
        "url": url,
        "repository": repository,
        "state": state,
        "updated_at": updated_at,
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
    """Fetch a GitHub Issue (title, body, labels, trusted non-bot comments) via `gh issue view`."""
    if (
        isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number <= 0
    ):
        raise ValueError(
            f"issue_number must be a positive integer, got {issue_number!r}"
        )
    if not shutil.which("gh"):
        raise RuntimeError("gh CLI is required to fetch GitHub issue context")
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--json",
                "number,title,body,labels,url,state,updatedAt,comments",
            ],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"gh issue view #{issue_number} timed out after {error.timeout}s"
        ) from error
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
