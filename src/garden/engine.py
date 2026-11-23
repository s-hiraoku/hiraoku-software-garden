"""Deterministic state-machine execution engine for Garden flows."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from garden import __version__ as CLI_VERSION
from garden.adapters import (
    AgentRequest,
    AgentResponse,
    run_command_adapter,
    run_mock_adapter,
)
from garden.artifacts import write_run_artifacts, write_step_json
from garden.checks import CheckExecutionResult, run_check
from garden.definition import (
    CheckConfig,
    DelegationConfig,
    DefinitionError,
    FlowConfig,
    GardenDefinition,
    StepConfig,
)
from garden.git_utils import (
    GitError,
    git_run,
    head_commit,
    is_dirty,
    list_remotes,
    repo_root,
)
from garden.guard import snapshot_untracked_files, verify_post_agent_guard
from garden.report import fetch_github_issue_comments, find_satisfied_marker
from garden.spec import TrustAnchorResult, verify_trust_anchor


VERDICT_EXIT_CODES = {
    "SATISFIED": 0,
    "UNSATISFIED": 1,
    "INCONCLUSIVE": 3,
    "NEEDS_HUMAN": 4,
    "ABORTED": 5,
}


@dataclass(frozen=True)
class EngineRunResult:
    exit_code: int
    verdict: str
    run_id: str
    run_dir: Path | None
    run_data: dict[str, Any]
    skipped_duplicate: bool = False


def compute_idempotency_key(
    *,
    canonical_remote: str,
    target_commit: str,
    trigger_event: str,
    trigger_id: str,
    flow_name: str,
    approved_definition_blob_sha: str | None,
    spec_blob_shas: list[str],
) -> str:
    payload = "\n".join(
        [
            canonical_remote,
            target_commit,
            trigger_event,
            trigger_id,
            flow_name,
            approved_definition_blob_sha or "",
            ",".join(sorted(spec_blob_shas)),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _find_existing_satisfied_run(
    runs_root: Path, idempotency_key: str
) -> tuple[Path, dict[str, Any]] | None:
    if not runs_root.is_dir():
        return None
    for run_json in sorted(runs_root.glob("*/run.json"), reverse=True):
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(data, dict)
            and data.get("idempotency_key") == idempotency_key
            and data.get("verdict") == "SATISFIED"
        ):
            return run_json.parent, data
    return None


def _canonical_remote(remotes: dict[str, str], root: Path) -> str:
    if "origin" in remotes:
        return remotes["origin"]
    if remotes:
        first_key = sorted(remotes.keys())[0]
        return remotes[first_key]
    return str(root)


def _referenced_active_checks(
    definition: GardenDefinition, flow: FlowConfig
) -> set[str]:
    names: set[str] = set()
    for step in flow.steps:
        if step.type == "check" and step.check:
            names.add(step.check)
        elif step.type == "gate":
            names.update(step.require_checks)
    return {
        name
        for name in names
        if name in definition.checks and definition.checks[name].status == "active"
    }


def _evaluate_criteria(
    definition: GardenDefinition,
    flow: FlowConfig,
    anchor: TrustAnchorResult,
    executed_checks: dict[str, CheckExecutionResult],
    *,
    stopped_early: bool = False,
) -> tuple[dict[str, dict[str, Any]], bool, bool]:
    all_spec_ids: list[str] = []
    for rec in anchor.specs:
        for cid in rec.document.all_criteria_ids:
            if cid not in all_spec_ids:
                all_spec_ids.append(cid)

    if flow.scope == "all_specs":
        eval_ids = set(all_spec_ids)
    else:
        eval_ids = set()
        for cname in _referenced_active_checks(definition, flow):
            eval_ids.update(definition.checks[cname].covers)

    results: dict[str, dict[str, Any]] = {}
    any_inconclusive = False
    any_unsatisfied = False

    for cid in all_spec_ids:
        if cid not in eval_ids:
            results[cid] = {
                "verdict": "not_evaluated",
                "checks": [],
                "note": "outside flow scope (flow_checks)",
            }
            continue

        covering_executed = [
            (cname, res)
            for cname, res in executed_checks.items()
            if definition.checks[cname].status == "active"
            and cid in definition.checks[cname].covers
        ]
        if not covering_executed:
            if stopped_early:
                results[cid] = {
                    "verdict": "not_evaluated",
                    "checks": [],
                    "note": "not executed because an earlier step halted the flow",
                }
            else:
                any_inconclusive = True
                results[cid] = {
                    "verdict": "inconclusive",
                    "checks": [],
                    "note": "no active check executed for this criterion",
                }
            continue

        check_names = [cname for cname, _ in covering_executed]
        statuses = [res.status for _, res in covering_executed]
        notes: list[str] = []
        for _, res in covering_executed:
            if res.formal and isinstance(res.formal, dict):
                scope_str = res.formal.get("scope")
                tool_str = res.formal.get("tool")
                if scope_str:
                    notes.append(f"{tool_str}: {scope_str}")
            elif res.detail and res.status != "passed":
                notes.append(res.detail)

        if "inconclusive" in statuses:
            any_inconclusive = True
            c_verdict = "inconclusive"
        elif "failed" in statuses:
            any_unsatisfied = True
            c_verdict = "unsatisfied"
        else:
            c_verdict = "satisfied"

        results[cid] = {
            "verdict": c_verdict,
            "checks": check_names,
            "note": "; ".join(notes) if notes else "なし",
        }

    return results, any_inconclusive, any_unsatisfied


def _setup_worktree(
    root: Path,
    run_id: str,
    target_commit: str,
    delegation_mode: str,
) -> tuple[Path, str | None]:
    wt_root = root / ".garden" / "worktrees"
    wt_root.mkdir(parents=True, exist_ok=True)
    wt_path = wt_root / run_id
    branch_name: str | None = None
    if delegation_mode == "observe_only":
        proc = git_run(root, "worktree", "add", "--detach", str(wt_path), target_commit)
    else:
        branch_name = f"garden/{run_id}"
        proc = git_run(
            root, "worktree", "add", "-b", branch_name, str(wt_path), target_commit
        )
    if proc.returncode != 0:
        raise GitError(
            f"failed to create isolated worktree: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return wt_path, branch_name


def _cleanup_worktree(
    root: Path, wt_path: Path, *, delete_branch: str | None = None
) -> None:
    git_run(root, "worktree", "remove", "--force", str(wt_path))
    if delete_branch:
        git_run(root, "branch", "-D", delete_branch)


def _commit_verified_worktree_changes(
    worktree_path: Path,
    *,
    run_id: str,
    target_commit: str,
    changed_paths: set[str],
) -> str | None:
    """Commit guard-approved agent changes onto the isolated worktree branch.

    Returns the verified commit SHA if the branch tip differs from target_commit
    with a non-empty tree diff; returns None if no changes remain.
    Raises GitError if committing fails unexpectedly.
    """
    if changed_paths:
        stage_paths = [
            p
            for p in sorted(changed_paths)
            if (worktree_path / p).exists()
            or git_run(worktree_path, "ls-files", "--error-unmatch", "--", p).returncode == 0
        ]
        if stage_paths:
            status_proc = git_run(
                worktree_path, "status", "--porcelain", "--", *stage_paths
            )
            if status_proc.returncode != 0:
                raise GitError(
                    f"git status failed in worktree: {status_proc.stderr.strip()}"
                )
            if status_proc.stdout.strip():
                add_proc = git_run(worktree_path, "add", "-A", "--", *stage_paths)
                if add_proc.returncode != 0:
                    raise GitError(
                        f"git add failed in worktree: {add_proc.stderr.strip()}"
                    )
                staged_diff = git_run(worktree_path, "diff", "--cached", "--quiet")
                if staged_diff.returncode == 1:
                    commit_proc = git_run(
                        worktree_path,
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "user.name=Software Garden",
                        "-c",
                        "user.email=garden@local.invalid",
                        "commit",
                        "-m",
                        f"garden({run_id}): apply verified changes",
                    )
                    if commit_proc.returncode != 0:
                        raise GitError(
                            "git commit failed in worktree: "
                            f"{commit_proc.stderr.strip() or commit_proc.stdout.strip()}"
                        )
                elif staged_diff.returncode not in (0, 1):
                    raise GitError(
                        f"git diff --cached failed: {staged_diff.stderr.strip()}"
                    )

    tip_sha = head_commit(worktree_path)
    if tip_sha == target_commit:
        return None
    diff_proc = git_run(worktree_path, "diff", "--quiet", target_commit, tip_sha)
    if diff_proc.returncode == 0:
        return None
    if diff_proc.returncode != 1:
        raise GitError(f"git diff failed in worktree: {diff_proc.stderr.strip()}")
    return tip_sha


def _apply_effective_delegation(
    base: DelegationConfig, anchor: TrustAnchorResult
) -> DelegationConfig:
    eff = base
    if anchor.force_observe_only:
        eff = replace(eff, mode="observe_only", allowed_paths=())
    elif anchor.evaluation_criteria_changed and eff.mode == "auto_merge":
        eff = replace(eff, mode="propose_pr")
    return eff


def resolve_flow_for_trigger(
    definition: GardenDefinition,
    *,
    event: str,
    action: str | None = None,
    label: str | None = None,
) -> tuple[str, str] | None:
    """Resolve (trigger_id, flow_name) from [[triggers]] for an incoming event."""
    for trig in definition.triggers:
        if trig.event != event:
            continue
        cond = trig.conditions
        actions = cond.get("actions")
        if isinstance(actions, list) and actions:
            if action not in actions:
                continue
        single_action = cond.get("action")
        if isinstance(single_action, str) and single_action:
            if action != single_action:
                continue
        req_label = cond.get("label")
        if isinstance(req_label, str) and req_label:
            if label != req_label:
                continue
        return trig.id, trig.flow
    return None


def run_flow(
    target: Path,
    flow_name: str,
    *,
    approval_branch: str = "main",
    trigger_event: str = "manual",
    trigger_id: str = "manual",
    trigger_action: str | None = None,
    trigger_label: str | None = None,
    issue_or_pr: int | None = None,
    force: bool = False,
    keep_worktree: bool = False,
    use_worktree: bool = True,
    include_proposed: bool = False,
    existing_comments: list[str] | None = None,
    check_github_markers: bool = False,
) -> EngineRunResult:
    started_dt = datetime.now(timezone.utc)
    started_mono = time.monotonic()

    root = repo_root(target)
    remotes = list_remotes(root)
    canonical_remote = _canonical_remote(remotes, root)
    target_commit = head_commit(root)
    dirty_before = is_dirty(root)

    # Preflight: load definition & verify approval trust anchor
    anchor = verify_trust_anchor(root, approval_branch=approval_branch)
    definition = anchor.active_definition
    if flow_name == "auto":
        resolved = resolve_flow_for_trigger(
            definition,
            event=trigger_event,
            action=trigger_action,
            label=trigger_label,
        )
        if resolved is None:
            raise DefinitionError(
                f"no matching [[triggers]] entry for event='{trigger_event}' action={trigger_action!r}"
            )
        trigger_id, flow_name = resolved
    if flow_name not in definition.flows:
        raise DefinitionError(f"unknown flow '{flow_name}'")
    flow = definition.flows[flow_name]
    eff_delegation = _apply_effective_delegation(flow.delegation, anchor)

    idempotency_key = compute_idempotency_key(
        canonical_remote=canonical_remote,
        target_commit=target_commit,
        trigger_event=trigger_event,
        trigger_id=trigger_id,
        flow_name=flow_name,
        approved_definition_blob_sha=anchor.approved_definition_blob_sha,
        spec_blob_shas=[rec.blob_sha for rec in anchor.specs],
    )

    runs_root = root / ".garden" / "runs"
    if not force:
        existing = _find_existing_satisfied_run(runs_root, idempotency_key)
        if existing is not None:
            existing_dir, existing_data = existing
            return EngineRunResult(
                exit_code=0,
                verdict="SATISFIED",
                run_id=str(existing_data["run_id"]),
                run_dir=existing_dir,
                run_data=existing_data,
                skipped_duplicate=True,
            )
        comments_to_check = list(existing_comments or [])
        if check_github_markers and issue_or_pr is not None:
            comments_to_check.extend(
                fetch_github_issue_comments(issue_or_pr, cwd=root)
            )
        if comments_to_check:
            marker = find_satisfied_marker(comments_to_check, idempotency_key)
            if marker is not None:
                return EngineRunResult(
                    exit_code=0,
                    verdict="SATISFIED",
                    run_id=marker.get("run_id", "github-marker"),
                    run_dir=None,
                    run_data={
                        "schema_version": 1,
                        "run_id": marker.get("run_id", "github-marker"),
                        "idempotency_key": idempotency_key,
                        "verdict": "SATISFIED",
                        "skipped_source": "github_comment_marker",
                        "marker": marker,
                    },
                    skipped_duplicate=True,
                )

    stamp = started_dt.strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{idempotency_key[:8]}"
    run_dir = runs_root / run_id
    steps_root = run_dir / "steps"
    steps_root.mkdir(parents=True, exist_ok=True)

    versions_block = {
        "cli": CLI_VERSION,
        "template": {
            "name": definition.template.name,
            "version": definition.template.version,
        },
        "common_harness": {
            "name": definition.common_harness.name,
            "ref": definition.common_harness.ref,
        },
        "target_harness": {
            "name": definition.target_harness.name,
            "version": definition.target_harness.version,
        },
        "approved_definition_blob_sha": anchor.approved_definition_blob_sha,
        "head_definition_blob_sha": anchor.head_definition_blob_sha,
        "specs": [
            {
                "spec_id": rec.spec_id,
                "path": rec.path,
                "version": rec.version,
                "approval_branch": rec.approval_branch,
                "approval_commit": rec.approval_commit,
                "blob_sha": rec.blob_sha,
                "modified_in_head": rec.modified_in_head,
            }
            for rec in anchor.specs
        ],
    }

    def _finalize(
        *,
        verdict: str,
        step_records: list[str],
        criteria_results: dict[str, dict[str, Any]],
        retries_used: int,
        usage_totals: dict[str, Any],
        escalation: dict[str, Any] | None,
        requested_report_kind: str,
        worktree_branch: str | None = None,
        verified_commit: str | None = None,
    ) -> EngineRunResult:
        finished_dt = datetime.now(timezone.utc)
        duration_sec = round(time.monotonic() - started_mono, 3)
        run_data: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "idempotency_key": idempotency_key,
            "trigger": {
                "event": trigger_event,
                "id": trigger_id,
                "ref": target_commit,
                "issue_or_pr": issue_or_pr,
            },
            "target": {
                "remote": canonical_remote,
                "commit": target_commit,
                "dirty": dirty_before,
                "evaluation_criteria_changed": anchor.evaluation_criteria_changed,
                "evaluation_change_reasons": list(anchor.evaluation_change_reasons),
            },
            "versions": versions_block,
            "flow": flow_name,
            "flow_scope": flow.scope,
            "effective_delegation": {
                "mode": eff_delegation.mode,
                "max_retries": eff_delegation.max_retries,
                "max_duration_sec": eff_delegation.max_duration_sec,
                "max_cost_usd": eff_delegation.max_cost_usd,
            },
            "worktree_branch": worktree_branch,
            "verified_commit": verified_commit,
            "started_at": started_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_sec": duration_sec,
            "retries_used": retries_used,
            "usage": usage_totals,
            "verdict": verdict,
            "criteria_results": criteria_results,
            "escalation": escalation,
            "steps": step_records,
        }
        write_run_artifacts(
            run_dir, run_data, requested_report_kind=requested_report_kind
        )
        return EngineRunResult(
            exit_code=VERDICT_EXIT_CODES[verdict],
            verdict=verdict,
            run_id=run_id,
            run_dir=run_dir,
            run_data=run_data,
        )

    usage_totals: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }

    # Preflight gate: unapproved spec -> NEEDS_HUMAN
    if not anchor.specs_approved:
        return _finalize(
            verdict="NEEDS_HUMAN",
            step_records=[],
            criteria_results={},
            retries_used=0,
            usage_totals=usage_totals,
            escalation={
                "kind": "escalation",
                "reason": "spec_not_approved",
                "detail": anchor.spec_failure_reason,
            },
            requested_report_kind="escalation",
        )

    # Prepare worktree
    worktree_path = root
    worktree_branch: str | None = None
    created_worktree = False
    baseline_untracked: dict[str, str] = {}
    guard_ignore_prefixes: tuple[str, ...] = ()
    if use_worktree:
        try:
            worktree_path, worktree_branch = _setup_worktree(
                root, run_id, target_commit, eff_delegation.mode
            )
            created_worktree = True
        except GitError as error:
            return _finalize(
                verdict="INCONCLUSIVE",
                step_records=[],
                criteria_results={},
                retries_used=0,
                usage_totals=usage_totals,
                escalation={
                    "kind": "escalation",
                    "reason": "worktree_unavailable",
                    "detail": str(error),
                },
                requested_report_kind="escalation",
            )
    else:
        baseline_untracked = snapshot_untracked_files(worktree_path)
        guard_ignore_prefixes = (f".garden/runs/{run_id}/",)

    step_records: list[str] = []
    step_counter = 0
    check_cache: dict[str, CheckExecutionResult] = {}
    executed_checks: dict[str, CheckExecutionResult] = {}
    steps_by_id = {s.id: s for s in flow.steps}
    agent_changed_paths: set[str] = set()
    verified_commit: str | None = None

    retries_used = 0
    needs_human = False
    inconclusive_occurred = False
    failed_occurred = False
    aborted = False
    escalation: dict[str, Any] | None = None
    requested_report_kind = "auto"

    def _next_step_dir(label: str) -> tuple[Path, str]:
        nonlocal step_counter
        step_counter += 1
        dirname = f"{step_counter:02d}-{label}"
        sdir = steps_root / dirname
        return sdir, f"steps/{dirname}/step.json"

    def _exec_check(
        check_cfg: CheckConfig, step_label: str
    ) -> CheckExecutionResult:
        sdir, rel_step = _next_step_dir(step_label)
        res = run_check(
            check_cfg,
            worktree=worktree_path,
            run_id=run_id,
            step_id=step_label,
            step_dir=sdir,
        )
        write_step_json(
            sdir,
            {
                "step_id": step_label,
                "type": "check",
                "check": check_cfg.name,
                "kind": check_cfg.kind,
                "status": res.status,
                "reason": res.reason,
                "exit_code": res.exit_code,
                "duration_sec": res.duration_sec,
                "detail": res.detail,
                "formal": res.formal,
            },
        )
        step_records.append(rel_step)
        check_cache[check_cfg.name] = res
        if check_cfg.status == "active":
            executed_checks[check_cfg.name] = res
        return res

    def _exec_agent(
        agent_step: StepConfig, context: dict[str, Any]
    ) -> tuple[AgentResponse, bool]:
        nonlocal needs_human, escalation
        assert agent_step.agent is not None
        agent_cfg = definition.agents[agent_step.agent]
        sdir, rel_step = _next_step_dir(agent_step.id)

        remaining_budget = max(0.0, eff_delegation.max_cost_usd - usage_totals["cost_usd"])
        effective_step_cost_cap = (
            min(agent_cfg.max_cost_usd, remaining_budget)
            if agent_cfg.max_cost_usd is not None
            else remaining_budget
        )

        request = AgentRequest(
            run_id=run_id,
            step_id=agent_step.id,
            purpose=agent_step.purpose,
            skill=agent_step.skill,
            worktree_path=str(worktree_path.resolve()),
            specs=versions_block["specs"],
            delegation={
                "mode": eff_delegation.mode,
                "allowed_paths": list(eff_delegation.allowed_paths),
                "forbidden_paths": list(eff_delegation.forbidden_paths),
                "max_cost_usd": effective_step_cost_cap,
            },
            context=context,
        )

        if agent_cfg.adapter == "mock":
            response, exit_code = run_mock_adapter(
                agent_cfg, request, worktree=worktree_path, step_dir=sdir
            )
        else:
            response, exit_code = run_command_adapter(
                agent_cfg, request, worktree=worktree_path, step_dir=sdir
            )

        usage_totals["input_tokens"] += response.usage.input_tokens
        usage_totals["output_tokens"] += response.usage.output_tokens
        usage_totals["cost_usd"] = round(
            usage_totals["cost_usd"] + response.usage.cost_usd, 6
        )

        elapsed = time.monotonic() - started_mono
        guard_res = verify_post_agent_guard(
            worktree_path,
            target_commit,
            eff_delegation,
            step_cost_usd=response.usage.cost_usd,
            agent_max_cost_usd=agent_cfg.max_cost_usd,
            cumulative_cost_usd=usage_totals["cost_usd"],
            elapsed_sec=elapsed,
            baseline_untracked=baseline_untracked,
            ignore_prefixes=guard_ignore_prefixes,
        )

        write_step_json(
            sdir,
            {
                "step_id": agent_step.id,
                "type": "agent",
                "agent": agent_cfg.name,
                "adapter": agent_cfg.adapter,
                "outcome": response.outcome,
                "exit_code": exit_code,
                "summary": response.summary,
                "guard_ok": guard_res.ok,
                "guard_violations": list(guard_res.violations),
                "changed_files": list(guard_res.changed_files),
                "usage": response.usage.to_dict(),
            },
        )
        step_records.append(rel_step)

        if guard_res.changed_files:
            # Invalidate cached check results whenever files changed
            check_cache.clear()

        if not guard_res.ok:
            needs_human = True
            escalation = {
                "kind": "escalation",
                "reason": "delegation_violation",
                "detail": "; ".join(guard_res.violations),
            }
            return response, False

        if response.outcome == "needs_spec_decision":
            needs_human = True
            escalation = {
                "kind": "spec_question",
                "reason": "needs_spec_decision",
                "detail": response.summary,
                "question": response.question,
            }
            return response, False

        if response.outcome != "completed":
            needs_human = True
            escalation = {
                "kind": "escalation",
                "reason": f"agent_{response.outcome}",
                "detail": response.summary,
            }
            return response, False

        if guard_res.changed_files:
            agent_changed_paths.update(guard_res.changed_files)
        return response, True

    try:
        for step in flow.steps:
            if step.run_on_demand:
                continue

            if time.monotonic() - started_mono > eff_delegation.max_duration_sec:
                needs_human = True
                escalation = {
                    "kind": "escalation",
                    "reason": "duration_limit_exceeded",
                    "detail": f"flow exceeded max_duration_sec ({eff_delegation.max_duration_sec}s)",
                }
                break

            if step.type == "check":
                assert step.check is not None
                check_cfg = definition.checks[step.check]
                if check_cfg.status == "proposed" and not include_proposed:
                    continue

                step_retry_cap = (
                    step.max_retries
                    if step.max_retries is not None
                    else eff_delegation.max_retries
                )
                step_retries = 0
                stop_flow = False

                while True:
                    res = _exec_check(check_cfg, step.id)
                    if check_cfg.status == "proposed":
                        # Proposed checks never block mandatory flow progression
                        break

                    if res.status == "passed":
                        break

                    if res.status == "inconclusive":
                        if (
                            step.on_inconclusive == "retry"
                            and step_retries < step_retry_cap
                            and retries_used < eff_delegation.max_retries
                        ):
                            step_retries += 1
                            retries_used += 1
                            continue
                        if step.on_inconclusive == "abort":
                            aborted = True
                            escalation = {
                                "kind": "escalation",
                                "reason": f"aborted_on_inconclusive:{step.id}",
                                "detail": res.detail,
                            }
                            stop_flow = True
                            break
                        inconclusive_occurred = True
                        escalation = {
                            "kind": "escalation",
                            "reason": res.reason,
                            "detail": res.detail,
                        }
                        stop_flow = True
                        break

                    # res.status == "failed"
                    if step.on_fail == "abort":
                        aborted = True
                        escalation = {
                            "kind": "escalation",
                            "reason": f"aborted_on_fail:{step.id}",
                            "detail": res.detail,
                        }
                        stop_flow = True
                        break
                    if step.on_fail == "escalate":
                        failed_occurred = True
                        escalation = {
                            "kind": "escalation",
                            "reason": res.reason,
                            "detail": res.detail,
                        }
                        stop_flow = True
                        break
                    if step.on_fail == "retry":
                        if (
                            step_retries < step_retry_cap
                            and retries_used < eff_delegation.max_retries
                        ):
                            step_retries += 1
                            retries_used += 1
                            continue
                        failed_occurred = True
                        escalation = {
                            "kind": "escalation",
                            "reason": "retry_limit_reached",
                            "detail": res.detail,
                        }
                        stop_flow = True
                        break
                    if step.on_fail == "fix_and_retry":
                        if eff_delegation.mode == "observe_only":
                            needs_human = True
                            escalation = {
                                "kind": "escalation",
                                "reason": "observe_only_fix_disallowed",
                                "detail": (
                                    "cannot invoke fix_and_retry agent while effective "
                                    "delegation mode is observe_only"
                                ),
                            }
                            stop_flow = True
                            break
                        if (
                            step_retries < step_retry_cap
                            and retries_used < eff_delegation.max_retries
                        ):
                            assert step.fix_step is not None
                            fix_step_cfg = steps_by_id[step.fix_step]
                            step_retries += 1
                            retries_used += 1
                            _, agent_ok = _exec_agent(
                                fix_step_cfg,
                                {
                                    "failed_step_id": step.id,
                                    "failed_check": check_cfg.name,
                                    "stderr_path": res.stderr_path,
                                    "evidence_dir": res.evidence_dir,
                                },
                            )
                            if not agent_ok:
                                stop_flow = True
                                break
                            continue
                        failed_occurred = True
                        escalation = {
                            "kind": "escalation",
                            "reason": "fix_retry_limit_reached",
                            "detail": res.detail,
                        }
                        stop_flow = True
                        break

                if stop_flow:
                    break

            elif step.type == "agent":
                if eff_delegation.mode == "observe_only" and step.purpose != "investigate":
                    needs_human = True
                    escalation = {
                        "kind": "escalation",
                        "reason": "observe_only_agent_disallowed",
                        "detail": (
                            f"cannot invoke agent step '{step.id}' (purpose={step.purpose}) "
                            "while effective delegation mode is observe_only"
                        ),
                    }
                    break
                _, agent_ok = _exec_agent(step, {})
                if not agent_ok:
                    break

            elif step.type == "gate":
                gate_summary: dict[str, dict[str, Any]] = {}
                for cname in step.require_checks:
                    check_cfg = definition.checks[cname]
                    if cname in check_cache:
                        res = check_cache[cname]
                        reused = True
                    else:
                        res = _exec_check(check_cfg, f"{step.id}-{cname}")
                        reused = False
                    gate_summary[cname] = {
                        "status": res.status,
                        "reason": res.reason,
                        "reused": reused,
                    }
                    if res.status == "inconclusive":
                        inconclusive_occurred = True
                        escalation = escalation or {
                            "kind": "escalation",
                            "reason": res.reason,
                            "detail": res.detail,
                        }
                    elif res.status == "failed":
                        failed_occurred = True
                        escalation = escalation or {
                            "kind": "escalation",
                            "reason": res.reason,
                            "detail": res.detail,
                        }

                sdir, rel_step = _next_step_dir(step.id)
                write_step_json(
                    sdir,
                    {
                        "step_id": step.id,
                        "type": "gate",
                        "checks": gate_summary,
                    },
                )
                step_records.append(rel_step)
                if inconclusive_occurred or failed_occurred:
                    break

            elif step.type == "report":
                requested_report_kind = step.kind

        criteria_results, crit_inconclusive, crit_unsatisfied = _evaluate_criteria(
            definition,
            flow,
            anchor,
            executed_checks,
            stopped_early=(needs_human or inconclusive_occurred or failed_occurred or aborted),
        )

        # Strict verdict precedence: NEEDS_HUMAN > INCONCLUSIVE > UNSATISFIED > ABORTED > SATISFIED
        if needs_human:
            verdict = "NEEDS_HUMAN"
        elif aborted and not inconclusive_occurred and not failed_occurred:
            # Explicit abort takes effect unless superseded by higher-precedence issue
            verdict = "ABORTED"
        elif inconclusive_occurred or crit_inconclusive:
            verdict = "INCONCLUSIVE"
        elif failed_occurred or crit_unsatisfied:
            verdict = "UNSATISFIED"
        elif aborted:
            verdict = "ABORTED"
        else:
            verdict = "SATISFIED"

        recorded_branch: str | None = worktree_branch
        if verdict == "SATISFIED" and created_worktree and worktree_branch is not None:
            if agent_changed_paths:
                try:
                    verified_commit = _commit_verified_worktree_changes(
                        worktree_path,
                        run_id=run_id,
                        target_commit=target_commit,
                        changed_paths=agent_changed_paths,
                    )
                except GitError as error:
                    verdict = "INCONCLUSIVE"
                    verified_commit = None
                    escalation = {
                        "kind": "escalation",
                        "reason": "verified_commit_failed",
                        "detail": str(error),
                    }
            if verified_commit is None:
                recorded_branch = None
        elif not keep_worktree:
            recorded_branch = None

        return _finalize(
            verdict=verdict,
            step_records=step_records,
            criteria_results=criteria_results,
            retries_used=retries_used,
            usage_totals=usage_totals,
            escalation=escalation,
            requested_report_kind=requested_report_kind,
            worktree_branch=recorded_branch,
            verified_commit=verified_commit,
        )
    finally:
        if created_worktree and not keep_worktree:
            _cleanup_worktree(
                root,
                worktree_path,
                delete_branch=worktree_branch if verified_commit is None else None,
            )
