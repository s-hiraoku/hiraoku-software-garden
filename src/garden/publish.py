"""Phase-6 deterministic reflection gate (observe / propose_pr / auto_merge decision & execution)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any

from garden.report import load_run_data


@dataclass(frozen=True)
class ReflectDecision:
    run_id: str
    allowed_action: str  # blocked | none | propose_pr | auto_merge
    reasons: tuple[str, ...]
    worktree_branch: str | None
    target_commit: str
    effective_mode: str
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "allowed_action": self.allowed_action,
            "reasons": list(self.reasons),
            "worktree_branch": self.worktree_branch,
            "target_commit": self.target_commit,
            "effective_mode": self.effective_mode,
            "verdict": self.verdict,
        }


def evaluate_reflect_gate(run_data: dict[str, Any]) -> ReflectDecision:
    """Deterministically evaluate what reflection action (if any) is permitted for a run."""
    run_id = str(run_data.get("run_id") or "")
    verdict = str(run_data.get("verdict") or "UNKNOWN")
    target = run_data.get("target") or {}
    target_commit = str(target.get("commit") or "")
    eval_changed = bool(target.get("evaluation_criteria_changed"))
    eff_del = run_data.get("effective_delegation") or {}
    eff_mode = str(eff_del.get("mode") or "observe_only")
    flow_scope = str(run_data.get("flow_scope") or "flow_checks")
    branch = run_data.get("worktree_branch")
    branch_str = str(branch) if isinstance(branch, str) and branch.strip() else None

    if verdict != "SATISFIED":
        return ReflectDecision(
            run_id=run_id,
            allowed_action="blocked",
            reasons=(f"verdict_not_satisfied:{verdict}",),
            worktree_branch=branch_str,
            target_commit=target_commit,
            effective_mode=eff_mode,
            verdict=verdict,
        )

    if eff_mode == "observe_only":
        return ReflectDecision(
            run_id=run_id,
            allowed_action="blocked",
            reasons=("mode_observe_only",),
            worktree_branch=branch_str,
            target_commit=target_commit,
            effective_mode=eff_mode,
            verdict=verdict,
        )

    if branch_str is None:
        return ReflectDecision(
            run_id=run_id,
            allowed_action="none",
            reasons=("no_change_branch",),
            worktree_branch=None,
            target_commit=target_commit,
            effective_mode=eff_mode,
            verdict=verdict,
        )

    if eff_mode == "propose_pr":
        return ReflectDecision(
            run_id=run_id,
            allowed_action="propose_pr",
            reasons=("delegation_propose_pr",),
            worktree_branch=branch_str,
            target_commit=target_commit,
            effective_mode=eff_mode,
            verdict=verdict,
        )

    if eff_mode == "auto_merge":
        downgrade_reasons: list[str] = []
        if eval_changed:
            downgrade_reasons.append("evaluation_criteria_changed_blocks_auto_merge")
        if flow_scope != "all_specs":
            downgrade_reasons.append("partial_flow_scope_blocks_auto_merge")
        criteria = run_data.get("criteria_results") or {}
        if not criteria or any(
            info.get("verdict") != "satisfied" for info in criteria.values()
        ):
            downgrade_reasons.append("incomplete_criteria_blocks_auto_merge")
        if downgrade_reasons:
            return ReflectDecision(
                run_id=run_id,
                allowed_action="propose_pr",
                reasons=tuple(downgrade_reasons),
                worktree_branch=branch_str,
                target_commit=target_commit,
                effective_mode=eff_mode,
                verdict=verdict,
            )
        return ReflectDecision(
            run_id=run_id,
            allowed_action="auto_merge",
            reasons=("all_auto_merge_gates_satisfied",),
            worktree_branch=branch_str,
            target_commit=target_commit,
            effective_mode=eff_mode,
            verdict=verdict,
        )

    return ReflectDecision(
        run_id=run_id,
        allowed_action="blocked",
        reasons=(f"unknown_delegation_mode:{eff_mode}",),
        worktree_branch=branch_str,
        target_commit=target_commit,
        effective_mode=eff_mode,
        verdict=verdict,
    )


def reflect_run(
    run_dir: Path,
    *,
    base_branch: str = "main",
    execute: bool = False,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Evaluate the reflection gate for run_dir, optionally creating a PR (and enabling auto-merge)."""
    run_data = load_run_data(run_dir)
    decision = evaluate_reflect_gate(run_data)
    result: dict[str, Any] = decision.to_dict() | {
        "dry_run": not execute,
        "executed": False,
        "pr_url": None,
        "auto_merge_enabled": False,
    }
    if not execute or decision.allowed_action in ("blocked", "none"):
        return result

    assert decision.worktree_branch is not None
    title = f"[Garden] {run_data.get('flow', 'run')}: {decision.run_id}"
    summary_file = run_dir / "summary.md"
    body_args = (
        ["--body-file", str(summary_file)]
        if summary_file.is_file()
        else ["--body", f"Automated Garden run `{decision.run_id}` (`{decision.verdict}`)."]
    )

    push_proc = subprocess.run(
        ["git", "push", "-u", "origin", decision.worktree_branch],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if push_proc.returncode != 0:
        raise RuntimeError(
            f"git push failed: {push_proc.stderr.strip() or push_proc.stdout.strip()}"
        )

    pr_proc = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base_branch,
            "--head",
            decision.worktree_branch,
            "--title",
            title,
            *body_args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if pr_proc.returncode != 0:
        raise RuntimeError(
            f"gh pr create failed: {pr_proc.stderr.strip() or pr_proc.stdout.strip()}"
        )

    pr_url = pr_proc.stdout.strip()
    result["executed"] = True
    result["pr_url"] = pr_url

    if decision.allowed_action == "auto_merge":
        # Never bypass branch protection (--admin is intentionally never used)
        merge_proc = subprocess.run(
            ["gh", "pr", "merge", pr_url, "--auto", "--merge"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if merge_proc.returncode != 0:
            raise RuntimeError(
                f"gh pr merge --auto failed: {merge_proc.stderr.strip() or merge_proc.stdout.strip()}"
            )
        result["auto_merge_enabled"] = True

    return result
