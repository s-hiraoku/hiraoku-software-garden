"""PR watch and guarded auto-fix loop (`garden pr-watch`) — Phase 2.

Monitors an open GitHub PR for CI failures, merge conflicts, behind-base status,
and unresolved actionable review comments (repo write-permission humans +
explicitly trusted reviewers + CodeRabbit/Codex bots). Fixes execute inside an
isolated git worktree with scrubbed environment + OS/custom sandboxing, bounded
by the freshly fetched remote Trust Anchor (`guard.py`). The candidate commit is
frozen BEFORE running local checks on an immutable snapshot of that exact SHA,
and only that verified SHA is pushed non-force. `garden pr-watch` NEVER merges a
PR and marks merge-ready PRs with `garden:pr-ready` (distinct from Garden's
spec-verified `garden:harvest-ready` label).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

from garden.adapters import (
    AgentRequest,
    AgentResponse,
    run_command_adapter,
    run_mock_adapter,
)
from garden.adapters.providers import select_pr_fix_findings
from garden.checks import run_check
from garden.definition import (
    AgentConfig,
    CheckConfig,
    DelegationConfig,
    FlowConfig,
    GardenDefinition,
)
from garden.engine import (
    VERDICT_EXIT_CODES,
    _apply_effective_delegation,
    _referenced_active_checks,
)
from garden.git_utils import git_run, head_commit, list_remotes, repo_root
from garden.guard import snapshot_untracked_files, verify_post_agent_guard
from garden.harness import evaluate_harness_requirements
from garden.issues import _is_bot_author, parse_repo_identity
from garden.report import (
    MANAGED_LABEL_SPECS,
    parse_garden_markers,
)
from garden.spec import verify_trust_anchor


DEFAULT_TRUSTED_REVIEW_BOTS: frozenset[str] = frozenset(
    {
        "coderabbitai",
        "coderabbitai[bot]",
        "chatgpt-codex-connector",
        "chatgpt-codex-connector[bot]",
        "copilot-pull-request-reviewer",
        "copilot-pull-request-reviewer[bot]",
    }
)
PR_WATCH_MANAGED_LABELS: tuple[str, ...] = (
    "garden:running",
    "garden:needs-human",
    "garden:pr-ready",
)
_WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})
_CODEX_BOT_LOGINS = frozenset({"chatgpt-codex-connector", "chatgpt-codex-connector[bot]"})
_CODERABBIT_BOT_LOGINS = frozenset({"coderabbitai", "coderabbitai[bot]"})
_CODEX_STATE_RE = re.compile(r"<!--\s*codex-review-state:v1\s+(\{.*?\})\s*-->", re.DOTALL)

_PENDING_CHECK_STATUSES = frozenset(
    {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED"}
)
_FAILED_CHECK_CONCLUSIONS = frozenset(
    {
        "FAILURE",
        "TIMED_OUT",
        "CANCELLED",
        "ACTION_REQUIRED",
        "STARTUP_FAILURE",
        "STALE",
        "ERROR",
    }
)
_MERGE_READY_STATES = frozenset({"CLEAN", "HAS_HOOKS"})
_PR_URL_NUM_RE = re.compile(r"/pull/(\d+)(?:[/?#].*)?$")
_ACTIONS_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")
_CONFLICT_MARKER_RE = re.compile(r"^(?:<{7}\s|={7}$|>{7}\s)", re.MULTILINE)
_GH_TIMEOUT_SEC = 30
_MAX_LOG_EXCERPT_CHARS = 3500
_SENSITIVE_ENV_SUBSTRINGS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "API_KEY",
    "APIKEY",
    "PRIVATE_KEY",
    "CREDENTIAL",
    "AUTH",
)
_SENSITIVE_ENV_EXACT = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_PAT",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "NPM_TOKEN",
        "NODE_AUTH_TOKEN",
        "PYPI_API_TOKEN",
        "TWINE_PASSWORD",
    }
)
_SENSITIVE_ENV_PREFIXES = ("AWS_", "AZURE_", "DOCKER_", "GH_", "GITHUB_")
_ADAPTER_ALLOWED_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"),
    "gemini": (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
    ),
    "codex": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "antigravity": (),
    "custom": (),
    "mock": (),
}
_ADAPTER_HOME_WRITE_SUBDIRS: dict[str, tuple[str, ...]] = {
    "claude": (".claude", ".claude.json"),
    "gemini": (".gemini",),
    "codex": (".codex",),
    "antigravity": (".gemini",),
}
_DENIED_HOME_SUBDIRS: tuple[str, ...] = (
    ".ssh",
    ".config/gh",
    ".aws",
    ".gnupg",
    ".netrc",
    ".kube",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".docker",
)


@dataclass(frozen=True)
class PrCheckItem:
    name: str
    status: str  # passed | failed | pending
    conclusion: str
    details_url: str = ""
    log_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "conclusion": self.conclusion,
            "details_url": self.details_url,
            "log_excerpt": self.log_excerpt,
        }


@dataclass(frozen=True)
class PrReviewFinding:
    finding_id: str
    author: str
    author_association: str
    body: str
    path: str = ""
    line: int | None = None
    created_at: str = ""
    source: str = "thread"  # thread | review

    @property
    def version_key(self) -> str:
        digest = hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:16]
        return f"{self.finding_id}:{digest}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "version_key": self.version_key,
            "author": self.author,
            "author_association": self.author_association,
            "path": self.path,
            "line": self.line,
            "body": self.body,
            "created_at": self.created_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class PrSnapshot:
    number: int
    url: str
    state: str
    is_draft: bool
    is_cross_repository: bool
    mergeable: str
    merge_state_status: str
    head_ref_name: str
    base_ref_name: str
    head_ref_oid: str
    checks: tuple[PrCheckItem, ...]
    findings: tuple[PrReviewFinding, ...]
    review_decision: str = ""
    review_pending: bool = False
    review_threads_complete: bool = True
    review_threads_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "url": self.url,
            "state": self.state,
            "is_draft": self.is_draft,
            "is_cross_repository": self.is_cross_repository,
            "mergeable": self.mergeable,
            "merge_state_status": self.merge_state_status,
            "review_decision": self.review_decision,
            "head_ref_name": self.head_ref_name,
            "base_ref_name": self.base_ref_name,
            "head_ref_oid": self.head_ref_oid,
            "checks": [c.to_dict() for c in self.checks],
            "findings": [f.to_dict() for f in self.findings],
            "review_pending": self.review_pending,
            "review_threads_complete": self.review_threads_complete,
            "review_threads_error": self.review_threads_error,
        }


def validate_watch_options(
    *,
    poll_interval_sec: int,
    max_rounds: int,
    max_wait_sec: int,
) -> None:
    if isinstance(poll_interval_sec, bool) or poll_interval_sec < 1:
        raise ValueError("poll_interval_sec must be >= 1")
    if isinstance(max_rounds, bool) or max_rounds < 0:
        raise ValueError("max_rounds must be >= 0")
    if isinstance(max_wait_sec, bool) or max_wait_sec < 1:
        raise ValueError("max_wait_sec must be >= 1")


def parse_pr_number_from_url(pr_url: str) -> int | None:
    match = _PR_URL_NUM_RE.search(str(pr_url or "").strip())
    if not match:
        return None
    num = int(match.group(1))
    return num if num > 0 else None


def _resolve_origin_repo(root: Path) -> tuple[str | None, str | None]:
    """Return (repo_identity 'host/owner/repo', gh_repo_flag) for git remote 'origin'."""
    try:
        remotes = list_remotes(root)
    except Exception:
        return None, None
    origin_url = remotes.get("origin")
    repo_id = parse_repo_identity(origin_url)
    if not repo_id:
        return None, None
    parts = repo_id.split("/")
    if len(parts) != 3:
        return None, None
    host, owner, repo = parts
    gh_arg = f"{owner}/{repo}" if host == "github.com" else f"{host}/{owner}/{repo}"
    return repo_id, gh_arg


def _run_gh(
    args: list[str],
    *,
    cwd: Path,
    gh_repo: str | None = None,
    timeout: int = _GH_TIMEOUT_SEC,
) -> subprocess.CompletedProcess[str]:
    if not shutil.which("gh"):
        raise RuntimeError("gh CLI is required for PR watch operations")
    full_args = list(args)
    if gh_repo and full_args and full_args[0] in ("pr", "issue", "label", "run"):
        full_args.extend(["-R", gh_repo])
    try:
        return subprocess.run(
            ["gh", *full_args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"gh {' '.join(args[:3])} timed out after {error.timeout}s"
        ) from error


def _post_pr_comment(
    pr_number: int, body: str, *, cwd: Path, gh_repo: str | None = None
) -> str | None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        body_path = Path(tmp_dir) / "pr-comment.md"
        body_path.write_text(body, encoding="utf-8")
        proc = _run_gh(
            ["pr", "comment", str(pr_number), "--body-file", str(body_path)],
            cwd=cwd,
            gh_repo=gh_repo,
        )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _ensure_pr_label(lbl: str, *, cwd: Path, gh_repo: str | None = None) -> None:
    color, desc = MANAGED_LABEL_SPECS.get(lbl, ("ededed", "Managed by Software Garden"))
    _run_gh(
        ["label", "create", lbl, "--color", color, "--description", desc],
        cwd=cwd,
        gh_repo=gh_repo,
    )


def _fetch_pr_attached_labels(
    pr_number: int, *, cwd: Path, gh_repo: str | None = None
) -> set[str] | None:
    proc = _run_gh(
        ["issue", "view", str(pr_number), "--json", "labels"],
        cwd=cwd,
        gh_repo=gh_repo,
    )
    if proc.returncode != 0:
        return None
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return {
        str(item.get("name"))
        for item in (parsed.get("labels") or [])
        if isinstance(item, dict) and item.get("name")
    }


def _set_pr_managed_label(
    pr_number: int,
    target_label: str | None,
    *,
    cwd: Path,
    gh_repo: str | None = None,
) -> dict[str, Any]:
    """Transition PR-watch labels (`garden:pr-ready`, `garden:needs-human`, `garden:running`).

    Intentionally never adds or removes `garden:harvest-ready`, which is reserved
    for Garden's spec-verified harvest reports.
    """
    add = [target_label] if target_label else []
    remove = [lbl for lbl in PR_WATCH_MANAGED_LABELS if lbl not in add]
    warnings: list[str] = []
    added: list[str] = []
    removed: list[str] = []

    for lbl in add:
        _ensure_pr_label(lbl, cwd=cwd, gh_repo=gh_repo)

    add_failed = False
    if add:
        args = ["issue", "edit", str(pr_number)]
        for lbl in add:
            args.extend(["--add-label", lbl])
        proc = _run_gh(args, cwd=cwd, gh_repo=gh_repo)
        if proc.returncode == 0:
            added = list(add)
        else:
            add_failed = True
            warnings.append(proc.stderr.strip() or "add-label failed")

    if remove and not add_failed:
        existing = _fetch_pr_attached_labels(pr_number, cwd=cwd, gh_repo=gh_repo)
        to_remove = (
            [lbl for lbl in remove if lbl in existing]
            if existing is not None
            else remove
        )
        if to_remove:
            args = ["issue", "edit", str(pr_number)]
            for lbl in to_remove:
                args.extend(["--remove-label", lbl])
            proc = _run_gh(args, cwd=cwd, gh_repo=gh_repo)
            if proc.returncode == 0:
                removed = to_remove
            else:
                warnings.append(proc.stderr.strip() or "remove-label failed")

    return {
        "added_labels": added,
        "removed_labels": removed,
        "label_warning": "; ".join(warnings) if warnings else None,
    }


def _fetch_user_repo_permission(
    owner: str, repo: str, login: str, *, cwd: Path, cache: dict[str, str | None]
) -> str | None:
    key = login.strip().lower()
    if key in cache:
        return cache[key]
    try:
        proc = _run_gh(
            [
                "api",
                f"repos/{owner}/{repo}/collaborators/{login}/permission",
                "--jq",
                ".permission",
            ],
            cwd=cwd,
            timeout=15,
        )
    except RuntimeError:
        cache[key] = None
        return None
    perm = proc.stdout.strip().lower() if proc.returncode == 0 else None
    cache[key] = perm or None
    return cache[key]


def _author_login_lower(author_raw: Any) -> str:
    if isinstance(author_raw, dict):
        return str(author_raw.get("login") or "").strip().lower()
    return str(author_raw or "").strip().lower()


def _is_trusted_reviewer(
    author_raw: Any,
    author_association: str,
    *,
    trusted_bots: frozenset[str],
    trusted_humans: frozenset[str] | None = None,
) -> bool:
    """Allow only explicitly trusted review bots, repo OWNER, or verified write-access humans."""
    login = _author_login_lower(author_raw)
    if not login:
        return False
    if login in trusted_bots or f"{login}[bot]" in trusted_bots:
        return True
    if _is_bot_author(author_raw):
        return False
    if trusted_humans is not None and login in trusted_humans:
        return True
    return author_association.strip().upper() == "OWNER"


def _parse_status_rollup(raw_rollup: Any) -> list[PrCheckItem]:
    items: list[PrCheckItem] = []
    if not isinstance(raw_rollup, list):
        return items
    for entry in raw_rollup:
        if not isinstance(entry, dict):
            continue
        typename = str(entry.get("__typename") or "")
        if typename == "StatusContext" or ("context" in entry and "state" in entry):
            name = str(entry.get("context") or "status").strip()
            state = str(entry.get("state") or "PENDING").strip().upper()
            url = str(entry.get("targetUrl") or "").strip()
            if state in _PENDING_CHECK_STATUSES or state == "EXPECTED":
                status = "pending"
            elif state in _FAILED_CHECK_CONCLUSIONS:
                status = "failed"
            else:
                status = "passed"
            items.append(
                PrCheckItem(name=name, status=status, conclusion=state, details_url=url)
            )
            continue

        name = str(entry.get("name") or "check").strip()
        raw_status = str(entry.get("status") or "").strip().upper()
        conclusion = str(entry.get("conclusion") or "").strip().upper()
        url = str(entry.get("detailsUrl") or "").strip()
        if raw_status in _PENDING_CHECK_STATUSES or (
            raw_status != "COMPLETED" and not conclusion
        ):
            status = "pending"
            concl = raw_status or "PENDING"
        elif conclusion in _FAILED_CHECK_CONCLUSIONS:
            status = "failed"
            concl = conclusion
        else:
            status = "passed"
            concl = conclusion or "SUCCESS"
        items.append(
            PrCheckItem(name=name, status=status, conclusion=concl, details_url=url)
        )
    return items


def _fetch_failed_check_log(
    details_url: str, *, cwd: Path, gh_repo: str | None = None
) -> str:
    match = _ACTIONS_RUN_ID_RE.search(details_url or "")
    if not match:
        return ""
    run_id = match.group(1)
    try:
        proc = _run_gh(
            ["run", "view", run_id, "--log-failed"],
            cwd=cwd,
            gh_repo=gh_repo,
            timeout=20,
        )
    except RuntimeError:
        return ""
    if proc.returncode != 0 or not proc.stdout.strip():
        return ""
    text = proc.stdout.strip()
    if len(text) > _MAX_LOG_EXCERPT_CHARS:
        return "...(truncated)...\n" + text[-_MAX_LOG_EXCERPT_CHARS:]
    return text


def _fetch_more_thread_comments(
    thread_id: str, start_cursor: str, *, cwd: Path
) -> tuple[list[dict[str, Any]], bool]:
    query = """
query($id: ID!, $after: String) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $after) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          body
          createdAt
          authorAssociation
          author {
            login
            __typename
          }
        }
      }
    }
  }
}
"""
    extra: list[dict[str, Any]] = []
    cursor: str | None = start_cursor
    for _ in range(20):
        args = ["api", "graphql", "-f", f"query={query}", "-f", f"id={thread_id}"]
        if cursor:
            args.extend(["-f", f"after={cursor}"])
        try:
            proc = _run_gh(args, cwd=cwd)
        except RuntimeError:
            return extra, False
        if proc.returncode != 0:
            return extra, False
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return extra, False
        if payload.get("errors"):
            return extra, False
        comments_conn = ((payload.get("data") or {}).get("node") or {}).get("comments") or {}
        nodes = comments_conn.get("nodes") or []
        extra.extend(n for n in nodes if isinstance(n, dict))
        page_info = comments_conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return extra, True
        cursor = page_info.get("endCursor")
        if not cursor:
            return extra, False
    return extra, False


def _fetch_review_threads_graphql(
    owner: str, repo: str, pr_number: int, *, cwd: Path
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Fetch ALL review threads & replies with full cursor pagination.

    Returns `(threads, complete, error_message)`. Never silently truncates or
    masquerades GraphQL failures as zero unresolved threads.
    """
    query = """
query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 100) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              id
              body
              createdAt
              authorAssociation
              author {
                login
                __typename
              }
            }
          }
        }
      }
    }
  }
}
"""
    all_threads: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(20):
        args = [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"repo={repo}",
            "-F",
            f"number={pr_number}",
        ]
        if cursor:
            args.extend(["-f", f"after={cursor}"])
        try:
            proc = _run_gh(args, cwd=cwd)
        except RuntimeError as error:
            return all_threads, False, str(error)
        if proc.returncode != 0:
            return (
                all_threads,
                False,
                proc.stderr.strip() or proc.stdout.strip() or "graphql reviewThreads failed",
            )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as error:
            return all_threads, False, f"invalid graphql JSON: {error}"
        if payload.get("errors"):
            return all_threads, False, f"graphql errors: {payload['errors']}"

        pr_node = (
            (payload.get("data") or {}).get("repository") or {}
        ).get("pullRequest")
        if not isinstance(pr_node, dict):
            return all_threads, False, "graphql response missing pullRequest node"
        conn = pr_node.get("reviewThreads")
        if not isinstance(conn, dict):
            return all_threads, False, "graphql response missing reviewThreads connection"

        nodes = conn.get("nodes") or []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            comments_conn = node.get("comments") or {}
            c_page = comments_conn.get("pageInfo") or {}
            if c_page.get("hasNextPage"):
                tid = str(node.get("id") or "")
                c_cursor = str(c_page.get("endCursor") or "")
                if not tid or not c_cursor:
                    return all_threads, False, f"incomplete comment pagination for thread {tid}"
                extra_comments, c_ok = _fetch_more_thread_comments(tid, c_cursor, cwd=cwd)
                if not c_ok:
                    return all_threads, False, f"failed to paginate comments for thread {tid}"
                merged_nodes = list(comments_conn.get("nodes") or []) + extra_comments
                node = dict(node)
                node["comments"] = {"nodes": merged_nodes}
            all_threads.append(node)

        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return all_threads, True, None
        cursor = page_info.get("endCursor")
        if not cursor:
            return all_threads, False, "graphql reviewThreads hasNextPage without endCursor"

    return all_threads, False, "reviewThreads pagination exceeded page limit"


def _detect_review_bot_pending(raw_comments: Any, head_oid: str) -> bool:
    """Detect active bot reviews strictly bound to verified bot authors and current HEAD."""
    if not isinstance(raw_comments, list):
        return False
    for cmt in raw_comments:
        if not isinstance(cmt, dict):
            continue
        login = _author_login_lower(cmt.get("author"))
        body = str(cmt.get("body") or "")
        if login in _CODERABBIT_BOT_LOGINS:
            if "review in progress by coderabbit.ai" in body.lower():
                return True
        if login in _CODEX_BOT_LOGINS:
            for match in _CODEX_STATE_RE.finditer(body):
                try:
                    state_obj = json.loads(match.group(1))
                except json.JSONDecodeError:
                    continue
                if not isinstance(state_obj, dict):
                    continue
                for section in state_obj.values():
                    if not isinstance(section, dict):
                        continue
                    status = str(section.get("status") or "").strip().lower()
                    commit_sha = section.get("commit_sha")
                    if status in ("running", "in_progress", "pending", "queued"):
                        if not commit_sha or not head_oid or str(commit_sha) == head_oid:
                            return True
    return False


def build_pr_snapshot(
    pr_view_data: dict[str, Any],
    *,
    review_threads: list[dict[str, Any]] | None = None,
    review_threads_complete: bool = True,
    review_threads_error: str | None = None,
    trusted_bots: frozenset[str] = DEFAULT_TRUSTED_REVIEW_BOTS,
    trusted_humans: frozenset[str] | None = None,
    failed_logs_by_url: dict[str, str] | None = None,
) -> PrSnapshot:
    """Build a normalized PrSnapshot from raw GitHub PR JSON + GraphQL review threads."""
    number = int(pr_view_data.get("number") or 0)
    url = str(pr_view_data.get("url") or "").strip()
    state = str(pr_view_data.get("state") or "OPEN").strip().upper()
    is_draft = bool(pr_view_data.get("isDraft"))
    is_cross_repo = bool(pr_view_data.get("isCrossRepository"))
    mergeable = str(pr_view_data.get("mergeable") or "UNKNOWN").strip().upper()
    merge_state = str(pr_view_data.get("mergeStateStatus") or "UNKNOWN").strip().upper()
    review_decision = str(pr_view_data.get("reviewDecision") or "").strip().upper()
    head_ref = str(pr_view_data.get("headRefName") or "").strip()
    base_ref = str(pr_view_data.get("baseRefName") or "main").strip()
    head_oid = str(pr_view_data.get("headRefOid") or "").strip()

    raw_checks = _parse_status_rollup(pr_view_data.get("statusCheckRollup"))
    logs_map = failed_logs_by_url or {}
    checks: list[PrCheckItem] = []
    for chk in raw_checks:
        excerpt = logs_map.get(chk.details_url, "") if chk.status == "failed" else ""
        checks.append(
            PrCheckItem(
                name=chk.name,
                status=chk.status,
                conclusion=chk.conclusion,
                details_url=chk.details_url,
                log_excerpt=excerpt,
            )
        )

    findings: list[PrReviewFinding] = []
    seen_ids: set[str] = set()

    for thread in review_threads or []:
        if thread.get("isResolved") is True or thread.get("isOutdated") is True:
            continue
        comments_nodes = (thread.get("comments") or {}).get("nodes") or []
        if not isinstance(comments_nodes, list) or not comments_nodes:
            continue
        first_comment = comments_nodes[0]
        if not isinstance(first_comment, dict):
            continue
        author_raw = first_comment.get("author")
        assoc = str(first_comment.get("authorAssociation") or "").strip().upper()
        if not _is_trusted_reviewer(
            author_raw, assoc, trusted_bots=trusted_bots, trusted_humans=trusted_humans
        ):
            continue
        body = str(first_comment.get("body") or "").strip()
        if not body or parse_garden_markers(body):
            continue
        reply_texts: list[str] = [body]
        for reply in comments_nodes[1:]:
            if not isinstance(reply, dict):
                continue
            r_assoc = str(reply.get("authorAssociation") or "").strip().upper()
            if not _is_trusted_reviewer(
                reply.get("author"),
                r_assoc,
                trusted_bots=trusted_bots,
                trusted_humans=trusted_humans,
            ):
                continue
            r_body = str(reply.get("body") or "").strip()
            if r_body and not parse_garden_markers(r_body):
                r_login = _author_login_lower(reply.get("author")) or "reviewer"
                reply_texts.append(f"[Follow-up by {r_login}]: {r_body}")

        fid = str(thread.get("id") or first_comment.get("id") or f"thread-{len(findings)}")
        if fid in seen_ids:
            continue
        seen_ids.add(fid)
        author_login = _author_login_lower(author_raw) or "reviewer"
        raw_line = thread.get("line")
        line_num = (
            int(raw_line)
            if isinstance(raw_line, int) and not isinstance(raw_line, bool)
            else None
        )
        findings.append(
            PrReviewFinding(
                finding_id=fid,
                author=author_login,
                author_association=assoc,
                path=str(thread.get("path") or "").strip(),
                line=line_num,
                body="\n\n".join(reply_texts),
                created_at=str(first_comment.get("createdAt") or "").strip(),
                source="thread",
            )
        )

    raw_reviews = pr_view_data.get("reviews") or []
    if isinstance(raw_reviews, list):
        latest_by_author: dict[str, dict[str, Any]] = {}
        for rev in raw_reviews:
            if not isinstance(rev, dict):
                continue
            author_raw = rev.get("author")
            assoc = str(rev.get("authorAssociation") or "").strip().upper()
            if not _is_trusted_reviewer(
                author_raw, assoc, trusted_bots=trusted_bots, trusted_humans=trusted_humans
            ):
                continue
            login = _author_login_lower(author_raw)
            if not login:
                continue
            latest_by_author[login] = rev

        for login, rev in latest_by_author.items():
            if str(rev.get("state") or "").strip().upper() != "CHANGES_REQUESTED":
                continue
            body = str(rev.get("body") or "").strip()
            if not body or parse_garden_markers(body):
                continue
            fid = str(rev.get("id") or f"review-{login}")
            if fid in seen_ids:
                continue
            seen_ids.add(fid)
            findings.append(
                PrReviewFinding(
                    finding_id=fid,
                    author=login,
                    author_association=str(rev.get("authorAssociation") or "").strip().upper(),
                    body=body,
                    created_at=str(rev.get("submittedAt") or "").strip(),
                    source="review",
                )
            )

    review_pending = _detect_review_bot_pending(pr_view_data.get("comments"), head_oid)

    return PrSnapshot(
        number=number,
        url=url,
        state=state,
        is_draft=is_draft,
        is_cross_repository=is_cross_repo,
        mergeable=mergeable,
        merge_state_status=merge_state,
        review_decision=review_decision,
        head_ref_name=head_ref,
        base_ref_name=base_ref,
        head_ref_oid=head_oid,
        checks=tuple(checks),
        findings=tuple(findings),
        review_pending=review_pending,
        review_threads_complete=review_threads_complete,
        review_threads_error=review_threads_error,
    )


def fetch_pr_snapshot(
    pr_number: int,
    *,
    cwd: Path,
    gh_repo: str | None = None,
    trusted_bots: frozenset[str] = DEFAULT_TRUSTED_REVIEW_BOTS,
    trusted_reviewers: frozenset[str] = frozenset(),
    permission_cache: dict[str, str | None] | None = None,
    fetch_failed_logs: bool = True,
) -> PrSnapshot:
    """Query GitHub for the current PR state, status checks, and paginated review threads."""
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError(f"pr_number must be a positive integer, got {pr_number!r}")

    proc = _run_gh(
        [
            "pr",
            "view",
            str(pr_number),
            "--json",
            "number,url,state,isDraft,isCrossRepository,mergeable,mergeStateStatus,"
            "reviewDecision,headRefName,baseRefName,headRefOid,statusCheckRollup,reviews,comments",
        ],
        cwd=cwd,
        gh_repo=gh_repo,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh pr view #{pr_number} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    try:
        pr_data = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"gh pr view #{pr_number} returned invalid JSON: {error}") from error
    if not isinstance(pr_data, dict):
        raise RuntimeError(f"gh pr view #{pr_number} returned non-object JSON")

    repo_id = parse_repo_identity(pr_data.get("url"))
    threads: list[dict[str, Any]] = []
    threads_complete = False
    threads_error: str | None = None
    owner_repo: tuple[str, str] | None = None
    if repo_id:
        parts = repo_id.split("/")
        if len(parts) == 3:
            owner_repo = (parts[1], parts[2])
            threads, threads_complete, threads_error = _fetch_review_threads_graphql(
                parts[1], parts[2], pr_number, cwd=cwd
            )
    else:
        threads_error = "cannot determine repository owner/name from PR URL"

    # Resolve effective write/maintain/admin permissions for candidate human reviewers
    verified_humans: set[str] = set(trusted_reviewers)
    perm_cache = permission_cache if permission_cache is not None else {}
    if owner_repo is not None:
        candidate_logins: set[str] = set()
        for thread in threads:
            if thread.get("isResolved") is True or thread.get("isOutdated") is True:
                continue
            for node in (thread.get("comments") or {}).get("nodes") or []:
                if not isinstance(node, dict):
                    continue
                author_raw = node.get("author")
                login = _author_login_lower(author_raw)
                assoc = str(node.get("authorAssociation") or "").strip().upper()
                if (
                    login
                    and assoc != "OWNER"
                    and login not in verified_humans
                    and login not in trusted_bots
                    and f"{login}[bot]" not in trusted_bots
                    and not _is_bot_author(author_raw)
                ):
                    candidate_logins.add(login)
        for rev in pr_data.get("reviews") or []:
            if not isinstance(rev, dict):
                continue
            author_raw = rev.get("author")
            login = _author_login_lower(author_raw)
            assoc = str(rev.get("authorAssociation") or "").strip().upper()
            if (
                login
                and assoc != "OWNER"
                and login not in verified_humans
                and login not in trusted_bots
                and f"{login}[bot]" not in trusted_bots
                and not _is_bot_author(author_raw)
            ):
                candidate_logins.add(login)

        for login in sorted(candidate_logins):
            perm = _fetch_user_repo_permission(
                owner_repo[0], owner_repo[1], login, cwd=cwd, cache=perm_cache
            )
            if perm in _WRITE_PERMISSIONS:
                verified_humans.add(login)

    failed_logs: dict[str, str] = {}
    if fetch_failed_logs:
        for chk in _parse_status_rollup(pr_data.get("statusCheckRollup")):
            if chk.status == "failed" and chk.details_url and chk.details_url not in failed_logs:
                failed_logs[chk.details_url] = _fetch_failed_check_log(
                    chk.details_url, cwd=cwd, gh_repo=gh_repo
                )

    return build_pr_snapshot(
        pr_data,
        review_threads=threads,
        review_threads_complete=threads_complete,
        review_threads_error=threads_error,
        trusted_bots=trusted_bots,
        trusted_humans=frozenset(verified_humans),
        failed_logs_by_url=failed_logs,
    )


def classify_pr_snapshot(snapshot: PrSnapshot) -> dict[str, Any]:
    """Deterministically classify a PrSnapshot into an action state.

    Status values:
      - merged: PR is already merged
      - closed: PR is closed without merge
      - needs_human: PR is draft, cross-repository, or blocked by branch/review policy
      - inconclusive: review threads could not be completely retrieved (never merge_ready)
      - waiting: checks or review bots still running, or GitHub mergeability UNKNOWN
      - needs_fix: failed checks, merge conflict, behind base, or unresolved actionable reviews
      - merge_ready: MERGEABLE, mergeStateStatus CLEAN/HAS_HOOKS, all checks green,
                     complete review thread inspection with 0 unresolved findings
    """
    if snapshot.state == "MERGED":
        return {"status": "merged", "reasons": []}
    if snapshot.state != "OPEN":
        return {"status": "closed", "reasons": [f"pr_state_{snapshot.state.lower()}"]}
    if snapshot.is_cross_repository:
        return {"status": "needs_human", "reasons": ["cross_repository_pr_disallowed"]}
    if snapshot.is_draft:
        return {"status": "needs_human", "reasons": ["pr_is_draft"]}

    pending_checks = [c for c in snapshot.checks if c.status == "pending"]
    failed_checks = [c for c in snapshot.checks if c.status == "failed"]
    has_conflict = (
        snapshot.mergeable == "CONFLICTING" or snapshot.merge_state_status == "DIRTY"
    )
    is_behind = snapshot.merge_state_status == "BEHIND"
    mergeable_unknown = (
        snapshot.mergeable == "UNKNOWN" or snapshot.merge_state_status == "UNKNOWN"
    ) and not has_conflict

    if pending_checks or snapshot.review_pending or mergeable_unknown:
        reasons: list[str] = []
        if pending_checks:
            reasons.append(f"pending_checks:{len(pending_checks)}")
        if snapshot.review_pending:
            reasons.append("review_in_progress")
        if mergeable_unknown:
            reasons.append("mergeable_unknown")
        return {"status": "waiting", "reasons": reasons}

    fix_reasons: list[str] = []
    if has_conflict:
        fix_reasons.append("merge_conflict")
    if is_behind:
        fix_reasons.append("behind_base_branch")
    if failed_checks:
        fix_reasons.append(f"failed_checks:{len(failed_checks)}")
    if snapshot.findings:
        fix_reasons.append(f"unresolved_reviews:{len(snapshot.findings)}")

    if fix_reasons:
        return {"status": "needs_fix", "reasons": fix_reasons}

    if not snapshot.review_threads_complete:
        return {
            "status": "inconclusive",
            "reasons": ["review_threads_incomplete"],
        }

    if snapshot.review_decision == "CHANGES_REQUESTED":
        return {"status": "needs_human", "reasons": ["review_changes_requested"]}
    if snapshot.review_decision == "REVIEW_REQUIRED":
        return {"status": "needs_human", "reasons": ["review_required"]}

    if (
        snapshot.mergeable != "MERGEABLE"
        or snapshot.merge_state_status not in _MERGE_READY_STATES
    ):
        return {
            "status": "needs_human",
            "reasons": [
                f"unmergeable_state:{snapshot.mergeable}:{snapshot.merge_state_status}"
            ],
        }

    return {"status": "merge_ready", "reasons": []}


def _select_flow_and_agent(
    definition: GardenDefinition,
    *,
    flow_name: str | None,
    agent_name: str | None,
) -> tuple[str, FlowConfig, AgentConfig]:
    if flow_name:
        if flow_name not in definition.flows:
            raise ValueError(
                f"unknown flow '{flow_name}'; available: {', '.join(sorted(definition.flows))}"
            )
        chosen_flow_name = flow_name
    else:
        ranked = sorted(
            definition.flows.items(),
            key=lambda kv: (
                0 if kv[1].delegation.mode != "observe_only" else 1,
                0 if kv[1].scope == "all_specs" else 1,
                kv[0],
            ),
        )
        chosen_flow_name = ranked[0][0]

    flow_cfg = definition.flows[chosen_flow_name]

    if agent_name:
        if agent_name not in definition.agents:
            raise ValueError(
                f"unknown agent '{agent_name}'; available: {', '.join(sorted(definition.agents))}"
            )
        chosen_agent = definition.agents[agent_name]
    else:
        flow_agents = [
            definition.agents[s.agent]
            for s in flow_cfg.steps
            if s.type == "agent" and s.agent and s.agent in definition.agents
        ]
        if flow_agents:
            chosen_agent = flow_agents[0]
        elif definition.agents:
            chosen_agent = definition.agents[sorted(definition.agents.keys())[0]]
        else:
            raise ValueError(
                "no [agents.<name>] configured in .garden/garden.toml for pr-watch fixes"
            )

    return chosen_flow_name, flow_cfg, chosen_agent


def _checks_to_verify_before_push(
    definition: GardenDefinition,
    flow: FlowConfig,
    delegation: DelegationConfig,
) -> list[CheckConfig]:
    names = list(delegation.require_checks)
    for cname in sorted(_referenced_active_checks(definition, flow)):
        if cname not in names:
            names.append(cname)
    if not names:
        for cname, cfg in definition.checks.items():
            if cfg.status == "active":
                names.append(cname)
    return [definition.checks[n] for n in names if n in definition.checks]


def _list_unmerged_files(worktree: Path) -> list[str]:
    """Read unmerged paths using NUL-delimited Git output so non-ASCII names are never escaped."""
    proc = git_run(worktree, "diff", "--name-only", "--diff-filter=U", "-z", text=False)
    if proc.returncode != 0 or not proc.stdout:
        return []
    return [
        entry.decode("utf-8", errors="replace")
        for entry in proc.stdout.split(b"\x00")
        if entry
    ]


def _has_conflict_markers(file_path: Path) -> bool:
    if not file_path.exists():
        # Allow intentional deletion as a valid resolution of modify/delete conflicts
        return False
    if not file_path.is_file():
        return True
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return _CONFLICT_MARKER_RE.search(content) is not None


def _hash_worktree_entry(path: Path) -> str | None:
    if path.is_symlink():
        return "symlink:" + hashlib.sha256(str(path.readlink()).encode("utf-8")).hexdigest()
    if path.is_file():
        return "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return None


def _list_diff_paths_z(worktree: Path, *diff_args: str) -> list[str]:
    proc = git_run(worktree, "diff", "--name-only", "--no-renames", "-z", *diff_args, text=False)
    if proc.returncode != 0 or not proc.stdout:
        return []
    return [
        entry.decode("utf-8", errors="replace")
        for entry in proc.stdout.split(b"\x00")
        if entry
    ]


def _build_scrubbed_env(
    *, kind: str, adapter: str | None = None
) -> dict[str, str]:
    """Strip host credentials and tokens before running untrusted PR-branch code."""
    allowed_adapter_keys = set(
        _ADAPTER_ALLOWED_ENV_KEYS.get(adapter or "", ()) if kind == "agent" else ()
    )
    scrubbed: dict[str, str] = {}
    for key, val in os.environ.items():
        upper = key.upper()
        if key in allowed_adapter_keys:
            scrubbed[key] = val
            continue
        if upper in _SENSITIVE_ENV_EXACT:
            continue
        if any(upper.startswith(pref) for pref in _SENSITIVE_ENV_PREFIXES):
            continue
        if any(sub in upper for sub in _SENSITIVE_ENV_SUBSTRINGS):
            continue
        scrubbed[key] = val
    return scrubbed


def _seatbelt_escape(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


_BWRAP_OK_CACHE: bool | None = None


def _bwrap_usable() -> bool:
    """Probe once whether bubblewrap can create user namespaces in this kernel/AppArmor environment."""
    global _BWRAP_OK_CACHE
    if _BWRAP_OK_CACHE is not None:
        return _BWRAP_OK_CACHE
    if platform.system() != "Linux" or not shutil.which("bwrap"):
        _BWRAP_OK_CACHE = False
        return False
    try:
        probe = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--", "/bin/true"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        _BWRAP_OK_CACHE = probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        _BWRAP_OK_CACHE = False
    return _BWRAP_OK_CACHE


def _resolve_pr_sandbox_prefix(
    worktree: Path,
    step_dir: Path,
    *,
    allow_network: bool,
    adapter: str | None = None,
    sandbox_cmd: tuple[str, ...] = (),
    trust_pr_code: bool = False,
) -> tuple[str, ...] | None:
    """Resolve an OS/custom sandbox command prefix for executing PR-branch code.

    Returns a prefix tuple (possibly empty ONLY when `trust_pr_code=True`), or
    None when no sandbox is available and `trust_pr_code` is False (fail-closed).
    """
    if sandbox_cmd:
        return tuple(sandbox_cmd)

    wt_res = worktree.resolve()
    step_res = step_dir.resolve()
    home_res = Path.home().resolve()

    if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
        write_allow_paths = [
            wt_res,
            step_res,
            Path("/tmp"),
            Path("/private/tmp"),
            Path("/var/folders"),
            Path("/private/var/folders"),
        ]
        git_file = wt_res / ".git"
        if git_file.is_file():
            try:
                raw = git_file.read_text(encoding="utf-8").strip()
                if raw.startswith("gitdir:"):
                    gd = Path(raw.split(":", 1)[1].strip())
                    if not gd.is_absolute():
                        gd = (wt_res / gd).resolve()
                    write_allow_paths.append(gd)
            except OSError:
                pass
        if allow_network and adapter:
            for rel in _ADAPTER_HOME_WRITE_SUBDIRS.get(adapter, ()):
                write_allow_paths.append(home_res / rel)

        allow_clauses = " ".join(
            f'(subpath "{_seatbelt_escape(p)}")' for p in write_allow_paths
        )
        deny_clauses = " ".join(
            f'(subpath "{_seatbelt_escape(home_res / rel)}")'
            for rel in _DENIED_HOME_SUBDIRS
        )
        net_rule = "" if allow_network else "(deny network*)\n"
        profile = (
            "(version 1)\n"
            "(allow default)\n"
            f"{net_rule}"
            "(deny file-write*)\n"
            f'(allow file-write* {allow_clauses} (literal "/dev/null"))\n'
            f"(deny file-read* {deny_clauses})\n"
        )
        return ("sandbox-exec", "-p", profile)

    if _bwrap_usable():
        # Mount --tmpfs /tmp BEFORE --bind worktree/step_dir so paths inside /tmp stay visible!
        cmd = [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--bind",
            str(wt_res),
            str(wt_res),
            "--bind",
            str(step_res),
            str(step_res),
        ]
        if not allow_network:
            cmd.append("--unshare-net")
        for rel in _DENIED_HOME_SUBDIRS:
            target = home_res / rel
            if target.is_dir():
                cmd.extend(["--tmpfs", str(target)])
        if allow_network and adapter:
            for rel in _ADAPTER_HOME_WRITE_SUBDIRS.get(adapter, ()):
                target = home_res / rel
                if target.exists():
                    cmd.extend(["--bind", str(target), str(target)])
        cmd.append("--")
        return tuple(cmd)

    if trust_pr_code:
        return ()

    return None


def _run_pr_fix_round(
    root: Path,
    snapshot: PrSnapshot,
    *,
    round_num: int,
    flow_name: str | None,
    agent_name: str | None,
    approval_branch: str,
    cumulative_cost_known: float,
    unaddressed_findings: list[PrReviewFinding],
    previous_local_failure: dict[str, Any] | None = None,
    sandbox_cmd: tuple[str, ...] = (),
    trust_pr_code: bool = False,
) -> dict[str, Any]:
    """Execute one isolated, sandboxed, commit-before-verify fix round for a PR."""
    round_started_mono = time.monotonic()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    round_dir = root / ".garden" / "runs" / f"pr-watch-{snapshot.number}-{stamp}-r{round_num}"
    round_dir.mkdir(parents=True, exist_ok=True)
    wt_root = root / ".garden" / "worktrees"
    wt_root.mkdir(parents=True, exist_ok=True)
    wt_path = wt_root / f"pr-{snapshot.number}-{stamp}-r{round_num}"
    verify_wt_path = wt_root / f"pr-verify-{snapshot.number}-{stamp}-r{round_num}"

    # 1) Fetch PR head, base branch, AND approval_branch from origin (require approval_branch fetch!)
    fetch_approval = git_run(
        root,
        "fetch",
        "origin",
        f"+refs/heads/{approval_branch}:refs/remotes/origin/{approval_branch}",
    )
    if fetch_approval.returncode != 0:
        return {
            "outcome": "needs_human",
            "reason": "approval_branch_fetch_failed",
            "detail": fetch_approval.stderr.strip() or fetch_approval.stdout.strip(),
            "pushed": False,
            "cost_usd": 0.0,
        }

    fetch_head = git_run(
        root,
        "fetch",
        "origin",
        f"+refs/heads/{snapshot.head_ref_name}:refs/remotes/origin/{snapshot.head_ref_name}",
    )
    if fetch_head.returncode != 0:
        return {
            "outcome": "needs_human",
            "reason": "git_fetch_head_failed",
            "detail": fetch_head.stderr.strip() or fetch_head.stdout.strip(),
            "pushed": False,
            "cost_usd": 0.0,
        }

    if snapshot.base_ref_name != approval_branch:
        fetch_base = git_run(
            root,
            "fetch",
            "origin",
            f"+refs/heads/{snapshot.base_ref_name}:refs/remotes/origin/{snapshot.base_ref_name}",
        )
        if fetch_base.returncode != 0:
            return {
                "outcome": "needs_human",
                "reason": "git_fetch_base_failed",
                "detail": fetch_base.stderr.strip() or fetch_base.stdout.strip(),
                "pushed": False,
                "cost_usd": 0.0,
            }

    add_wt = git_run(
        root, "worktree", "add", "--detach", str(wt_path), snapshot.head_ref_oid
    )
    if add_wt.returncode != 0:
        add_wt = git_run(
            root,
            "worktree",
            "add",
            "--detach",
            str(wt_path),
            f"origin/{snapshot.head_ref_name}",
        )
    if add_wt.returncode != 0:
        return {
            "outcome": "needs_human",
            "reason": "worktree_create_failed",
            "detail": add_wt.stderr.strip() or add_wt.stdout.strip(),
            "pushed": False,
            "cost_usd": 0.0,
        }

    try:
        actual_head = head_commit(wt_path)
        if snapshot.head_ref_oid and actual_head != snapshot.head_ref_oid:
            return {
                "outcome": "waiting",
                "reason": "pr_head_moved_concurrently",
                "detail": f"expected {snapshot.head_ref_oid[:12]}, remote is now {actual_head[:12]}",
                "pushed": False,
                "cost_usd": 0.0,
            }

        # Verify Trust Anchor strictly against freshly fetched origin/<approval_branch>
        anchor = verify_trust_anchor(
            wt_path, approval_branch=approval_branch, prefer_remote=True
        )
        if not anchor.specs_approved:
            return {
                "outcome": "needs_human",
                "reason": "spec_not_approved",
                "detail": anchor.spec_failure_reason or "approved specs unavailable",
                "pushed": False,
                "cost_usd": 0.0,
            }

        definition = anchor.active_definition
        chosen_flow_name, flow_cfg, agent_cfg = _select_flow_and_agent(
            definition, flow_name=flow_name, agent_name=agent_name
        )
        eff_delegation = _apply_effective_delegation(flow_cfg.delegation, anchor)

        if anchor.evaluation_criteria_changed:
            return {
                "outcome": "needs_human",
                "reason": "evaluation_criteria_changed_in_pr",
                "detail": (
                    "PR modifies .garden/garden.toml or approved specs; "
                    "automated PR fixes are disabled for evaluation-criteria changes"
                ),
                "pushed": False,
                "cost_usd": 0.0,
            }

        if eff_delegation.mode == "observe_only":
            return {
                "outcome": "needs_human",
                "reason": "observe_only_fix_disallowed",
                "detail": (
                    f"flow '{chosen_flow_name}' effective delegation mode is observe_only; "
                    "automated PR fixes require propose_pr (or higher)"
                ),
                "pushed": False,
                "cost_usd": 0.0,
            }

        # Fail-closed sandbox check BEFORE executing any PR-branch agent or check!
        agent_step_dir = round_dir / "agent"
        agent_step_dir.mkdir(parents=True, exist_ok=True)
        agent_sandbox = _resolve_pr_sandbox_prefix(
            wt_path,
            agent_step_dir,
            allow_network=agent_cfg.adapter != "mock",
            adapter=agent_cfg.adapter,
            sandbox_cmd=sandbox_cmd,
            trust_pr_code=trust_pr_code,
        )
        if agent_sandbox is None:
            return {
                "outcome": "needs_human",
                "reason": "pr_sandbox_unavailable",
                "detail": (
                    "no OS sandbox (sandbox-exec/bwrap) or --sandbox-cmd available to isolate PR code; "
                    "configure --sandbox-cmd or pass --trust-pr-code only in an already isolated environment"
                ),
                "pushed": False,
                "cost_usd": 0.0,
            }

        if agent_cfg.adapter != "mock":
            harness_eval = evaluate_harness_requirements(
                wt_path,
                definition,
                enforce_core=True,
                adapters=(agent_cfg.adapter,),
            )
            if harness_eval.has_blocking_missing:
                return {
                    "outcome": "inconclusive",
                    "reason": "harness_requirements_missing",
                    "detail": "; ".join(harness_eval.remedy_hints)
                    or "missing required skills or MCP servers",
                    "pushed": False,
                    "cost_usd": 0.0,
                }

        baseline_untracked = snapshot_untracked_files(wt_path)
        has_conflict = (
            snapshot.mergeable == "CONFLICTING"
            or snapshot.merge_state_status == "DIRTY"
        )
        is_behind = snapshot.merge_state_status == "BEHIND"
        conflict_files: list[str] = []
        merged_base_cleanly = False
        auto_merged_base_hashes: dict[str, str | None] = {}

        if has_conflict or is_behind:
            merge_proc = git_run(
                wt_path,
                "merge",
                "--no-commit",
                "--no-ff",
                f"origin/{snapshot.base_ref_name}",
            )
            if merge_proc.returncode == 0:
                merged_base_cleanly = True
                # Record cleanly auto-merged files from origin/<base> so guard distinguishes them
                for rel in _list_diff_paths_z(wt_path, actual_head):
                    auto_merged_base_hashes[rel] = _hash_worktree_entry(wt_path / rel)
            else:
                conflict_files = _list_unmerged_files(wt_path)
                if not conflict_files:
                    git_run(wt_path, "merge", "--abort")
                    return {
                        "outcome": "needs_human",
                        "reason": "merge_base_failed",
                        "detail": merge_proc.stderr.strip() or merge_proc.stdout.strip(),
                        "pushed": False,
                        "cost_usd": 0.0,
                    }
                conflict_set = set(conflict_files)
                for rel in _list_diff_paths_z(wt_path, actual_head):
                    if rel not in conflict_set:
                        auto_merged_base_hashes[rel] = _hash_worktree_entry(wt_path / rel)

        failed_checks = [c.to_dict() for c in snapshot.checks if c.status == "failed"]
        selected_findings_dicts, _ = select_pr_fix_findings(
            [f.to_dict() for f in unaddressed_findings]
        )
        delivered_finding_versions = [
            str(item["version_key"])
            for item in selected_findings_dicts
            if item.get("version_key")
        ]
        needs_agent = bool(conflict_files or failed_checks or selected_findings_dicts)

        step_cost_usd: float | None = 0.0
        agent_response: AgentResponse | None = None
        if needs_agent:
            remaining_budget = max(0.0, eff_delegation.max_cost_usd - cumulative_cost_known)
            effective_step_cap = (
                min(agent_cfg.max_cost_usd, remaining_budget)
                if agent_cfg.max_cost_usd is not None
                else remaining_budget
            )
            specs_block = [
                {
                    "spec_id": rec.spec_id,
                    "path": rec.path,
                    "version": rec.version,
                    "blob_sha": rec.blob_sha,
                }
                for rec in anchor.specs
            ]
            request = AgentRequest(
                run_id=round_dir.name,
                step_id=f"pr_fix_r{round_num}",
                purpose="pr_fix",
                skill=None,
                worktree_path=str(wt_path.resolve()),
                specs=specs_block,
                delegation={
                    "mode": eff_delegation.mode,
                    "allowed_paths": list(eff_delegation.allowed_paths),
                    "forbidden_paths": list(eff_delegation.forbidden_paths),
                    "max_cost_usd": effective_step_cap,
                },
                context={
                    "pr_fix": {
                        "pr_number": snapshot.number,
                        "pr_url": snapshot.url,
                        "base_branch": snapshot.base_ref_name,
                        "head_branch": snapshot.head_ref_name,
                        "conflict_files": conflict_files,
                        "failed_checks": failed_checks,
                        "review_findings": selected_findings_dicts,
                        "previous_local_failure": previous_local_failure,
                    }
                },
            )
            scrubbed_agent_env = _build_scrubbed_env(
                kind="agent", adapter=agent_cfg.adapter
            )
            if agent_cfg.adapter == "mock":
                agent_response, _ = run_mock_adapter(
                    agent_cfg,
                    request,
                    worktree=wt_path,
                    step_dir=agent_step_dir,
                    env_override=scrubbed_agent_env,
                    command_prefix=agent_sandbox,
                )
            else:
                agent_response, _ = run_command_adapter(
                    agent_cfg,
                    request,
                    worktree=wt_path,
                    step_dir=agent_step_dir,
                    env_override=scrubbed_agent_env,
                    command_prefix=agent_sandbox,
                )
            step_cost_usd = agent_response.usage.cost_usd

            if agent_response.outcome == "needs_spec_decision":
                return {
                    "outcome": "needs_human",
                    "reason": "needs_spec_decision",
                    "detail": agent_response.summary,
                    "question": agent_response.question,
                    "pushed": False,
                    "cost_usd": step_cost_usd,
                }
            if agent_response.outcome != "completed":
                return {
                    "outcome": "needs_human",
                    "reason": f"agent_{agent_response.outcome}",
                    "detail": agent_response.summary,
                    "pushed": False,
                    "cost_usd": step_cost_usd,
                }

        remaining_unmerged = _list_unmerged_files(wt_path)
        still_conflicted = [
            rel for rel in remaining_unmerged if _has_conflict_markers(wt_path / rel)
        ]
        if still_conflicted:
            return {
                "outcome": "needs_human",
                "reason": "unresolved_merge_conflicts",
                "detail": f"unresolved conflict marker(s) in: {', '.join(still_conflicted)}",
                "pushed": False,
                "cost_usd": step_cost_usd,
            }

        # Distinguish cleanly auto-merged base paths untouched by agent from agent edits/resolutions
        untouched_base_paths = frozenset(
            rel
            for rel, expected_hash in auto_merged_base_hashes.items()
            if _hash_worktree_entry(wt_path / rel) == expected_hash
        )

        next_cum_cost = (
            round(cumulative_cost_known + step_cost_usd, 6)
            if step_cost_usd is not None
            else cumulative_cost_known
        )
        guard_res = verify_post_agent_guard(
            wt_path,
            actual_head,
            eff_delegation,
            step_cost_usd=step_cost_usd,
            agent_max_cost_usd=agent_cfg.max_cost_usd,
            cumulative_cost_usd=next_cum_cost,
            allow_unknown_cost=agent_cfg.allow_unknown_cost,
            elapsed_sec=time.monotonic() - round_started_mono,
            baseline_untracked=baseline_untracked,
            ignore_prefixes=(".garden/runs/", ".garden/worktrees/"),
            ignore_paths=untouched_base_paths,
        )
        if not guard_res.ok:
            return {
                "outcome": "needs_human",
                "reason": "guard_violation",
                "detail": "; ".join(guard_res.violations),
                "pushed": False,
                "cost_usd": step_cost_usd,
            }

        if not guard_res.changed_files and not merged_base_cleanly and not remaining_unmerged:
            return {
                "outcome": "needs_human",
                "reason": "no_changes_produced",
                "detail": "fix round produced no file changes to push",
                "pushed": False,
                "cost_usd": step_cost_usd,
            }

        # 2) Freeze the exact candidate commit BEFORE running checks so checks cannot
        #    mutate or stage files into the commit that gets pushed!
        paths_to_stage = list(
            dict.fromkeys([*guard_res.changed_files, *remaining_unmerged])
        )
        if paths_to_stage:
            add_proc = git_run(wt_path, "add", "-A", "--", *paths_to_stage)
            if add_proc.returncode != 0:
                return {
                    "outcome": "needs_human",
                    "reason": "git_add_failed",
                    "detail": add_proc.stderr.strip(),
                    "pushed": False,
                    "cost_usd": step_cost_usd,
                }

        status_proc = git_run(wt_path, "status", "--porcelain")
        if status_proc.stdout.strip() or merged_base_cleanly or remaining_unmerged:
            commit_msg = (
                f"fix(garden): address PR #{snapshot.number} CI/review feedback (round {round_num})"
            )
            commit_proc = git_run(wt_path, "commit", "--no-verify", "-m", commit_msg)
            if commit_proc.returncode != 0:
                return {
                    "outcome": "needs_human",
                    "reason": "git_commit_failed",
                    "detail": commit_proc.stderr.strip() or commit_proc.stdout.strip(),
                    "pushed": False,
                    "cost_usd": step_cost_usd,
                }

        candidate_sha = head_commit(wt_path)
        if candidate_sha == actual_head:
            return {
                "outcome": "needs_human",
                "reason": "no_commit_created",
                "detail": "fix round produced no new commit to push",
                "pushed": False,
                "cost_usd": step_cost_usd,
            }

        # 3) Checkout an immutable clean verification worktree at candidate_sha and verify there!
        add_verify_wt = git_run(
            root, "worktree", "add", "--detach", str(verify_wt_path), candidate_sha
        )
        if add_verify_wt.returncode != 0:
            return {
                "outcome": "needs_human",
                "reason": "verify_worktree_create_failed",
                "detail": add_verify_wt.stderr.strip() or add_verify_wt.stdout.strip(),
                "pushed": False,
                "cost_usd": step_cost_usd,
            }

        # Re-verify Trust Anchor on the frozen candidate commit (catches upstream criteria edits)
        post_anchor = verify_trust_anchor(
            verify_wt_path, approval_branch=approval_branch, prefer_remote=True
        )
        if post_anchor.evaluation_criteria_changed:
            return {
                "outcome": "needs_human",
                "reason": "evaluation_criteria_changed_in_candidate",
                "detail": "candidate commit differs from approved evaluation criteria on approval_branch",
                "pushed": False,
                "cost_usd": step_cost_usd,
            }

        scrubbed_check_env = _build_scrubbed_env(kind="check")
        checks_to_run = _checks_to_verify_before_push(definition, flow_cfg, eff_delegation)
        for chk in checks_to_run:
            chk_dir = round_dir / "checks" / chk.name
            chk_dir.mkdir(parents=True, exist_ok=True)
            check_sandbox = _resolve_pr_sandbox_prefix(
                verify_wt_path,
                chk_dir,
                allow_network=False,
                sandbox_cmd=sandbox_cmd,
                trust_pr_code=trust_pr_code,
            )
            if check_sandbox is None:
                return {
                    "outcome": "needs_human",
                    "reason": "pr_sandbox_unavailable",
                    "detail": "no sandbox available for local check verification",
                    "pushed": False,
                    "cost_usd": step_cost_usd,
                }
            res = run_check(
                chk,
                worktree=verify_wt_path,
                run_id=round_dir.name,
                step_id=f"pr-verify-{chk.name}",
                step_dir=chk_dir,
                env_override=scrubbed_check_env,
                command_prefix=check_sandbox,
            )
            if res.status == "inconclusive":
                return {
                    "outcome": "inconclusive",
                    "reason": f"local_check_inconclusive:{chk.name}",
                    "detail": res.detail,
                    "pushed": False,
                    "cost_usd": step_cost_usd,
                }
            if res.status != "passed":
                return {
                    "outcome": "local_check_failed",
                    "reason": f"local_check_failed:{chk.name}",
                    "detail": res.detail,
                    "pushed": False,
                    "cost_usd": step_cost_usd,
                }

        # 4) Verify remote PR branch still exists at actual_head before pushing (never recreate deleted branch)
        ls_proc = git_run(
            wt_path, "ls-remote", "origin", f"refs/heads/{snapshot.head_ref_name}"
        )
        remote_lines = [ln.strip() for ln in ls_proc.stdout.splitlines() if ln.strip()]
        if ls_proc.returncode != 0 or not remote_lines:
            return {
                "outcome": "needs_human",
                "reason": "pr_branch_deleted_remotely",
                "detail": f"remote branch refs/heads/{snapshot.head_ref_name} no longer exists; refusing to recreate",
                "pushed": False,
                "cost_usd": step_cost_usd,
            }
        remote_sha = remote_lines[0].split()[0]
        if remote_sha != actual_head:
            return {
                "outcome": "waiting",
                "reason": "pr_head_moved_concurrently",
                "detail": f"remote branch advanced to {remote_sha[:12]} during verification",
                "pushed": False,
                "cost_usd": step_cost_usd,
            }

        # Push the EXACT verified candidate_sha non-force!
        push_proc = git_run(
            wt_path,
            "push",
            "origin",
            f"{candidate_sha}:refs/heads/{snapshot.head_ref_name}",
        )
        if push_proc.returncode != 0:
            return {
                "outcome": "needs_human",
                "reason": "git_push_failed",
                "detail": push_proc.stderr.strip() or push_proc.stdout.strip(),
                "pushed": False,
                "cost_usd": step_cost_usd,
            }

        return {
            "outcome": "pushed",
            "reason": "fix_pushed",
            "detail": (
                agent_response.summary
                if agent_response is not None and agent_response.summary
                else f"pushed verified fix commit {candidate_sha[:7]}"
            ),
            "pushed": True,
            "new_head_oid": candidate_sha,
            "changed_files": list(guard_res.changed_files),
            "delivered_finding_versions": delivered_finding_versions,
            "cost_usd": step_cost_usd,
        }
    finally:
        for path_to_clean in (verify_wt_path, wt_path):
            git_run(root, "worktree", "remove", "--force", str(path_to_clean))
            if path_to_clean.exists():
                shutil.rmtree(path_to_clean, ignore_errors=True)


def watch_pr(
    target_path: Path,
    pr_number: int,
    *,
    flow_name: str | None = None,
    agent_name: str | None = None,
    approval_branch: str = "main",
    poll_interval_sec: int = 30,
    max_rounds: int = 3,
    max_wait_sec: int = 1800,
    dry_run: bool = False,
    once: bool = False,
    trusted_bots: frozenset[str] = DEFAULT_TRUSTED_REVIEW_BOTS,
    trusted_reviewers: frozenset[str] = frozenset(),
    sandbox_cmd: tuple[str, ...] = (),
    trust_pr_code: bool = False,
) -> dict[str, Any]:
    """Watch an open PR until merge_ready (`garden:pr-ready`, never auto-merges) or bounded stop."""
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError(f"pr_number must be a positive integer, got {pr_number!r}")
    validate_watch_options(
        poll_interval_sec=poll_interval_sec,
        max_rounds=max_rounds,
        max_wait_sec=max_wait_sec,
    )

    root = repo_root(target_path)
    origin_repo_id, gh_repo = _resolve_origin_repo(root)
    started_mono = time.monotonic()
    deadline_mono = started_mono + max_wait_sec
    rounds_used = 0
    cumulative_cost_known = 0.0
    cost_unknown = False
    round_history: list[dict[str, Any]] = []
    stale_pre_push_head: str | None = None
    addressed_finding_versions: set[str] = set()
    previous_local_failure: dict[str, Any] | None = None
    perm_cache: dict[str, str | None] = {}

    def _sleep_capped() -> None:
        remaining = deadline_mono - time.monotonic()
        if remaining > 0:
            time.sleep(min(poll_interval_sec, remaining))

    while True:
        snapshot = fetch_pr_snapshot(
            pr_number,
            cwd=root,
            gh_repo=gh_repo,
            trusted_bots=trusted_bots,
            trusted_reviewers=trusted_reviewers,
            permission_cache=perm_cache,
            fetch_failed_logs=not dry_run,
        )

        # Verify GitHub PR repository matches Git remote 'origin' before any mutation/push
        pr_repo_id = parse_repo_identity(snapshot.url)
        if origin_repo_id and pr_repo_id and origin_repo_id != pr_repo_id:
            return {
                "pr_number": pr_number,
                "pr_url": snapshot.url,
                "status": "needs_human",
                "reasons": ["pr_origin_repo_mismatch"],
                "detail": f"PR belongs to {pr_repo_id}, but git origin is {origin_repo_id}",
                "rounds_used": rounds_used,
                "rounds": round_history,
                "exit_code": VERDICT_EXIT_CODES["NEEDS_HUMAN"],
            }

        classification = classify_pr_snapshot(snapshot)
        status = classification["status"]

        if dry_run:
            return {
                "pr_number": pr_number,
                "pr_url": snapshot.url,
                "status": status,
                "reasons": classification["reasons"],
                "dry_run": True,
                "rounds_used": 0,
                "snapshot": snapshot.to_dict(),
                "exit_code": 0,
            }

        if status == "merged":
            return {
                "pr_number": pr_number,
                "pr_url": snapshot.url,
                "status": "merged",
                "reasons": [],
                "rounds_used": rounds_used,
                "rounds": round_history,
                "exit_code": 0,
            }

        if status == "closed":
            return {
                "pr_number": pr_number,
                "pr_url": snapshot.url,
                "status": "closed",
                "reasons": classification["reasons"],
                "rounds_used": rounds_used,
                "rounds": round_history,
                "exit_code": 1,
            }

        if status == "inconclusive":
            return {
                "pr_number": pr_number,
                "pr_url": snapshot.url,
                "status": "inconclusive",
                "reasons": classification["reasons"],
                "detail": snapshot.review_threads_error,
                "rounds_used": rounds_used,
                "rounds": round_history,
                "exit_code": VERDICT_EXIT_CODES["INCONCLUSIVE"],
            }

        # If GitHub hasn't yet refreshed past the pre-push HEAD we already fixed, wait!
        if stale_pre_push_head is not None and snapshot.head_ref_oid == stale_pre_push_head:
            if once or time.monotonic() >= deadline_mono:
                return {
                    "pr_number": pr_number,
                    "pr_url": snapshot.url,
                    "status": "waiting",
                    "reasons": ["awaiting_refresh_after_push"],
                    "rounds_used": rounds_used,
                    "rounds": round_history,
                    "exit_code": 0,
                }
            _sleep_capped()
            continue
        if stale_pre_push_head is not None and snapshot.head_ref_oid != stale_pre_push_head:
            stale_pre_push_head = None

        if status == "merge_ready":
            label_res = _set_pr_managed_label(
                pr_number, "garden:pr-ready", cwd=root, gh_repo=gh_repo
            )
            return {
                "pr_number": pr_number,
                "pr_url": snapshot.url,
                "status": "merge_ready",
                "reasons": [],
                "head_ref_oid": snapshot.head_ref_oid,
                "rounds_used": rounds_used,
                "rounds": round_history,
                "cost_usd": None if cost_unknown else cumulative_cost_known,
                "cost_unknown": cost_unknown,
                "labels": label_res,
                "label_warning": label_res.get("label_warning"),
                "exit_code": 0,
            }

        if status == "needs_human":
            label_res = _set_pr_managed_label(
                pr_number, "garden:needs-human", cwd=root, gh_repo=gh_repo
            )
            return {
                "pr_number": pr_number,
                "pr_url": snapshot.url,
                "status": "needs_human",
                "reasons": classification["reasons"],
                "rounds_used": rounds_used,
                "rounds": round_history,
                "labels": label_res,
                "exit_code": VERDICT_EXIT_CODES["NEEDS_HUMAN"],
            }

        if status == "waiting":
            if once:
                return {
                    "pr_number": pr_number,
                    "pr_url": snapshot.url,
                    "status": "waiting",
                    "reasons": classification["reasons"],
                    "rounds_used": rounds_used,
                    "rounds": round_history,
                    "exit_code": 0,
                }
            if time.monotonic() >= deadline_mono:
                label_res = _set_pr_managed_label(
                    pr_number, "garden:needs-human", cwd=root, gh_repo=gh_repo
                )
                _post_pr_comment(
                    pr_number,
                    f"<!-- garden:v1 kind=pr_watch_timeout pr={pr_number} -->\n"
                    f"### [Garden] PR 監視の上限時間に到達しました\n\n"
                    f"- 状態: `waiting` ({', '.join(classification['reasons'])})\n"
                    f"- 修正ラウンド: {rounds_used}/{max_rounds}\n",
                    cwd=root,
                    gh_repo=gh_repo,
                )
                return {
                    "pr_number": pr_number,
                    "pr_url": snapshot.url,
                    "status": "needs_human",
                    "reasons": ["max_wait_sec_exceeded", *classification["reasons"]],
                    "rounds_used": rounds_used,
                    "rounds": round_history,
                    "labels": label_res,
                    "exit_code": VERDICT_EXIT_CODES["NEEDS_HUMAN"],
                }
            _sleep_capped()
            continue

        # Check watch deadline BEFORE starting a new fix round (unless --once)
        if not once and time.monotonic() >= deadline_mono:
            label_res = _set_pr_managed_label(
                pr_number, "garden:needs-human", cwd=root, gh_repo=gh_repo
            )
            _post_pr_comment(
                pr_number,
                f"<!-- garden:v1 kind=pr_watch_timeout pr={pr_number} -->\n"
                f"### [Garden] PR 監視の上限時間に到達しました\n\n"
                f"- 残課題: {', '.join(classification['reasons'])}\n"
                f"- 修正ラウンド: {rounds_used}/{max_rounds}\n",
                cwd=root,
                gh_repo=gh_repo,
            )
            return {
                "pr_number": pr_number,
                "pr_url": snapshot.url,
                "status": "needs_human",
                "reasons": ["max_wait_sec_exceeded", *classification["reasons"]],
                "rounds_used": rounds_used,
                "rounds": round_history,
                "labels": label_res,
                "exit_code": VERDICT_EXIT_CODES["NEEDS_HUMAN"],
            }

        # status == "needs_fix"
        has_conflict = (
            snapshot.mergeable == "CONFLICTING"
            or snapshot.merge_state_status == "DIRTY"
        )
        is_behind = snapshot.merge_state_status == "BEHIND"
        failed_checks_now = [c for c in snapshot.checks if c.status == "failed"]
        unaddressed_findings = [
            f
            for f in snapshot.findings
            if f.version_key not in addressed_finding_versions
        ]
        if (
            not has_conflict
            and not is_behind
            and not failed_checks_now
            and snapshot.findings
            and not unaddressed_findings
        ):
            label_res = _set_pr_managed_label(
                pr_number, "garden:needs-human", cwd=root, gh_repo=gh_repo
            )
            _post_pr_comment(
                pr_number,
                f"<!-- garden:v1 kind=pr_watch_awaiting_resolution pr={pr_number} -->\n"
                f"### [Garden] 修正反映済み・レビュースレッドの解決待ちです（PR #{pr_number}）\n\n"
                f"- CI は通過しており、未解決スレッド（{len(snapshot.findings)} 件）の最新版はすべて前回の修正ラウンドで対応済みです。\n"
                f"- 内容を確認のうえ、スレッドの解決または再レビューをお願いします。\n",
                cwd=root,
                gh_repo=gh_repo,
            )
            return {
                "pr_number": pr_number,
                "pr_url": snapshot.url,
                "status": "needs_human",
                "reasons": ["awaiting_review_thread_resolution"],
                "rounds_used": rounds_used,
                "rounds": round_history,
                "labels": label_res,
                "exit_code": VERDICT_EXIT_CODES["NEEDS_HUMAN"],
            }

        if rounds_used >= max_rounds:
            label_res = _set_pr_managed_label(
                pr_number, "garden:needs-human", cwd=root, gh_repo=gh_repo
            )
            _post_pr_comment(
                pr_number,
                f"<!-- garden:v1 kind=pr_watch_cap_reached pr={pr_number} -->\n"
                f"### [Garden] 自動修正の上限回数（{max_rounds} 回）に到達しました\n\n"
                f"- 残課題: {', '.join(classification['reasons'])}\n"
                f"- 人の確認・判断をお願いします。\n",
                cwd=root,
                gh_repo=gh_repo,
            )
            return {
                "pr_number": pr_number,
                "pr_url": snapshot.url,
                "status": "needs_human",
                "reasons": ["max_rounds_exceeded", *classification["reasons"]],
                "rounds_used": rounds_used,
                "rounds": round_history,
                "labels": label_res,
                "exit_code": VERDICT_EXIT_CODES["NEEDS_HUMAN"],
            }

        rounds_used += 1
        round_res = _run_pr_fix_round(
            root,
            snapshot,
            round_num=rounds_used,
            flow_name=flow_name,
            agent_name=agent_name,
            approval_branch=approval_branch,
            cumulative_cost_known=cumulative_cost_known,
            unaddressed_findings=unaddressed_findings,
            previous_local_failure=previous_local_failure,
            sandbox_cmd=sandbox_cmd,
            trust_pr_code=trust_pr_code,
        )
        round_history.append(round_res)
        step_cost = round_res.get("cost_usd")
        if step_cost is None:
            cost_unknown = True
        else:
            cumulative_cost_known = round(cumulative_cost_known + float(step_cost), 6)

        outcome = round_res["outcome"]
        if outcome == "pushed":
            stale_pre_push_head = snapshot.head_ref_oid
            previous_local_failure = None
            for vkey in round_res.get("delivered_finding_versions") or []:
                addressed_finding_versions.add(str(vkey))
            if once:
                return {
                    "pr_number": pr_number,
                    "pr_url": snapshot.url,
                    "status": "fix_pushed",
                    "reasons": classification["reasons"],
                    "rounds_used": rounds_used,
                    "rounds": round_history,
                    "exit_code": 0,
                }
            if time.monotonic() >= deadline_mono:
                return {
                    "pr_number": pr_number,
                    "pr_url": snapshot.url,
                    "status": "waiting",
                    "reasons": ["max_wait_sec_after_push"],
                    "rounds_used": rounds_used,
                    "rounds": round_history,
                    "exit_code": 0,
                }
            _sleep_capped()
            continue

        if outcome == "waiting":
            if once:
                return {
                    "pr_number": pr_number,
                    "pr_url": snapshot.url,
                    "status": "waiting",
                    "reasons": [str(round_res.get("reason") or "waiting")],
                    "rounds_used": rounds_used,
                    "rounds": round_history,
                    "exit_code": 0,
                }
            _sleep_capped()
            continue

        if outcome == "inconclusive":
            label_res = _set_pr_managed_label(
                pr_number, "garden:needs-human", cwd=root, gh_repo=gh_repo
            )
            return {
                "pr_number": pr_number,
                "pr_url": snapshot.url,
                "status": "inconclusive",
                "reasons": [str(round_res.get("reason") or "inconclusive")],
                "detail": round_res.get("detail"),
                "rounds_used": rounds_used,
                "rounds": round_history,
                "labels": label_res,
                "exit_code": VERDICT_EXIT_CODES["INCONCLUSIVE"],
            }

        if outcome == "local_check_failed":
            previous_local_failure = {
                "reason": str(round_res.get("reason") or "local_check_failed"),
                "detail": str(round_res.get("detail") or ""),
            }
            if once or rounds_used >= max_rounds:
                label_res = _set_pr_managed_label(
                    pr_number, "garden:needs-human", cwd=root, gh_repo=gh_repo
                )
                _post_pr_comment(
                    pr_number,
                    f"<!-- garden:v1 kind=pr_watch_local_failed pr={pr_number} -->\n"
                    f"### [Garden] PR 自動修正後のローカル検証に失敗しました\n\n"
                    f"- 理由: `{round_res.get('reason')}`\n"
                    f"- 詳細: {round_res.get('detail')}\n"
                    f"- 未検証のコミットは push していません。\n",
                    cwd=root,
                    gh_repo=gh_repo,
                )
                return {
                    "pr_number": pr_number,
                    "pr_url": snapshot.url,
                    "status": "needs_human",
                    "reasons": [str(round_res.get("reason") or "local_check_failed")],
                    "detail": round_res.get("detail"),
                    "rounds_used": rounds_used,
                    "rounds": round_history,
                    "labels": label_res,
                    "exit_code": VERDICT_EXIT_CODES["NEEDS_HUMAN"],
                }
            continue

        label_res = _set_pr_managed_label(
            pr_number, "garden:needs-human", cwd=root, gh_repo=gh_repo
        )
        q = round_res.get("question")
        if isinstance(q, dict) and q.get("prompt"):
            opts = q.get("options") or []
            opts_md = "\n".join(f"  - {o}" for o in opts) if isinstance(opts, list) and opts else ""
            body = (
                f"<!-- garden:v1 kind=pr_watch_spec_question pr={pr_number} -->\n"
                f"### [Garden] 仕様の判断が必要です（PR #{pr_number}）\n\n"
                f"- **問い**: {q['prompt']}\n"
                + (f"- **選択肢**:\n{opts_md}\n" if opts_md else "")
                + f"- **補足**: {round_res.get('detail') or ''}\n"
            )
        else:
            body = (
                f"<!-- garden:v1 kind=pr_watch_needs_human pr={pr_number} -->\n"
                f"### [Garden] 人の確認が必要です（PR #{pr_number}）\n\n"
                f"- **理由**: `{round_res.get('reason')}`\n"
                f"- **詳細**: {round_res.get('detail') or ''}\n"
            )
        _post_pr_comment(pr_number, body, cwd=root, gh_repo=gh_repo)
        return {
            "pr_number": pr_number,
            "pr_url": snapshot.url,
            "status": "needs_human",
            "reasons": [str(round_res.get("reason") or "needs_human")],
            "detail": round_res.get("detail"),
            "rounds_used": rounds_used,
            "rounds": round_history,
            "labels": label_res,
            "exit_code": VERDICT_EXIT_CODES["NEEDS_HUMAN"],
        }
