---
name: formal-verify-garden-engine
description: "Formally verify the safety invariants of hiraoku-software-garden's deterministic state-machine engine (`src/garden/engine.py`), harvest/report trust gate (`src/garden/artifacts.py`), and delegation strictness validator (`src/garden/definition.py`) via bounded exhaustive real-code exploration. Use whenever changing flow transitions, retry caps, check cache invalidation, verdict precedence, harvest gating, or delegation merging."
---

# Formally verify Garden engine & delegation invariants

This skill runs a bounded exhaustive verifier directly against the real Python production code in `src/garden/engine.py`, `src/garden/artifacts.py`, `src/garden/definition.py`, `src/garden/guard.py`, and `src/garden/publish.py` (59,442 reachable scenarios and combinations in ~2 seconds, zero external dependencies).

## Covered code & sync record

- **Last verified sync commit:** `ce0f91b`
- **Covered files and functions:**
  - `src/garden/engine.py`: `run_flow`, `_evaluate_criteria`, `_apply_effective_delegation`, `VERDICT_EXIT_CODES`
  - `src/garden/artifacts.py`: `resolve_report_kind`
  - `src/garden/definition.py`: `load_definition_bytes`, `_merge_strict_delegation`, `MODE_RANK`
  - `src/garden/guard.py`: `match_glob`, `_strip_dot_slash`
  - `src/garden/publish.py`: `evaluate_reflect_gate`

Check whether your change touches covered code before deciding to skip:

```sh
git diff --name-only ce0f91b -- src/garden/engine.py src/garden/artifacts.py src/garden/definition.py src/garden/guard.py src/garden/publish.py
```

If that command outputs any file, you **must** run the verifier below and confirm whether the verifier still matches the code at your new commit.

## Invariants checked (plain English)

1. **`INV-VERDICT-SOUNDNESS`** — *Property:* Any run with an unapproved spec, a post-agent guard violation, a `needs_spec_decision` outcome, or an unresolved `inconclusive` criterion never produces `SATISFIED`, and exit codes always match the final verdict. *Harm if broken:* Broken or unverified code silently passes CI and can be merged.
2. **`INV-HARVEST-TRUST`** — *Property:* Whenever `evaluation_criteria_changed` is true, `auto_merge` is never retained and `resolve_report_kind` never emits `harvest`; `harvest` is emitted only when `verdict == "SATISFIED"`, `scope == "all_specs"`, and `evaluation_criteria_changed == False`. *Harm if broken:* A PR that alters `.garden/garden.toml` or specs could self-approve or trigger premature harvest.
3. **`INV-CACHE-FRESHNESS`** — *Property:* After any `agent` step mutates files in the worktree, a subsequent `gate` step never passes using a stale pre-mutation cached check result; every required check in the final gate must have run at the latest worktree generation. *Harm if broken:* A fix breaks an earlier check while the gate silently reuses the stale pre-fix `passed` result.
4. **`INV-RETRY-BOUND`** — *Property:* `retries_used` never exceeds `effective_delegation.max_retries` across any combination of `on_fail`, `on_inconclusive`, and retry limits (`0..2`). *Harm if broken:* Runaway retry loops burn agent cost and time caps.
5. **`INV-DELEGATION-MONOTONICITY`** — *Property:* Flow-level `[flows.<name>.delegation]` overrides can never relax `mode`, expand `allowed_paths`, raise `max_retries` / `max_cost_usd`, remove `require_checks`, or drop mandatory `forbidden_paths` (`.garden/**` and `[spec].paths`). *Harm if broken:* A flow definition silently escalates its own write or auto-merge authority beyond repo policy.
6. **`INV-OBSERVE-ONLY-NO-AGENT`** — *Property:* When Trust Anchor downgrades `effective_delegation.mode` to `observe_only` at runtime (e.g., on a spec-modifying PR), `fix_and_retry` never invokes a mutating `agent` step. *Harm if broken:* The engine spends agent tokens/time and attempts edits on a read-only run instead of halting cleanly.
7. **`INV-GUARD-PATH-SOUNDNESS`** — *Property:* `match_glob` preserves leading dots on hidden directories (`.garden/**`, `.github/workflows/**`) and strips only leading `./`, never confusing `garden/` with `.garden/` or missing nested forbidden paths. *Harm if broken:* Forbidden paths slip past the guard or unrelated directories trigger false blocks.
8. **`INV-REFLECT-GATE-SOUNDNESS`** — *Property:* `evaluate_reflect_gate` permits `auto_merge` only when `verdict == "SATISFIED"`, `mode == "auto_merge"`, `worktree_branch` is present, `evaluation_criteria_changed == False`, `flow_scope == "all_specs"`, and every criterion is `satisfied`; non-`SATISFIED` or `observe_only` runs are always `blocked`. *Harm if broken:* Unverified or criteria-mutating changes could be queued for auto-merge.

## Tool choice, bounds, and what is NOT covered

- **Tool:** Pure Python 3.11+ bounded exhaustive state-space verifier ([scripts/verify_invariants.py](scripts/verify_invariants.py)) executing the real production functions directly with deterministic I/O boundary stubs (`run_check`, `run_mock_adapter`, `verify_post_agent_guard`, `verify_trust_anchor`).
- **Why this tool:** Neither a Java Runtime (for TLA+/TLC), Quint, Lean 4, nor Z3 is installed in this environment, and the Nursery repository intentionally maintains zero third-party dependencies. Running exhaustive exploration directly against the real Python functions avoids hand-model drift and runs in ~2 seconds on every developer machine and CI runner.
- **Explicit bounds:**
  - `engine.py` / `artifacts.py`: 12,840 combinations across `specs_approved ∈ {True, False}`, `eval_changed ∈ {True, False}`, `force_observe_only ∈ {True, False}`, `scope ∈ {all_specs, flow_checks}`, `on_fail ∈ {abort, escalate, retry, fix_and_retry}`, `on_inconclusive ∈ {escalate, retry, abort}`, `max_retries ∈ {0, 1, 2}`, cache-primed vs unprimed flows, 6 bounded check-outcome sequences, 3 second-check outcomes, and 5 agent/guard outcomes.
  - `definition.py`: 45,927 combinations across 3 modes, 3 `allowed_paths` sets, 3 `max_retries` values, 3 `max_cost_usd` values, and 3 `require_checks` sets.
  - `guard.py`: 195 `(path, pattern)` combinations across hidden directories (`.garden/**`, `.github/workflows/**`), lookalike directories (`garden/...`), `./` prefixes, and nested files.
  - `publish.py`: 480 combinations across 5 verdicts, 4 delegation modes, branch presence, `evaluation_criteria_changed`, `flow_scope`, and 3 criteria-completeness shapes.
- **Out of scope (not proved by this check):**
  - Subprocess I/O, Git CLI behavior, symlink/OS filesystem semantics, and real `git diff --no-renames` execution (covered instead by `tests/test_guard.py`, `tests/test_engine.py`, and `.agents/skills/verify-garden/`).
  - Multi-step flows with more than 2 distinct checks, `max_retries > 2`, or concurrent multi-process execution across separate OS processes.

## Run the verifier

```sh
.agents/skills/formal-verify-garden-engine/scripts/verify_invariants.py
```

Expected passing output: exit code `0`, `"total_states_checked": 59442`, all eight invariants `"PROVED_WITHIN_BOUNDS"`, and `"counterexamples": {}`. When `GARDEN_EVIDENCE_DIR` is set, it also writes `$GARDEN_EVIDENCE_DIR/formal.json` for native `kind = "formal_check"` integration.

## Interpreting failures (real bug vs verifier drift)

When the verifier exits `1`, inspect `"counterexamples"` in the JSON output. Each entry lists:
- `description`: the exact invariant violated.
- `trace`: the ordered sequence of check, agent, and guard events (`check:c2@s_c2_pre->gen0:passed`, `agent:completed:mutates=True->gen1`, ...).
- `meta`: the exact flow parameters and outcome sequences that reproduced it.

Distinguish real bugs from verifier drift:
- **Real bug:** The trace violates one of the eight plain-English promises above under current design rules (`docs/implementation/detailed-design.md`). Fix the production code (or report the finding without weakening the invariant).
- **Verifier drift:** You intentionally changed a function signature, patched symbol name, or new valid verdict/mode in `src/garden/`. Update `scripts/verify_invariants.py` to match the new contract, confirm the updated invariant catches a deliberately broken variant, and then advance **Last verified sync commit** above to the new commit SHA.

## Updating after intended changes

1. Update `scripts/verify_invariants.py` so every invariant and boundary stub remains true to the code.
2. Temporarily introduce a one-line regression (e.g., disable `check_cache.clear()` or relax a delegation check) and verify the script exits `1` with a counterexample; revert the scratch regression immediately.
3. Re-run `.agents/skills/formal-verify-garden-engine/scripts/verify_invariants.py` and confirm exit `0`.
4. Update **Last verified sync commit** in this `SKILL.md` only after confirming the verifier matches the code at the new commit.
