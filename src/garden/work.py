"""Single-command Issue-to-Mergeable-PR orchestrator (`garden work`) — Phase 2.

Connects:
  1. Validate all watch options upfront BEFORE mutating GitHub or running agents
  2. Fetch GitHub Issue (or load offline --issue-file)
  3. Mark Issue `garden:running`
  4. Run isolated `garden run` (implement + verify + post-agent guard)
  5. On SATISFIED: resume an existing open PR for (head, base) if present,
     otherwise push verified branch and open linked PR via `garden reflect --execute`
     with `allow_auto_merge=False` (NEVER auto-merges)
  6. Unless `--no-watch`: run `garden pr-watch` until PR is mergeable (`garden:pr-ready`)
     or stopped with `garden:needs-human`
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from garden.engine import VERDICT_EXIT_CODES, run_flow
from garden.git_utils import repo_root
from garden.issues import fetch_github_issue, load_issue_context_file
from garden.pr_watch import (
    DEFAULT_TRUSTED_REVIEW_BOTS,
    _resolve_origin_repo,
    parse_pr_number_from_url,
    validate_watch_options,
    watch_pr,
)
from garden.publish import reflect_run
from garden.report import (
    clear_github_running,
    mark_github_running,
    post_github_report,
)
from garden.spec import verify_trust_anchor


def _post_issue_note(
    issue_number: int, body: str, *, cwd: Path, gh_repo: str | None = None
) -> str | None:
    """Post an operational comment to the Issue, returning a warning string on failure (never raising)."""
    args = ["gh", "issue", "comment", str(issue_number), "--body-file"]
    with tempfile.TemporaryDirectory() as tmp_dir:
        body_path = Path(tmp_dir) / "issue-note.md"
        body_path.write_text(body, encoding="utf-8")
        cmd = [*args, str(body_path)]
        if gh_repo:
            cmd.extend(["-R", gh_repo])
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as error:
            return f"issue comment failed: {error}"
    if proc.returncode != 0:
        return proc.stderr.strip() or proc.stdout.strip() or "gh issue comment failed"
    return None


def _find_existing_open_pr(
    root: Path,
    *,
    head_branch: str,
    base_branch: str,
    gh_repo: str | None = None,
) -> tuple[int, str] | None:
    """Return (pr_number, pr_url) if an open PR already exists for (head, base) on the target repo."""
    if not head_branch or not shutil.which("gh"):
        return None
    cmd = [
        "gh",
        "pr",
        "list",
        "--state",
        "open",
        "--head",
        head_branch,
        "--base",
        base_branch,
        "--json",
        "number,url,headRefName,baseRefName",
    ]
    if gh_repo:
        cmd.extend(["-R", gh_repo])
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        items = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        if (
            item.get("headRefName") == head_branch
            and item.get("baseRefName") == base_branch
            and isinstance(item.get("number"), int)
            and item["number"] > 0
            and isinstance(item.get("url"), str)
            and item["url"].strip()
        ):
            return int(item["number"]), item["url"].strip()
    return None


def _resolve_work_flow_name(
    root: Path, flow_name: str | None, *, approval_branch: str
) -> str:
    if flow_name:
        return flow_name
    anchor = verify_trust_anchor(root, approval_branch=approval_branch)
    definition = anchor.active_definition
    for preferred in ("implement_issue", "implement", "work"):
        if preferred in definition.flows:
            return preferred
    ranked = sorted(
        definition.flows.items(),
        key=lambda kv: (
            0 if kv[1].delegation.mode != "observe_only" else 1,
            0 if kv[1].scope == "all_specs" else 1,
            kv[0],
        ),
    )
    return ranked[0][0]


def work_issue(
    target_path: Path,
    *,
    issue_number: int | None = None,
    issue_file: Path | None = None,
    flow_name: str | None = None,
    agent_name: str | None = None,
    base_branch: str = "main",
    approval_branch: str = "main",
    watch: bool = True,
    watch_poll_sec: int = 30,
    watch_max_rounds: int = 3,
    watch_max_wait_sec: int = 1800,
    dry_run: bool = False,
    force: bool = False,
    post_issue_updates: bool = True,
    trusted_bots: frozenset[str] = DEFAULT_TRUSTED_REVIEW_BOTS,
    trusted_reviewers: frozenset[str] = frozenset(),
    sandbox_cmd: tuple[str, ...] = (),
    trust_pr_code: bool = False,
) -> dict[str, Any]:
    """Execute the end-to-end Issue -> Verified PR -> PR Watch workflow for a single Issue."""
    if issue_number is None and issue_file is None:
        raise ValueError("either --issue <N> or --issue-file <PATH> is required")
    if issue_number is not None and (
        isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0
    ):
        raise ValueError(f"--issue must be a positive integer, got {issue_number!r}")
    # Validate watch options upfront BEFORE touching GitHub or executing any agent
    if watch:
        validate_watch_options(
            poll_interval_sec=watch_poll_sec,
            max_rounds=watch_max_rounds,
            max_wait_sec=watch_max_wait_sec,
        )

    root = repo_root(target_path)
    origin_repo_id, gh_repo = _resolve_origin_repo(root)

    if issue_file is not None:
        issue_ctx = load_issue_context_file(issue_file)
        effective_issue_num = (
            issue_number if issue_number is not None else issue_ctx.get("number")
        )
        if (
            issue_number is not None
            and issue_ctx.get("number") is not None
            and issue_ctx["number"] != issue_number
        ):
            raise ValueError(
                f"conflicting issue numbers: --issue {issue_number} vs --issue-file #{issue_ctx['number']}"
            )
    else:
        assert issue_number is not None
        issue_ctx = fetch_github_issue(issue_number, cwd=root)
        effective_issue_num = issue_number

    resolved_flow = _resolve_work_flow_name(
        root, flow_name, approval_branch=approval_branch
    )
    issue_repo_id = issue_ctx.get("repository")
    repo_verified_for_issue_updates = (
        issue_file is None
        or (
            origin_repo_id is not None
            and issue_repo_id is not None
            and origin_repo_id == issue_repo_id
        )
    )
    should_touch_github_issue = bool(
        post_issue_updates
        and not dry_run
        and effective_issue_num is not None
        and repo_verified_for_issue_updates
    )

    marked_running = False
    if should_touch_github_issue:
        assert effective_issue_num is not None
        mark_github_running(effective_issue_num, cwd=root)
        marked_running = True

    try:
        run_res = run_flow(
            root,
            resolved_flow,
            approval_branch=approval_branch,
            trigger_event="issues",
            trigger_id=f"work-issue-{effective_issue_num or 'file'}",
            issue_or_pr=effective_issue_num,
            force=force,
            issue_context=issue_ctx,
            fetch_issue=False,
        )

        actual_flow = str(run_res.run_data.get("flow") or resolved_flow)
        summary: dict[str, Any] = {
            "target": str(root),
            "issue_number": effective_issue_num,
            "flow": actual_flow,
            "run_id": run_res.run_id,
            "run_dir": str(run_res.run_dir) if run_res.run_dir else None,
            "verdict": run_res.verdict,
            "skipped_duplicate": run_res.skipped_duplicate,
            "resumed_existing_pr": False,
            "dry_run": dry_run,
            "reflect": None,
            "pr_url": None,
            "pr_number": None,
            "issue_note_warning": None,
            "watch": None,
            "exit_code": run_res.exit_code,
        }

        if run_res.verdict != "SATISFIED" or run_res.run_dir is None:
            if should_touch_github_issue and run_res.run_dir is not None:
                assert effective_issue_num is not None
                post_github_report(run_res.run_dir, effective_issue_num, cwd=root)
                marked_running = False
            return summary

        # Check if an open PR for this run's branch already exists (retry-safe watch resumption!)
        existing_pr: tuple[int, str] | None = None
        wt_branch = str(run_res.run_data.get("worktree_branch") or "").strip()
        if not dry_run and wt_branch:
            existing_pr = _find_existing_open_pr(
                root,
                head_branch=wt_branch,
                base_branch=base_branch,
                gh_repo=gh_repo,
            )

        if existing_pr is not None:
            pr_num, pr_url = existing_pr
            summary["pr_number"] = pr_num
            summary["pr_url"] = pr_url
            summary["resumed_existing_pr"] = True
            if should_touch_github_issue:
                assert effective_issue_num is not None
                clear_res = clear_github_running(effective_issue_num, cwd=root)
                if not clear_res.get("label_warning"):
                    marked_running = False
        else:
            # Always cap reflect to propose_pr: `garden work` never enables auto-merge!
            ref_res = reflect_run(
                run_res.run_dir,
                base_branch=base_branch,
                execute=not dry_run,
                cwd=root,
                allow_auto_merge=False,
            )
            summary["reflect"] = ref_res
            summary["pr_url"] = ref_res.get("pr_url")

            if dry_run:
                summary["exit_code"] = 0 if ref_res["allowed_action"] != "blocked" else 1
                return summary

            if ref_res["allowed_action"] in ("blocked", "none") or not ref_res.get("executed"):
                if should_touch_github_issue:
                    assert effective_issue_num is not None
                    post_github_report(run_res.run_dir, effective_issue_num, cwd=root)
                    marked_running = False
                summary["exit_code"] = (
                    0 if ref_res["allowed_action"] == "none" else VERDICT_EXIT_CODES["NEEDS_HUMAN"]
                )
                return summary

            pr_url = str(ref_res.get("pr_url") or "")
            pr_num = parse_pr_number_from_url(pr_url)
            summary["pr_number"] = pr_num

            if should_touch_github_issue:
                assert effective_issue_num is not None
                # Include canonical Garden operational marker so future Issue fetches exclude it!
                note_warn = _post_issue_note(
                    effective_issue_num,
                    f"<!-- garden:v1 kind=work_pr_created run_id={run_res.run_id} pr={pr_num or 0} -->\n"
                    f"### [Garden] PR を作成しました\n\n- PR: {pr_url}\n- Run ID: `{run_res.run_id}`\n",
                    cwd=root,
                    gh_repo=gh_repo,
                )
                summary["issue_note_warning"] = note_warn
                clear_res = clear_github_running(effective_issue_num, cwd=root)
                if not clear_res.get("label_warning"):
                    marked_running = False

        if not watch or summary["pr_number"] is None:
            summary["exit_code"] = 0
            return summary

        watch_res = watch_pr(
            root,
            int(summary["pr_number"]),
            flow_name=actual_flow,
            agent_name=agent_name,
            approval_branch=approval_branch,
            poll_interval_sec=watch_poll_sec,
            max_rounds=watch_max_rounds,
            max_wait_sec=watch_max_wait_sec,
            dry_run=False,
            once=False,
            trusted_bots=trusted_bots,
            trusted_reviewers=trusted_reviewers,
            sandbox_cmd=sandbox_cmd,
            trust_pr_code=trust_pr_code,
        )
        summary["watch"] = watch_res
        summary["exit_code"] = int(watch_res.get("exit_code", 0))
        return summary
    finally:
        if marked_running and effective_issue_num is not None:
            try:
                clear_github_running(effective_issue_num, cwd=root)
            except Exception:
                pass
