"""PR watch and guarded auto-fix loop (`garden pr-watch`) — Phase 2.

Monitors an open GitHub PR for CI failures, merge conflicts, and unresolved
actionable review comments (trusted humans + CodeRabbit/Codex bots). Fixes are
always executed inside an isolated git worktree, bounded by the approved Trust
Anchor delegation policy (`guard.py`) and local check re-verification before
pushing. `garden pr-watch` NEVER merges a PR; it marks a mergeable PR with
`garden:harvest-ready` for human final merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
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
from garden.checks import run_check
from garden.definition import (
    AgentConfig,
    CheckConfig,
    DelegationConfig,
    DefinitionError,
    FlowConfig,
    GardenDefinition,
)
from garden.engine import (
    VERDICT_EXIT_CODES,
    _apply_effective_delegation,
    _referenced_active_checks,
)
from garden.git_utils import GitError, git_run, head_commit, list_remotes, repo_root
from garden.guard import snapshot_untracked_files, verify_post_agent_guard
from garden.harness import evaluate_harness_requirements, flow_real_adapters
from garden.issues import TRUSTED_AUTHOR_ASSOCIATIONS, _is_bot_author, parse_repo_identity
from garden.report import (
    MANAGED_LABEL_SPECS,
    _ensure_managed_label,
    _fetch_attached_labels,
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
_PR_URL_NUM_RE = re.compile(r"/pull/(\d+)(?:[/?#].*)?$")
_ACTIONS_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")
_GH_TIMEOUT_SEC = 30
_MAX_LOG_EXCERPT_CHARS = 3500


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
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
    review_pending: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "url": self.url,
            "state": self.state,
            "is_draft": self.is_draft,
            "is_cross_repository": self.is_cross_repository,
            "mergeable": self.mergeable,
            "merge_state_status": self.merge_state_status,
            "head_ref_name": self.head_ref_name,
            "base_ref_name": self.base_ref_name,
            "head_ref_oid": self.head_ref_oid,
            "checks": [c.to_dict() for c in self.checks],
            "findings": [f.to_dict() for f in self.findings],
            "review_pending": self.review_pending,
        }


def parse_pr_number_from_url(pr_url: str) -> int | None:
    match = _PR_URL_NUM_RE.search(str(pr_url or "").strip())
    if not match:
        return None
    num = int(match.group(1))
    return num if num > 0 else None


def _run_gh(args: list[str], *, cwd: Path, timeout: int = _GH_TIMEOUT_SEC) -> subprocess.CompletedProcess[str]:
    if not shutil.which("gh"):
        raise RuntimeError("gh CLI is required for PR watch operations")
    try:
        return subprocess.run(
            ["gh", *args],
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


def _post_pr_comment(pr_number: int, body: str, *, cwd: Path) -> str | None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        body_path = Path(tmp_dir) / "pr-comment.md"
        body_path.write_text(body, encoding="utf-8")
        proc = _run_gh(
            ["pr", "comment", str(pr_number), "--body-file", str(body_path)],
            cwd=cwd,
        )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _set_pr_managed_label(
    pr_number: int, target_label: str | None, *, cwd: Path
) -> dict[str, Any]:
    """Transition managed Garden labels on a PR (adding target_label, removing other managed labels)."""
    managed = tuple(MANAGED_LABEL_SPECS.keys())
    add = [target_label] if target_label else []
    remove = [lbl for lbl in managed if lbl not in add]
    warnings: list[str] = []
    added: list[str] = []
    removed: list[str] = []

    for lbl in add:
        _ensure_managed_label(lbl, cwd=cwd)

    add_failed = False
    if add:
        args = ["issue", "edit", str(pr_number)]
        for lbl in add:
            args.extend(["--add-label", lbl])
        proc = _run_gh(args, cwd=cwd)
        if proc.returncode == 0:
            added = list(add)
        else:
            add_failed = True
            warnings.append(proc.stderr.strip() or "add-label failed")

    if remove and not add_failed:
        existing = _fetch_attached_labels(pr_number, cwd=cwd)
        to_remove = (
            [lbl for lbl in remove if lbl in existing]
            if existing is not None
            else remove
        )
        if to_remove:
            args = ["issue", "edit", str(pr_number)]
            for lbl in to_remove:
                args.extend(["--remove-label", lbl])
            proc = _run_gh(args, cwd=cwd)
            if proc.returncode == 0:
                removed = to_remove
            else:
                warnings.append(proc.stderr.strip() or "remove-label failed")

    return {
        "added_labels": added,
        "removed_labels": removed,
        "label_warning": "; ".join(warnings) if warnings else None,
    }


def _is_trusted_reviewer(
    author_raw: Any,
    author_association: str,
    trusted_bots: frozenset[str],
) -> bool:
    if isinstance(author_raw, dict):
        login = str(author_raw.get("login") or "").strip().lower()
    else:
        login = str(author_raw or "").strip().lower()
    if login in trusted_bots or f"{login}[bot]" in trusted_bots:
        return True
    if _is_bot_author(author_raw):
        return False
    return author_association.strip().upper() in TRUSTED_AUTHOR_ASSOCIATIONS


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


def _fetch_failed_check_log(details_url: str, *, cwd: Path) -> str:
    match = _ACTIONS_RUN_ID_RE.search(details_url or "")
    if not match:
        return ""
    run_id = match.group(1)
    try:
        proc = _run_gh(["run", "view", run_id, "--log-failed"], cwd=cwd, timeout=20)
    except RuntimeError:
        return ""
    if proc.returncode != 0 or not proc.stdout.strip():
        return ""
    text = proc.stdout.strip()
    if len(text) > _MAX_LOG_EXCERPT_CHARS:
        return "...(truncated)...\n" + text[-_MAX_LOG_EXCERPT_CHARS:]
    return text


def _fetch_review_threads_graphql(
    owner: str, repo: str, pr_number: int, *, cwd: Path
) -> list[dict[str, Any]]:
    query = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 20) {
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
    proc = _run_gh(
        [
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
        ],
        cwd=cwd,
    )
    if proc.returncode != 0:
        return []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    pr_node = (
        (payload.get("data") or {}).get("repository") or {}
    ).get("pullRequest") or {}
    nodes = (pr_node.get("reviewThreads") or {}).get("nodes") or []
    return [n for n in nodes if isinstance(n, dict)]


def build_pr_snapshot(
    pr_view_data: dict[str, Any],
    *,
    review_threads: list[dict[str, Any]] | None = None,
    trusted_bots: frozenset[str] = DEFAULT_TRUSTED_REVIEW_BOTS,
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
        if not _is_trusted_reviewer(author_raw, assoc, trusted_bots):
            continue
        body = str(first_comment.get("body") or "").strip()
        if not body or parse_garden_markers(body):
            continue
        # Combine follow-up replies from trusted reviewers in the same unresolved thread
        reply_texts: list[str] = [body]
        for reply in comments_nodes[1:]:
            if not isinstance(reply, dict):
                continue
            r_assoc = str(reply.get("authorAssociation") or "").strip().upper()
            if not _is_trusted_reviewer(reply.get("author"), r_assoc, trusted_bots):
                continue
            r_body = str(reply.get("body") or "").strip()
            if r_body and not parse_garden_markers(r_body):
                r_login = (
                    str((reply.get("author") or {}).get("login") or "reviewer")
                    if isinstance(reply.get("author"), dict)
                    else str(reply.get("author") or "reviewer")
                )
                reply_texts.append(f"[Follow-up by {r_login}]: {r_body}")

        fid = str(thread.get("id") or first_comment.get("id") or f"thread-{len(findings)}")
        if fid in seen_ids:
            continue
        seen_ids.add(fid)
        author_login = (
            str(author_raw.get("login") or "reviewer")
            if isinstance(author_raw, dict)
            else str(author_raw or "reviewer")
        )
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

    # Also capture CHANGES_REQUESTED top-level reviews on the current HEAD (or latest per reviewer)
    raw_reviews = pr_view_data.get("reviews") or []
    if isinstance(raw_reviews, list):
        latest_by_author: dict[str, dict[str, Any]] = {}
        for rev in raw_reviews:
            if not isinstance(rev, dict):
                continue
            author_raw = rev.get("author")
            assoc = str(rev.get("authorAssociation") or "").strip().upper()
            if not _is_trusted_reviewer(author_raw, assoc, trusted_bots):
                continue
            login = (
                str(author_raw.get("login") or "").strip().lower()
                if isinstance(author_raw, dict)
                else str(author_raw or "").strip().lower()
            )
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

    # Detect in-progress bot review comments (e.g. CodeRabbit / Codex review in progress)
    review_pending = False
    raw_comments = pr_view_data.get("comments") or []
    if isinstance(raw_comments, list):
        for cmt in raw_comments:
            if not isinstance(cmt, dict):
                continue
            body = str(cmt.get("body") or "")
            if "review in progress by coderabbit.ai" in body.lower():
                review_pending = True
            if "<!-- codex-review-state:v1" in body and '"status":"in_progress"' in body:
                review_pending = True

    return PrSnapshot(
        number=number,
        url=url,
        state=state,
        is_draft=is_draft,
        is_cross_repository=is_cross_repo,
        mergeable=mergeable,
        merge_state_status=merge_state,
        head_ref_name=head_ref,
        base_ref_name=base_ref,
        head_ref_oid=head_oid,
        checks=tuple(checks),
        findings=tuple(findings),
        review_pending=review_pending,
    )


def fetch_pr_snapshot(
    pr_number: int,
    *,
    cwd: Path,
    trusted_bots: frozenset[str] = DEFAULT_TRUSTED_REVIEW_BOTS,
    fetch_failed_logs: bool = True,
) -> PrSnapshot:
    """Query GitHub for the current PR state, status checks, and unresolved review threads."""
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError(f"pr_number must be a positive integer, got {pr_number!r}")

    proc = _run_gh(
        [
            "pr",
            "view",
            str(pr_number),
            "--json",
            "number,url,state,isDraft,isCrossRepository,mergeable,mergeStateStatus,"
            "headRefName,baseRefName,headRefOid,statusCheckRollup,reviews,comments",
        ],
        cwd=cwd,
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
    if repo_id:
        parts = repo_id.split("/")
        if len(parts) == 3:
            threads = _fetch_review_threads_graphql(parts[1], parts[2], pr_number, cwd=cwd)

    failed_logs: dict[str, str] = {}
    if fetch_failed_logs:
        for chk in _parse_status_rollup(pr_data.get("statusCheckRollup")):
            if chk.status == "failed" and chk.details_url and chk.details_url not in failed_logs:
                failed_logs[chk.details_url] = _fetch_failed_check_log(chk.details_url, cwd=cwd)

    return build_pr_snapshot(
        pr_data,
        review_threads=threads,
        trusted_bots=trusted_bots,
        failed_logs_by_url=failed_logs,
    )


def classify_pr_snapshot(snapshot: PrSnapshot) -> dict[str, Any]:
    """Deterministically classify a PrSnapshot into an action state.

    Status values:
      - merged: PR is already merged
      - closed: PR is closed without merge
      - needs_human: PR is draft or cross-repository (unsafe for automated push)
      - waiting: checks or review bots still running, or GitHub mergeability UNKNOWN
      - needs_fix: failed checks, merge conflict, or unresolved actionable review findings
      - merge_ready: mergeable, all checks green, 0 unresolved actionable findings
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
    mergeable_unknown = snapshot.mergeable == "UNKNOWN" and not has_conflict

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
    if failed_checks:
        fix_reasons.append(f"failed_checks:{len(failed_checks)}")
    if snapshot.findings:
        fix_reasons.append(f"unresolved_reviews:{len(snapshot.findings)}")

    if fix_reasons:
        return {"status": "needs_fix", "reasons": fix_reasons}

    if snapshot.mergeable != "MERGEABLE":
        return {
            "status": "needs_human",
            "reasons": [f"unmergeable_state:{snapshot.mergeable}:{snapshot.merge_state_status}"],
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
        # Prefer a non-observe_only all_specs flow, otherwise any non-observe_only flow, else first flow
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


_CONFLICT_MARKER_RE = re.compile(r"^(?:<{7}\s|={7}$|>{7}\s)", re.MULTILINE)


def _list_unmerged_files(worktree: Path) -> list[str]:
    proc = git_run(worktree, "diff", "--name-only", "--diff-filter=U")
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _has_conflict_markers(file_path: Path) -> bool:
    if not file_path.is_file():
        return True
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return _CONFLICT_MARKER_RE.search(content) is not None


def _run_pr_fix_round(
    root: Path,
    snapshot: PrSnapshot,
    *,
    round_num: int,
    flow_name: str | None,
    agent_name: str | None,
    approval_branch: str,
    cumulative_cost_known: float,
    started_mono: float,
) -> dict[str, Any]:
    """Execute one isolated, guarded fix round for a PR and push only after local checks pass."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    round_dir = root / ".garden" / "runs" / f"pr-watch-{snapshot.number}-{stamp}-r{round_num}"
    round_dir.mkdir(parents=True, exist_ok=True)
    wt_root = root / ".garden" / "worktrees"
    wt_root.mkdir(parents=True, exist_ok=True)
    wt_path = wt_root / f"pr-{snapshot.number}-{stamp}-r{round_num}"

    # Fetch the exact PR head and base branch from origin without touching the caller's checkout
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
    git_run(
        root,
        "fetch",
        "origin",
        f"+refs/heads/{snapshot.base_ref_name}:refs/remotes/origin/{snapshot.base_ref_name}",
    )

    add_wt = git_run(
        root, "worktree", "add", "--detach", str(wt_path), snapshot.head_ref_oid
    )
    if add_wt.returncode != 0:
        # Fallback to origin/<head_ref_name> if head_ref_oid wasn't yet fetched directly
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

        # Verify Trust Anchor ON THE PR WORKTREE so evaluation-criteria edits are detected!
        anchor = verify_trust_anchor(wt_path, approval_branch=approval_branch)
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

        # Harness preflight inside the PR worktree before invoking real agents
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
        conflict_files: list[str] = []
        merged_base_cleanly = False
        if has_conflict:
            merge_proc = git_run(
                wt_path,
                "merge",
                "--no-commit",
                "--no-ff",
                f"origin/{snapshot.base_ref_name}",
            )
            if merge_proc.returncode == 0:
                merged_base_cleanly = True
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

        failed_checks = [c.to_dict() for c in snapshot.checks if c.status == "failed"]
        review_findings = [f.to_dict() for f in snapshot.findings]
        needs_agent = bool(conflict_files or failed_checks or review_findings)

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
                        "review_findings": review_findings,
                    }
                },
            )
            step_dir = round_dir / "agent"
            if agent_cfg.adapter == "mock":
                agent_response, _ = run_mock_adapter(
                    agent_cfg, request, worktree=wt_path, step_dir=step_dir
                )
            else:
                agent_response, _ = run_command_adapter(
                    agent_cfg, request, worktree=wt_path, step_dir=step_dir
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

        # Ensure no unresolved merge conflict markers remain in any unmerged file
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

        # Deterministic post-agent guard against pre-fix head_oid
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
            elapsed_sec=time.monotonic() - started_mono,
            baseline_untracked=baseline_untracked,
            ignore_prefixes=(".garden/runs/", ".garden/worktrees/"),
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

        # Local verification checks MUST pass before pushing any commit to the PR!
        checks_to_run = _checks_to_verify_before_push(definition, flow_cfg, eff_delegation)
        for chk in checks_to_run:
            chk_dir = round_dir / "checks" / chk.name
            res = run_check(
                chk,
                worktree=wt_path,
                run_id=round_dir.name,
                step_id=f"pr-verify-{chk.name}",
                step_dir=chk_dir,
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

        # Stage only guard-approved changes (and conflict-resolved paths) before committing
        paths_to_stage = list(dict.fromkeys([*guard_res.changed_files, *remaining_unmerged]))
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

        new_head = head_commit(wt_path)
        if new_head == actual_head:
            return {
                "outcome": "needs_human",
                "reason": "no_commit_created",
                "detail": "fix round produced no new commit to push",
                "pushed": False,
                "cost_usd": step_cost_usd,
            }

        # Non-force push only: never overwrite concurrent human commits
        push_proc = git_run(
            wt_path, "push", "origin", f"HEAD:refs/heads/{snapshot.head_ref_name}"
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
                else f"pushed verified fix commit {new_head[:7]}"
            ),
            "pushed": True,
            "new_head_oid": new_head,
            "changed_files": list(guard_res.changed_files),
            "cost_usd": step_cost_usd,
        }
    finally:
        git_run(root, "worktree", "remove", "--force", str(wt_path))
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)


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
) -> dict[str, Any]:
    """Watch an open PR until merge_ready (never auto-merges) or bounded stop."""
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError(f"pr_number must be a positive integer, got {pr_number!r}")
    if poll_interval_sec < 1:
        raise ValueError("poll_interval_sec must be >= 1")
    if max_rounds < 0:
        raise ValueError("max_rounds must be >= 0")
    if max_wait_sec < 1:
        raise ValueError("max_wait_sec must be >= 1")

    root = repo_root(target_path)
    started_mono = time.monotonic()
    rounds_used = 0
    cumulative_cost_known = 0.0
    cost_unknown = False
    round_history: list[dict[str, Any]] = []
    last_pushed_head: str | None = None
    addressed_finding_ids: set[str] = set()

    while True:
        snapshot = fetch_pr_snapshot(
            pr_number,
            cwd=root,
            trusted_bots=trusted_bots,
            fetch_failed_logs=not dry_run,
        )
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

        if status == "merge_ready":
            label_res = _set_pr_managed_label(pr_number, "garden:harvest-ready", cwd=root)
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
                "exit_code": 0,
            }

        if status == "needs_human":
            label_res = _set_pr_managed_label(pr_number, "garden:needs-human", cwd=root)
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

        # Waiting for CI / review bot / mergeable calculation
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
            if time.monotonic() - started_mono >= max_wait_sec:
                label_res = _set_pr_managed_label(pr_number, "garden:needs-human", cwd=root)
                _post_pr_comment(
                    pr_number,
                    f"### [Garden] PR 監視の上限時間に到達しました\n\n"
                    f"- 状態: `waiting` ({', '.join(classification['reasons'])})\n"
                    f"- 修正ラウンド: {rounds_used}/{max_rounds}\n",
                    cwd=root,
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
            time.sleep(poll_interval_sec)
            continue

        # status == "needs_fix"
        has_conflict = (
            snapshot.mergeable == "CONFLICTING"
            or snapshot.merge_state_status == "DIRTY"
        )
        failed_checks_now = [c for c in snapshot.checks if c.status == "failed"]
        unaddressed_findings = [
            f for f in snapshot.findings if f.finding_id not in addressed_finding_ids
        ]
        if (
            not has_conflict
            and not failed_checks_now
            and snapshot.findings
            and not unaddressed_findings
        ):
            label_res = _set_pr_managed_label(pr_number, "garden:needs-human", cwd=root)
            _post_pr_comment(
                pr_number,
                f"### [Garden] 修正反映済み・レビュースレッドの解決待ちです（PR #{pr_number}）\n\n"
                f"- CI は通過しており、未解決スレッド（{len(snapshot.findings)} 件）はすべて前回の修正ラウンドで対応済みです。\n"
                f"- 内容を確認のうえ、スレッドの解決または再レビューをお願いします。\n",
                cwd=root,
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

        if last_pushed_head is not None and snapshot.head_ref_oid == last_pushed_head:
            # Guard against acting twice on the exact same commit if reviews/checks haven't refreshed
            if once or time.monotonic() - started_mono >= max_wait_sec:
                return {
                    "pr_number": pr_number,
                    "pr_url": snapshot.url,
                    "status": "waiting",
                    "reasons": ["awaiting_refresh_after_push"],
                    "rounds_used": rounds_used,
                    "rounds": round_history,
                    "exit_code": 0,
                }
            time.sleep(poll_interval_sec)
            continue

        if rounds_used >= max_rounds:
            label_res = _set_pr_managed_label(pr_number, "garden:needs-human", cwd=root)
            _post_pr_comment(
                pr_number,
                f"### [Garden] 自動修正の上限回数（{max_rounds} 回）に到達しました\n\n"
                f"- 残課題: {', '.join(classification['reasons'])}\n"
                f"- 人の確認・判断をお願いします。\n",
                cwd=root,
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
            started_mono=started_mono,
        )
        round_history.append(round_res)
        step_cost = round_res.get("cost_usd")
        if step_cost is None:
            cost_unknown = True
        else:
            cumulative_cost_known = round(cumulative_cost_known + float(step_cost), 6)

        outcome = round_res["outcome"]
        if outcome == "pushed":
            last_pushed_head = str(round_res.get("new_head_oid") or "")
            for finding in snapshot.findings:
                addressed_finding_ids.add(finding.finding_id)
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
            if time.monotonic() - started_mono >= max_wait_sec:
                return {
                    "pr_number": pr_number,
                    "pr_url": snapshot.url,
                    "status": "waiting",
                    "reasons": ["max_wait_sec_after_push"],
                    "rounds_used": rounds_used,
                    "rounds": round_history,
                    "exit_code": 0,
                }
            time.sleep(poll_interval_sec)
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
            time.sleep(poll_interval_sec)
            continue

        if outcome == "inconclusive":
            label_res = _set_pr_managed_label(pr_number, "garden:needs-human", cwd=root)
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
            if once or rounds_used >= max_rounds:
                label_res = _set_pr_managed_label(pr_number, "garden:needs-human", cwd=root)
                _post_pr_comment(
                    pr_number,
                    f"### [Garden] PR 自動修正後のローカル検証に失敗しました\n\n"
                    f"- 理由: `{round_res.get('reason')}`\n"
                    f"- 詳細: {round_res.get('detail')}\n"
                    f"- 未検証のコミットは push していません。\n",
                    cwd=root,
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

        # needs_human
        label_res = _set_pr_managed_label(pr_number, "garden:needs-human", cwd=root)
        q = round_res.get("question")
        if isinstance(q, dict) and q.get("prompt"):
            opts = q.get("options") or []
            opts_md = "\n".join(f"  - {o}" for o in opts) if isinstance(opts, list) and opts else ""
            body = (
                f"### [Garden] 仕様の判断が必要です（PR #{pr_number}）\n\n"
                f"- **問い**: {q['prompt']}\n"
                + (f"- **選択肢**:\n{opts_md}\n" if opts_md else "")
                + f"- **補足**: {round_res.get('detail') or ''}\n"
            )
        else:
            body = (
                f"### [Garden] 人の確認が必要です（PR #{pr_number}）\n\n"
                f"- **理由**: `{round_res.get('reason')}`\n"
                f"- **詳細**: {round_res.get('detail') or ''}\n"
            )
        _post_pr_comment(pr_number, body, cwd=root)
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
