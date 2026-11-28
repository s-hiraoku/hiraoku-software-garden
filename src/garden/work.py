"""Single-command Issue-to-Mergeable-PR orchestrator (`garden work`) — Phase 2.

Connects:
  1. Fetch GitHub Issue (or load offline --issue-file)
  2. Mark Issue `garden:running`
  3. Run isolated `garden run` (implement + verify + post-agent guard)
  4. On SATISFIED: push verified branch and open linked PR via `garden reflect --execute`
  5. Unless `--no-watch`: run `garden pr-watch` until PR is mergeable (`garden:harvest-ready`)
     or stopped with `garden:needs-human` (never auto-merges)
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
from typing import Any

from garden.engine import VERDICT_EXIT_CODES, run_flow
from garden.git_utils import repo_root
from garden.issues import fetch_github_issue, load_issue_context_file
from garden.pr_watch import (
    DEFAULT_TRUSTED_REVIEW_BOTS,
    parse_pr_number_from_url,
    watch_pr,
)
from garden.publish import reflect_run
from garden.report import (
    clear_github_running,
    mark_github_running,
    post_github_report,
)
from garden.spec import verify_trust_anchor


def _post_issue_note(issue_number: int, body: str, *, cwd: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        body_path = Path(tmp_dir) / "issue-note.md"
        body_path.write_text(body, encoding="utf-8")
        subprocess.run(
            ["gh", "issue", "comment", str(issue_number), "--body-file", str(body_path)],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )


def _resolve_work_flow_name(
    root: Path, flow_name: str | None, *, approval_branch: str
) -> str:
    if flow_name:
        return flow_name
    anchor = verify_trust_anchor(root, approval_branch=approval_branch)
    definition = anchor.active_definition
    # Prefer a flow explicitly named implement_issue / implement / work, else non-observe_only all_specs flow
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
) -> dict[str, Any]:
    """Execute the end-to-end Issue -> Verified PR -> PR Watch workflow for a single Issue."""
    if issue_number is None and issue_file is None:
        raise ValueError("either --issue <N> or --issue-file <PATH> is required")
    if issue_number is not None and (
        isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0
    ):
        raise ValueError(f"--issue must be a positive integer, got {issue_number!r}")

    root = repo_root(target_path)
    if issue_file is not None:
        issue_ctx = load_issue_context_file(issue_file)
        effective_issue_num = (
            issue_number
            if issue_number is not None
            else issue_ctx.get("number")
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
    should_touch_github_issue = bool(
        post_issue_updates and not dry_run and effective_issue_num is not None
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

        summary: dict[str, Any] = {
            "target": str(root),
            "issue_number": effective_issue_num,
            "flow": resolved_flow,
            "run_id": run_res.run_id,
            "run_dir": str(run_res.run_dir) if run_res.run_dir else None,
            "verdict": run_res.verdict,
            "skipped_duplicate": run_res.skipped_duplicate,
            "dry_run": dry_run,
            "reflect": None,
            "pr_url": None,
            "pr_number": None,
            "watch": None,
            "exit_code": run_res.exit_code,
        }

        if run_res.verdict != "SATISFIED" or run_res.run_dir is None:
            if should_touch_github_issue and run_res.run_dir is not None:
                assert effective_issue_num is not None
                post_github_report(run_res.run_dir, effective_issue_num, cwd=root)
                marked_running = False
            return summary

        ref_res = reflect_run(
            run_res.run_dir,
            base_branch=base_branch,
            execute=not dry_run,
            cwd=root,
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
            _post_issue_note(
                effective_issue_num,
                f"### [Garden] PR を作成しました\n\n- PR: {pr_url}\n- Run ID: `{run_res.run_id}`\n",
                cwd=root,
            )
            clear_github_running(effective_issue_num, cwd=root)
            marked_running = False

        if not watch or pr_num is None:
            summary["exit_code"] = 0
            return summary

        watch_res = watch_pr(
            root,
            pr_num,
            flow_name=resolved_flow,
            agent_name=agent_name,
            approval_branch=approval_branch,
            poll_interval_sec=watch_poll_sec,
            max_rounds=watch_max_rounds,
            max_wait_sec=watch_max_wait_sec,
            dry_run=False,
            once=False,
            trusted_bots=trusted_bots,
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
