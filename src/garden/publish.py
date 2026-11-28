"""Phase-6 deterministic reflection gate (observe / propose_pr / auto_merge decision & execution)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

from garden.git_utils import list_remotes
from garden.issues import parse_repo_identity
from garden.report import load_run_data


@dataclass(frozen=True)
class ReflectDecision:
    run_id: str
    allowed_action: str  # blocked | none | propose_pr | auto_merge
    reasons: tuple[str, ...]
    worktree_branch: str | None
    verified_commit: str | None
    target_commit: str
    effective_mode: str
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "allowed_action": self.allowed_action,
            "reasons": list(self.reasons),
            "worktree_branch": self.worktree_branch,
            "verified_commit": self.verified_commit,
            "target_commit": self.target_commit,
            "effective_mode": self.effective_mode,
            "verdict": self.verdict,
        }


def evaluate_reflect_gate(
    run_data: dict[str, Any], *, target_on_base: bool | None = None
) -> ReflectDecision:
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
    branch_str = str(branch).strip() if isinstance(branch, str) and branch.strip() else None
    v_commit = run_data.get("verified_commit")
    verified_str = (
        str(v_commit).strip() if isinstance(v_commit, str) and v_commit.strip() else None
    )

    if verdict != "SATISFIED":
        return ReflectDecision(
            run_id=run_id,
            allowed_action="blocked",
            reasons=(f"verdict_not_satisfied:{verdict}",),
            worktree_branch=branch_str,
            verified_commit=verified_str,
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
            verified_commit=verified_str,
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
            verified_commit=verified_str,
            target_commit=target_commit,
            effective_mode=eff_mode,
            verdict=verdict,
        )

    if verified_str is None or (target_commit and verified_str == target_commit):
        return ReflectDecision(
            run_id=run_id,
            allowed_action="none",
            reasons=("no_verified_commit_diff",),
            worktree_branch=branch_str,
            verified_commit=verified_str,
            target_commit=target_commit,
            effective_mode=eff_mode,
            verdict=verdict,
        )

    if target_on_base is False:
        return ReflectDecision(
            run_id=run_id,
            allowed_action="blocked",
            reasons=("target_commit_not_on_base_branch",),
            worktree_branch=branch_str,
            verified_commit=verified_str,
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
            verified_commit=verified_str,
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
                verified_commit=verified_str,
                target_commit=target_commit,
                effective_mode=eff_mode,
                verdict=verdict,
            )
        return ReflectDecision(
            run_id=run_id,
            allowed_action="auto_merge",
            reasons=("all_auto_merge_gates_satisfied",),
            worktree_branch=branch_str,
            verified_commit=verified_str,
            target_commit=target_commit,
            effective_mode=eff_mode,
            verdict=verdict,
        )

    return ReflectDecision(
        run_id=run_id,
        allowed_action="blocked",
        reasons=(f"unknown_delegation_mode:{eff_mode}",),
        worktree_branch=branch_str,
        verified_commit=verified_str,
        target_commit=target_commit,
        effective_mode=eff_mode,
        verdict=verdict,
    )


def _find_git_root(path: Path) -> Path | None:
    probe = path.resolve()
    if probe.is_file():
        probe = probe.parent
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=probe,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip())


def _resolve_base_ref(git_root: Path, base_branch: str) -> str | None:
    if not base_branch or base_branch.startswith("-") or ".." in base_branch:
        return None
    for candidate in (f"refs/heads/{base_branch}", f"refs/remotes/origin/{base_branch}"):
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            cwd=git_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return candidate
    return None


def _verify_branch_in_git(
    git_root: Path,
    decision: ReflectDecision,
    *,
    base_branch: str,
    agent_changed_files: list[str] | None,
) -> ReflectDecision:
    """Confirm branch tip, base-branch ancestry, and guard-approved diff before reflection."""
    assert decision.worktree_branch is not None
    assert decision.verified_commit is not None

    ref = f"refs/heads/{decision.worktree_branch}"
    tip_proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=git_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if tip_proc.returncode != 0 or not tip_proc.stdout.strip():
        return replace(
            decision,
            allowed_action="blocked",
            reasons=("branch_missing",),
        )

    tip_sha = tip_proc.stdout.strip()
    if tip_sha != decision.verified_commit:
        return replace(
            decision,
            allowed_action="blocked",
            reasons=("branch_tip_mismatch_verified_commit",),
        )

    if not decision.target_commit:
        return replace(
            decision,
            allowed_action="blocked",
            reasons=("missing_target_commit",),
        )

    # Ensure target_commit is already on base_branch so unreviewed PR commits can never
    # piggyback onto an automated reflection PR or auto-merge into base_branch.
    base_ref = _resolve_base_ref(git_root, base_branch)
    if base_ref is None:
        return replace(
            decision,
            allowed_action="blocked",
            reasons=(f"base_branch_not_found:{base_branch}",),
        )

    anc_proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", decision.target_commit, base_ref],
        cwd=git_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if anc_proc.returncode == 1:
        return replace(
            decision,
            allowed_action="blocked",
            reasons=(f"target_commit_not_on_base_branch:{base_branch}",),
        )
    if anc_proc.returncode != 0:
        return replace(
            decision,
            allowed_action="blocked",
            reasons=("base_ancestry_verification_failed",),
        )

    diff_proc = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", decision.target_commit, tip_sha],
        cwd=git_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if diff_proc.returncode != 0:
        return replace(
            decision,
            allowed_action="blocked",
            reasons=("diff_verification_failed",),
        )

    diff_files = [
        line.strip() for line in diff_proc.stdout.splitlines() if line.strip()
    ]
    if not diff_files:
        return replace(
            decision,
            allowed_action="none",
            reasons=("empty_diff_against_target_commit",),
        )

    if not agent_changed_files:
        return replace(
            decision,
            allowed_action="blocked",
            reasons=("missing_agent_changed_files",),
        )

    unapproved = sorted(set(diff_files) - set(agent_changed_files))
    if unapproved:
        return replace(
            decision,
            allowed_action="blocked",
            reasons=(f"unapproved_files_in_verified_commit:{','.join(unapproved)}",),
        )

    return decision


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
    git_root = _find_git_root(cwd) if cwd is not None else _find_git_root(run_dir)
    git_verified = False

    raw_agent_files = run_data.get("agent_changed_files")
    agent_changed_files = (
        [str(p) for p in raw_agent_files if isinstance(p, str) and p.strip()]
        if isinstance(raw_agent_files, list)
        else None
    )

    if decision.allowed_action in ("propose_pr", "auto_merge"):
        if git_root is not None:
            decision = _verify_branch_in_git(
                git_root,
                decision,
                base_branch=base_branch,
                agent_changed_files=agent_changed_files,
            )
            git_verified = True
        elif execute:
            raise RuntimeError(
                f"cannot execute reflect: no git repository found for {run_dir}"
            )

    issue_info = run_data.get("issue") if isinstance(run_data.get("issue"), dict) else None
    issue_num_raw = issue_info.get("number") if issue_info else None
    issue_number = (
        int(issue_num_raw)
        if isinstance(issue_num_raw, int) and not isinstance(issue_num_raw, bool) and issue_num_raw > 0
        else None
    )
    issue_title = (
        " ".join(str(issue_info.get("title") or "").split()) if issue_info else ""
    )
    issue_url = str(issue_info.get("url") or "").strip() if issue_info else ""
    issue_repo = (
        (
            str(issue_info.get("repository")).strip().lower()
            if isinstance(issue_info.get("repository"), str) and issue_info.get("repository").strip()
            else None
        )
        or parse_repo_identity(issue_url)
    ) if issue_info else None

    target_repo: str | None = None
    if git_root is not None:
        try:
            remotes = list_remotes(git_root)
            origin_url = remotes.get("origin") or (
                remotes[sorted(remotes.keys())[0]] if remotes else None
            )
            target_repo = parse_repo_identity(origin_url)
        except Exception:
            target_repo = None
    if target_repo is None:
        target_repo = parse_repo_identity((run_data.get("target") or {}).get("remote"))

    issue_repo_matched = bool(
        issue_number is not None
        and issue_repo is not None
        and target_repo is not None
        and issue_repo == target_repo
    )
    issue_repo_mismatch = bool(
        issue_number is not None
        and issue_repo is not None
        and target_repo is not None
        and issue_repo != target_repo
    )

    result: dict[str, Any] = decision.to_dict() | {
        "base_branch": base_branch,
        "git_verified": git_verified,
        "dry_run": not execute,
        "executed": False,
        "pr_url": None,
        "issue_number": issue_number,
        "issue_linked": False,
        "issue_repo_mismatch": issue_repo_mismatch,
        "auto_merge_enabled": False,
    }
    if not execute or decision.allowed_action in ("blocked", "none"):
        return result

    assert decision.worktree_branch is not None
    assert git_root is not None
    if issue_number is not None and issue_title and issue_repo_matched:
        title = f"[Garden] #{issue_number}: {issue_title}"[:120]
    elif issue_title:
        title = f"[Garden] {issue_title}"[:120]
    else:
        title = f"[Garden] {run_data.get('flow', 'run')}: {decision.run_id}"
    summary_file = run_dir / "summary.md"
    base_body = (
        summary_file.read_text(encoding="utf-8")
        if summary_file.is_file()
        else f"Automated Garden run `{decision.run_id}` (`{decision.verdict}`)."
    )
    if issue_repo_matched and issue_number is not None:
        if not re.search(rf"(?i)\bcloses\s+#{issue_number}\b", base_body):
            base_body = f"Closes #{issue_number}\n\n{base_body}"
    elif issue_url and issue_url not in base_body:
        base_body = f"Ref: {issue_url}\n\n{base_body}"
    push_proc = subprocess.run(
        ["git", "push", "-u", "origin", decision.worktree_branch],
        cwd=git_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if push_proc.returncode != 0:
        raise RuntimeError(
            f"git push failed: {push_proc.stderr.strip() or push_proc.stdout.strip()}"
        )

    with tempfile.TemporaryDirectory() as tmp_dir:
        pr_body_path = Path(tmp_dir) / "pr-body.md"
        pr_body_path.write_text(base_body, encoding="utf-8")
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
                "--body-file",
                str(pr_body_path),
            ],
            cwd=git_root,
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
    result["issue_linked"] = issue_repo_matched

    if decision.allowed_action == "auto_merge":
        # Never bypass branch protection (--admin is intentionally never used)
        merge_proc = subprocess.run(
            ["gh", "pr", "merge", pr_url, "--auto", "--merge"],
            cwd=git_root,
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
