#!/usr/bin/env python3
"""Bounded exhaustive formal invariant verifier for hiraoku-software-garden core.

Directly exercises the real production functions in `src/garden/engine.py`,
`src/garden/artifacts.py`, and `src/garden/definition.py` across every reachable
state combination within explicit bounds, emitting minimal counterexamples in
plain code terms whenever an invariant breaks.
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from garden.adapters.base import AgentResponse, AgentUsage
from garden.artifacts import resolve_report_kind
from garden.checks import CheckExecutionResult
from garden.definition import (
    MODE_RANK,
    DefinitionError,
    load_definition_bytes,
)
from garden.engine import VERDICT_EXIT_CODES, run_flow
from garden.guard import GuardResult, match_glob
from garden.publish import evaluate_reflect_gate
from garden.spec import ApprovedSpecRecord, SpecDocument, TrustAnchorResult

PSTACK_SHA = "7a583913eb1b8d1a0ee76753d1a2cf072e7b54f9"
SPEC_DOC = SpecDocument(
    path="docs/specs/app.md",
    spec_id="app",
    version=1,
    status="approved",
    title="Formal Bound Spec",
    owner="@verifier",
    acceptance_criteria={"AC-001": "c1", "AC-002": "c2"},
    invariants={"INV-001": "i1"},
)


def _make_definition(
    *,
    default_mode: str = "auto_merge",
    scope: str = "all_specs",
    delegation_max_retries: int = 2,
    step_max_retries: int = 2,
    on_fail: str = "fix_and_retry",
    on_inconclusive: str = "escalate",
    prime_c2_cache_before_fix: bool = False,
    gate_checks: tuple[str, ...] = ("c1", "c2"),
) -> Any:
    if on_fail == "fix_and_retry" and default_mode == "observe_only":
        default_mode = "propose_pr"
    allowed = '["src/**"]' if default_mode != "observe_only" else "[]"
    steps_parts: list[str] = []

    if prime_c2_cache_before_fix:
        steps_parts.append(
            "[[flows.target.steps]]\n"
            'id = "s_c2_pre"\n'
            'type = "check"\n'
            'check = "c2"\n'
            'on_fail = "escalate"\n'
            'on_inconclusive = "escalate"\n'
        )

    fix_line = 'fix_step = "fix_s"\n' if on_fail == "fix_and_retry" else ""
    steps_parts.append(
        "[[flows.target.steps]]\n"
        'id = "s_c1"\n'
        'type = "check"\n'
        'check = "c1"\n'
        f'on_fail = "{on_fail}"\n'
        f"{fix_line}"
        f'on_inconclusive = "{on_inconclusive}"\n'
        f"max_retries = {min(step_max_retries, delegation_max_retries)}\n"
    )
    if on_fail == "fix_and_retry":
        steps_parts.append(
            "[[flows.target.steps]]\n"
            'id = "fix_s"\n'
            'type = "agent"\n'
            'agent = "default"\n'
            "run_on_demand = true\n"
        )
    steps_parts.append(
        "[[flows.target.steps]]\n"
        'id = "final_gate"\n'
        'type = "gate"\n'
        f"require_checks = {json.dumps(list(gate_checks))}\n"
    )
    steps_parts.append(
        "[[flows.target.steps]]\n"
        'id = "rep"\n'
        'type = "report"\n'
        'kind = "auto"\n'
    )
    toml_text = (
        "schema_version = 1\n"
        '[template]\nname = "cli"\nversion = 1\n'
        f'[common_harness]\nname = "pstack"\nref = "{PSTACK_SHA}"\n'
        '[target_harness]\nname = "h"\nversion = 1\n'
        '[spec]\npaths = ["docs/specs/app.md"]\napproval_branch = "main"\n'
        f'[delegation]\nmode = "{default_mode}"\nallowed_paths = {allowed}\n'
        f"forbidden_paths = []\nmax_retries = {delegation_max_retries}\n"
        "max_duration_sec = 60\nmax_cost_usd = 5.0\n"
        '[checks.c1]\nkind = "check"\ncommand = ["true"]\ncovers = ["AC-001"]\n'
        '[checks.c2]\nkind = "check"\ncommand = ["true"]\ncovers = ["AC-002", "INV-001"]\n'
        '[agents.default]\nadapter = "mock"\ncommand = ["true"]\nmax_cost_usd = 2.0\n'
        f'[flows.target]\nscope = "{scope}"\n'
        + "\n".join(steps_parts)
    )
    return load_definition_bytes(toml_text.encode("utf-8"))


class _ScenarioHarness:
    """Reusable in-memory boundary harness so 10,000+ engine runs execute in ~1-2s."""

    def __init__(self, fake_root: Path) -> None:
        self.fake_root = fake_root
        self.anchor: TrustAnchorResult | None = None
        self.c1_outcomes: tuple[str, ...] = ("passed",)
        self.c2_outcomes: tuple[str, ...] = ("passed",)
        self.agent_plan: tuple[tuple[str, bool, bool], ...] = (("completed", True, False),)
        self.c1_idx = 0
        self.c2_idx = 0
        self.ag_idx = 0
        self.worktree_gen = 0
        self.check_exec_gen: dict[str, int] = {}
        self.events: list[str] = []
        self.recorded_steps: list[dict[str, Any]] = []

    def reset(
        self,
        *,
        definition: Any,
        specs_approved: bool,
        eval_changed: bool,
        force_observe_only: bool,
        c1_outcomes: tuple[str, ...],
        c2_outcomes: tuple[str, ...],
        agent_plan: tuple[tuple[str, bool, bool], ...],
    ) -> None:
        spec_rec = ApprovedSpecRecord(
            spec_id="app",
            path="docs/specs/app.md",
            version=1,
            status="approved",
            approval_branch="main",
            approval_commit="a" * 40,
            blob_sha="b" * 40,
            modified_in_head=force_observe_only,
            document=SPEC_DOC,
        )
        self.anchor = TrustAnchorResult(
            active_definition=definition,
            approval_branch="main",
            approval_commit="a" * 40,
            approved_definition_blob_sha="c" * 40,
            head_definition_blob_sha=("d" * 40 if eval_changed else "c" * 40),
            evaluation_criteria_changed=eval_changed,
            evaluation_change_reasons=("diff",) if eval_changed else (),
            specs_approved=specs_approved,
            spec_failure_reason=None if specs_approved else "spec_not_approved",
            specs=(spec_rec,) if specs_approved else (),
            force_observe_only=force_observe_only,
        )
        self.c1_outcomes = c1_outcomes
        self.c2_outcomes = c2_outcomes
        self.agent_plan = agent_plan
        self.c1_idx = 0
        self.c2_idx = 0
        self.ag_idx = 0
        self.worktree_gen = 0
        self.check_exec_gen = {}
        self.events = []
        self.recorded_steps = []

    def fake_trust_anchor(self, _root: Path, **_kwargs: Any) -> TrustAnchorResult:
        assert self.anchor is not None
        return self.anchor

    def fake_run_check(self, check_cfg, *, worktree, run_id, step_id, step_dir):
        if check_cfg.name == "c1":
            status = self.c1_outcomes[min(self.c1_idx, len(self.c1_outcomes) - 1)]
            self.c1_idx += 1
        else:
            status = self.c2_outcomes[min(self.c2_idx, len(self.c2_outcomes) - 1)]
            self.c2_idx += 1
        self.check_exec_gen[check_cfg.name] = self.worktree_gen
        self.events.append(f"check:{check_cfg.name}@{step_id}->gen{self.worktree_gen}:{status}")
        return CheckExecutionResult(
            check_name=check_cfg.name,
            kind=check_cfg.kind,
            status=status,
            reason="ok" if status == "passed" else ("exit_code_inconclusive" if status == "inconclusive" else "assertion_failed"),
            exit_code=0 if status == "passed" else (75 if status == "inconclusive" else 1),
            duration_sec=0.01,
            stdout_path=str(step_dir / "stdout.log"),
            stderr_path=str(step_dir / "stderr.log"),
            evidence_dir=str(step_dir / "evidence"),
        )

    def fake_mock_adapter(self, agent_cfg, request, *, worktree, step_dir):
        outcome, _guard_ok, mutates = self.agent_plan[min(self.ag_idx, len(self.agent_plan) - 1)]
        self.ag_idx += 1
        if mutates:
            self.worktree_gen += 1
        self.events.append(f"agent:{outcome}:mutates={mutates}->gen{self.worktree_gen}")
        q = {"prompt": "decide?"} if outcome == "needs_spec_decision" else None
        return (
            AgentResponse(
                outcome=outcome,
                summary=f"stub:{outcome}",
                changed_files=("src/app.py",) if mutates else (),
                question=q,
                usage=AgentUsage(input_tokens=10, output_tokens=5, cost_usd=0.01),
            ),
            0 if outcome == "completed" else 1,
        )

    def fake_guard(self, worktree, target_commit, delegation, **kwargs):
        idx = max(0, self.ag_idx - 1)
        _outcome, guard_ok, mutates = self.agent_plan[min(idx, len(self.agent_plan) - 1)]
        self.events.append(f"guard:ok={guard_ok}:changed={mutates}")
        return GuardResult(
            ok=guard_ok,
            violations=() if guard_ok else ("forbidden_path_violation",),
            changed_files=("src/app.py",) if mutates else (),
        )

    def fake_write_step(self, step_dir, step_data):
        self.recorded_steps.append(step_data)
        return step_dir / "step.json"

    def fake_write_run(self, run_dir, run_data, *, requested_report_kind="auto"):
        return run_dir / "run.json", run_dir / "summary.md"


def check_engine_invariants() -> dict[str, Any]:
    states_checked = 0
    violations: dict[str, list[dict[str, Any]]] = {
        "INV-VERDICT-SOUNDNESS": [],
        "INV-HARVEST-TRUST": [],
        "INV-CACHE-FRESHNESS": [],
        "INV-RETRY-BOUND": [],
        "INV-OBSERVE-ONLY-NO-AGENT": [],
    }

    def record_violation(inv: str, desc: str, trace: list[str], meta: dict[str, Any]) -> None:
        if len(violations[inv]) < 3:
            violations[inv].append({"description": desc, "trace": trace, "meta": meta})

    with tempfile.TemporaryDirectory() as tmp:
        fake_root = Path(tmp)
        h = _ScenarioHarness(fake_root)
        with (
            patch("garden.engine.repo_root", return_value=fake_root),
            patch("garden.engine.list_remotes", return_value={"origin": "https://example.invalid/r.git"}),
            patch("garden.engine.head_commit", return_value="e" * 40),
            patch("garden.engine.is_dirty", return_value=False),
            patch("garden.engine.snapshot_untracked_files", return_value={}),
            patch("garden.engine.verify_trust_anchor", side_effect=h.fake_trust_anchor),
            patch("garden.engine.run_check", side_effect=h.fake_run_check),
            patch("garden.engine.run_mock_adapter", side_effect=h.fake_mock_adapter),
            patch("garden.engine.verify_post_agent_guard", side_effect=h.fake_guard),
            patch("garden.engine.write_step_json", side_effect=h.fake_write_step),
            patch("garden.engine.write_run_artifacts", side_effect=h.fake_write_run),
        ):
            for (
                specs_approved,
                eval_changed,
                force_obs,
                scope,
                on_fail,
                on_inconclusive,
                max_retries,
                prime_cache,
            ) in itertools.product(
                (True, False),
                (True, False),
                (True, False),
                ("all_specs", "flow_checks"),
                ("abort", "escalate", "retry", "fix_and_retry"),
                ("escalate", "retry", "abort"),
                (0, 1, 2),
                (False, True),
            ):
                # Cache-priming step before fix only adds states when fix_and_retry can mutate worktree
                if prime_cache and (on_fail != "fix_and_retry" or scope != "all_specs" or max_retries == 0):
                    continue
                definition = _make_definition(
                    default_mode="auto_merge",
                    scope=scope,
                    delegation_max_retries=max_retries,
                    step_max_retries=max_retries,
                    on_fail=on_fail,
                    on_inconclusive=on_inconclusive,
                    prime_c2_cache_before_fix=prime_cache,
                    gate_checks=("c1", "c2") if scope == "all_specs" else ("c1",),
                )

                c1_seqs = [
                    ("passed",),
                    ("failed", "passed"),
                    ("failed", "failed", "passed"),
                    ("failed", "failed", "failed"),
                    ("inconclusive", "passed"),
                    ("inconclusive", "inconclusive", "inconclusive"),
                ]
                c2_seqs = [("passed",), ("failed",), ("inconclusive",)]
                agent_plans = (
                    [
                        (("completed", True, True),),
                        (("completed", True, False),),
                        (("completed", False, True),),
                        (("needs_spec_decision", True, False),),
                        (("failed", True, False),),
                    ]
                    if on_fail == "fix_and_retry"
                    else [(("completed", True, False),)]
                )

                for c1_seq, c2_seq, ag_plan in itertools.product(c1_seqs, c2_seqs, agent_plans):
                    if not specs_approved and (
                        c1_seq != ("passed",) or c2_seq != ("passed",) or ag_plan != agent_plans[0]
                    ):
                        continue
                    states_checked += 1
                    h.reset(
                        definition=definition,
                        specs_approved=specs_approved,
                        eval_changed=eval_changed,
                        force_observe_only=force_obs,
                        c1_outcomes=c1_seq,
                        c2_outcomes=c2_seq,
                        agent_plan=ag_plan,
                    )
                    res = run_flow(fake_root, "target", force=True, use_worktree=False)
                    run_data = res.run_data
                    verdict = res.verdict
                    events = list(h.events)
                    meta = {
                        "specs_approved": specs_approved,
                        "eval_changed": eval_changed,
                        "force_obs": force_obs,
                        "scope": scope,
                        "on_fail": on_fail,
                        "on_inconclusive": on_inconclusive,
                        "max_retries": max_retries,
                        "prime_cache": prime_cache,
                        "c1_seq": c1_seq,
                        "c2_seq": c2_seq,
                        "agent_plan": ag_plan,
                        "verdict": verdict,
                    }

                    if res.exit_code != VERDICT_EXIT_CODES[verdict]:
                        record_violation(
                            "INV-VERDICT-SOUNDNESS",
                            f"exit_code {res.exit_code} does not match verdict {verdict}",
                            events,
                            meta,
                        )
                    if not specs_approved and verdict != "NEEDS_HUMAN":
                        record_violation(
                            "INV-VERDICT-SOUNDNESS",
                            f"unapproved spec produced {verdict} instead of NEEDS_HUMAN",
                            events,
                            meta,
                        )
                    if any("guard:ok=False" in e for e in events) and verdict != "NEEDS_HUMAN":
                        record_violation(
                            "INV-VERDICT-SOUNDNESS",
                            f"post-agent guard violation produced {verdict} instead of NEEDS_HUMAN",
                            events,
                            meta,
                        )
                    if any("agent:needs_spec_decision" in e for e in events) and verdict != "NEEDS_HUMAN":
                        record_violation(
                            "INV-VERDICT-SOUNDNESS",
                            f"needs_spec_decision produced {verdict} instead of NEEDS_HUMAN",
                            events,
                            meta,
                        )
                    any_crit_inconc = any(
                        v.get("verdict") == "inconclusive"
                        for v in (run_data.get("criteria_results") or {}).values()
                    )
                    if any_crit_inconc and verdict == "SATISFIED":
                        record_violation(
                            "INV-VERDICT-SOUNDNESS",
                            "inconclusive criterion still yielded SATISFIED",
                            events,
                            meta,
                        )
                    if verdict == "SATISFIED":
                        for cid, info in (run_data.get("criteria_results") or {}).items():
                            if info.get("verdict") not in ("satisfied", "not_evaluated"):
                                record_violation(
                                    "INV-VERDICT-SOUNDNESS",
                                    f"SATISFIED while criterion {cid} is {info.get('verdict')}",
                                    events,
                                    meta,
                                )

                    report_kind = resolve_report_kind(run_data, "auto")
                    eff_mode = (run_data.get("effective_delegation") or {}).get("mode")
                    if eval_changed and eff_mode == "auto_merge":
                        record_violation(
                            "INV-HARVEST-TRUST",
                            "evaluation_criteria_changed=True allowed auto_merge mode",
                            events,
                            meta,
                        )
                    if force_obs and eff_mode != "observe_only":
                        record_violation(
                            "INV-HARVEST-TRUST",
                            f"force_observe_only=True allowed mode={eff_mode}",
                            events,
                            meta,
                        )
                    if report_kind == "harvest" and (
                        verdict != "SATISFIED" or scope != "all_specs" or eval_changed
                    ):
                        record_violation(
                            "INV-HARVEST-TRUST",
                            f"illegal harvest report emitted (verdict={verdict}, scope={scope}, eval_changed={eval_changed})",
                            events,
                            meta,
                        )

                    gate_records = [s for s in h.recorded_steps if s.get("type") == "gate"]
                    if gate_records and verdict == "SATISFIED":
                        latest_gen = h.worktree_gen
                        for cname in gate_records[-1]["checks"].keys():
                            exec_gen = h.check_exec_gen.get(cname, -1)
                            if exec_gen != latest_gen:
                                record_violation(
                                    "INV-CACHE-FRESHNESS",
                                    f"gate passed using stale check '{cname}' from worktree gen {exec_gen} != latest gen {latest_gen}",
                                    events,
                                    meta,
                                )

                    if run_data["retries_used"] > max_retries:
                        record_violation(
                            "INV-RETRY-BOUND",
                            f"retries_used={run_data['retries_used']} exceeded max_retries={max_retries}",
                            events,
                            meta,
                        )

                    if eff_mode == "observe_only" and any(e.startswith("agent:") for e in events):
                        record_violation(
                            "INV-OBSERVE-ONLY-NO-AGENT",
                            "fix_and_retry invoked an agent step despite effective delegation mode being observe_only",
                            events,
                            meta,
                        )

    return {"states_checked": states_checked, "violations": violations}


def check_delegation_monotonicity() -> dict[str, Any]:
    """Exhaustively check INV-DELEGATION-MONOTONICITY over bounded delegation overrides."""
    from garden.definition import DelegationConfig, _merge_strict_delegation

    states_checked = 0
    violations: list[dict[str, Any]] = []

    modes = ("observe_only", "propose_pr", "auto_merge")
    allowed_sets = ((), ("src/**",), ("src/**", "tests/**"))
    retries_vals = (0, 1, 2)
    cost_vals = (0.0, 2.0, 5.0)
    req_check_sets = ((), ("c1",), ("c1", "c2"))
    valid_checks = {"c1", "c2"}

    for (
        base_mode,
        flow_mode,
        base_allowed,
        flow_allowed,
        base_ret,
        flow_ret,
        base_cost,
        flow_cost,
        base_req,
        flow_req,
    ) in itertools.product(
        modes,
        modes,
        allowed_sets,
        allowed_sets,
        retries_vals,
        retries_vals,
        cost_vals,
        cost_vals,
        req_check_sets,
        req_check_sets,
    ):
        if base_mode == "observe_only" and base_allowed:
            continue
        states_checked += 1
        base = DelegationConfig(
            mode=base_mode,
            allowed_paths=base_allowed,
            forbidden_paths=(".garden/**", "docs/specs/app.md"),
            max_retries=base_ret,
            max_duration_sec=60,
            max_cost_usd=base_cost,
            require_checks=base_req,
        )
        override = {
            "mode": flow_mode,
            "allowed_paths": list(flow_allowed),
            "forbidden_paths": [],
            "max_retries": flow_ret,
            "max_duration_sec": 60,
            "max_cost_usd": flow_cost,
            "require_checks": list(flow_req),
        }
        should_reject = (
            MODE_RANK[flow_mode] > MODE_RANK[base_mode]
            or (flow_mode == "observe_only" and bool(flow_allowed))
            or (not set(flow_allowed).issubset(set(base_allowed)))
            or flow_ret > base_ret
            or flow_cost > base_cost
            or (not set(base_req).issubset(set(flow_req)))
        )
        try:
            eff = _merge_strict_delegation(base, override, "flows.f1.delegation", valid_checks)
        except DefinitionError:
            if not should_reject and len(violations) < 3:
                violations.append(
                    {
                        "description": "valid stricter delegation was unexpectedly rejected",
                        "meta": {
                            "base": (base_mode, base_allowed, base_ret, base_cost, base_req),
                            "flow": (flow_mode, flow_allowed, flow_ret, flow_cost, flow_req),
                        },
                    }
                )
            continue

        if should_reject and len(violations) < 3:
            violations.append(
                {
                    "description": "relaxed delegation was accepted by validator",
                    "meta": {
                        "base": (base_mode, base_allowed, base_ret, base_cost, base_req),
                        "flow": (flow_mode, flow_allowed, flow_ret, flow_cost, flow_req),
                    },
                }
            )
        if ".garden/**" not in eff.forbidden_paths or "docs/specs/app.md" not in eff.forbidden_paths:
            if len(violations) < 3:
                violations.append(
                    {
                        "description": "mandatory forbidden_paths missing from effective delegation",
                        "meta": {"forbidden_paths": eff.forbidden_paths},
                    }
                )

    return {"states_checked": states_checked, "violations": violations}


def check_guard_and_reflect_invariants() -> dict[str, Any]:
    """Exhaustively check INV-GUARD-PATH-SOUNDNESS and INV-REFLECT-GATE-SOUNDNESS."""
    guard_states = 0
    guard_violations: list[dict[str, Any]] = []

    patterns_and_oracle = [
        (".garden/**", lambda norm: norm.startswith(".garden/")),
        (".github/workflows/**", lambda norm: norm.startswith(".github/workflows/")),
        ("docs/specs/**", lambda norm: norm.startswith("docs/specs/")),
        ("src/**", lambda norm: norm.startswith("src/")),
        ("src/*.py", lambda norm: norm.startswith("src/") and "/" not in norm[4:] and norm.endswith(".py")),
    ]
    prefixes = ("", "./", "././")
    sample_paths = (
        ".garden/garden.toml",
        ".garden/runs/r1/run.json",
        "garden/garden.toml",
        "garden/cli.py",
        ".github/workflows/ci.yml",
        "github/workflows/ci.yml",
        "docs/specs/app.md",
        "docs/specs/sub/app.md",
        "docs/README.md",
        "src/a.py",
        "src/nested/b.py",
        "src/a.txt",
        "tests/test_a.py",
    )
    for (pat, oracle), pref, raw_p in itertools.product(
        patterns_and_oracle, prefixes, sample_paths
    ):
        guard_states += 1
        candidate = pref + raw_p
        actual = match_glob(candidate, pat)
        expected = oracle(raw_p)
        if actual != expected and len(guard_violations) < 3:
            guard_violations.append(
                {
                    "description": f"match_glob({candidate!r}, {pat!r}) returned {actual}, expected {expected}",
                    "meta": {"path": candidate, "pattern": pat, "actual": actual, "expected": expected},
                }
            )

    reflect_states = 0
    reflect_violations: list[dict[str, Any]] = []
    verdicts = ("SATISFIED", "UNSATISFIED", "INCONCLUSIVE", "NEEDS_HUMAN", "ABORTED")
    modes = ("observe_only", "propose_pr", "auto_merge")
    scopes = ("all_specs", "flow_checks")
    branches = (None, "garden/run-1")
    criteria_variants = (
        {"AC-001": {"verdict": "satisfied"}},
        {"AC-001": {"verdict": "satisfied"}, "INV-001": {"verdict": "not_evaluated"}},
        {"AC-001": {"verdict": "inconclusive"}},
        {},
    )
    for verdict, mode, eval_changed, scope, branch, crit in itertools.product(
        verdicts, modes, (False, True), scopes, branches, criteria_variants
    ):
        reflect_states += 1
        run_data = {
            "run_id": "r1",
            "verdict": verdict,
            "target": {"commit": "a" * 40, "evaluation_criteria_changed": eval_changed},
            "effective_delegation": {"mode": mode},
            "flow_scope": scope,
            "worktree_branch": branch,
            "criteria_results": crit,
        }
        decision = evaluate_reflect_gate(run_data)
        all_crit_ok = bool(crit) and all(v.get("verdict") == "satisfied" for v in crit.values())

        # Safety property 1: auto_merge only when every safety precondition holds
        if decision.allowed_action == "auto_merge":
            if not (
                verdict == "SATISFIED"
                and mode == "auto_merge"
                and branch is not None
                and not eval_changed
                and scope == "all_specs"
                and all_crit_ok
            ):
                if len(reflect_violations) < 3:
                    reflect_violations.append(
                        {
                            "description": "evaluate_reflect_gate allowed auto_merge without all safety preconditions",
                            "meta": run_data | {"decision": decision.to_dict()},
                        }
                    )

        # Safety property 2: non-SATISFIED or observe_only must always be blocked
        if (verdict != "SATISFIED" or mode == "observe_only") and decision.allowed_action != "blocked":
            if len(reflect_violations) < 3:
                reflect_violations.append(
                    {
                        "description": f"expected blocked for verdict={verdict}, mode={mode}, got {decision.allowed_action}",
                        "meta": run_data | {"decision": decision.to_dict()},
                    }
                )

    return {
        "guard_states_checked": guard_states,
        "reflect_states_checked": reflect_states,
        "guard_violations": guard_violations,
        "reflect_violations": reflect_violations,
    }


def main() -> int:
    started = time.monotonic()
    engine_res = check_engine_invariants()
    deleg_res = check_delegation_monotonicity()
    extra_res = check_guard_and_reflect_invariants()
    elapsed = round(time.monotonic() - started, 3)

    all_violations = dict(engine_res["violations"])
    all_violations["INV-DELEGATION-MONOTONICITY"] = deleg_res["violations"]
    all_violations["INV-GUARD-PATH-SOUNDNESS"] = extra_res["guard_violations"]
    all_violations["INV-REFLECT-GATE-SOUNDNESS"] = extra_res["reflect_violations"]
    failed = {k: v for k, v in all_violations.items() if v}

    total_states = (
        engine_res["states_checked"]
        + deleg_res["states_checked"]
        + extra_res["guard_states_checked"]
        + extra_res["reflect_states_checked"]
    )
    summary = {
        "verifier": "bounded-exhaustive-python-real-code",
        "python_version": sys.version.split()[0],
        "duration_sec": elapsed,
        "engine_scenarios_checked": engine_res["states_checked"],
        "delegation_combinations_checked": deleg_res["states_checked"],
        "guard_path_combinations_checked": extra_res["guard_states_checked"],
        "reflect_gate_combinations_checked": extra_res["reflect_states_checked"],
        "total_states_checked": total_states,
        "invariants": {
            name: ("FAILED" if viols else "PROVED_WITHIN_BOUNDS")
            for name, viols in all_violations.items()
        },
        "counterexamples": failed,
    }

    evidence_dir = os.environ.get("GARDEN_EVIDENCE_DIR")
    if evidence_dir:
        ev_path = Path(evidence_dir) / "formal.json"
        ev_path.parent.mkdir(parents=True, exist_ok=True)
        formal_payload = {
            "schema_version": 1,
            "tool": "python-exhaustive",
            "tool_version": sys.version.split()[0],
            "model_path": ".agents/skills/formal-verify-garden-engine/scripts/verify_invariants.py",
            "invariants": ["INV-001", "INV-002"],
            "assumptions": [
                "bounded engine transition space (max_retries<=2, 2 checks)",
                "bounded delegation override space (45,927 combinations)",
                "bounded glob path matrix (195 cases) and reflect gate matrix (480 cases)",
            ],
            "scope": f"bounded exhaustive real-code verification ({total_states} states)",
            "completed": True,
            "result": "counterexample_found" if failed else "proved",
            "counterexample": failed if failed else None,
        }
        ev_path.write_text(
            json.dumps(formal_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
