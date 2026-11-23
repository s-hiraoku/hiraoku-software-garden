# Flow execution and verdicts

Flow execution and verdicts lets an operator run a declarative Garden flow against a target repository and receive deterministic exit codes, structured Run Artifacts, HTML-marked summaries, and idempotent duplicate skipping.

## Sub-features

- `run-satisfied` executes `verify_all` (`check` + `gate` + `formal_check`), produces `SATISFIED` (exit `0`), and writes `.garden/runs/<run-id>/` with a `kind=harvest` marker.
- `run-duplicate-skip` skips an identical second run when a `SATISFIED` record already exists for the same `idempotency_key`, and `--force` overrides the skip.
- `run-unsatisfied` reports `UNSATISFIED` (exit `1`) on assertion failure without misclassifying unexecuted downstream criteria as `INCONCLUSIVE`.
- `run-inconclusive` reports `INCONCLUSIVE` (exit `3`) when a check exits with `75` (environment unavailable).
- `run-unapproved-spec` halts in Preflight with `NEEDS_HUMAN` (exit `4`) when the spec on `main` is `draft`.

## How to get to it (user POV)

- Run `python3 garden.py run <flow> --target <repo> [--force] [--json]` in a terminal.
- Inspect `$TARGET/.garden/runs/<run-id>/run.json` and `summary.md`.

## Driving it with verify-garden-harness

Preconditions:

- `launch` and `doctor` succeeded (`$GARDEN_VERIFY_DIR/sample-cli` is on `main` with `src/app.txt = OK` and `status: approved`).

- **Execute passing flow.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature run-satisfied --label run-satisfied -- run verify_all --target "$GARDEN_VERIFY_DIR/sample-cli" --json`. Exit code `0`; stdout JSON reports `"verdict": "SATISFIED"` and `"skipped_duplicate": false`, with a non-empty `run_dir`.
- **Snapshot Run Artifacts.** Extract `run_dir` from `$GARDEN_PROOF_DIR/run-satisfied.json` and run `.agents/skills/verify-garden/scripts/verify-garden-harness.py snapshot "<run_dir>" run-satisfied-artifacts`. Verify `run.json` marks `AC-001`, `AC-002`, and `INV-001` as `"satisfied"`, and `summary.md` starts with `<!-- garden:v1 kind=harvest `.
- **Confirm duplicate skip and force.** Re-run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature run-duplicate-skip --label run-duplicate-skip -- run verify_all --target "$GARDEN_VERIFY_DIR/sample-cli" --json` (exit `0`, `"skipped_duplicate": true`), then run with `--force` (`--label run-forced`, exit `0`, `"skipped_duplicate": false`).
- **Prove UNSATISFIED verdict.** Mutate the fixture with `.agents/skills/verify-garden/scripts/verify-garden-harness.py mutate set-app BROKEN`, then run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature run-unsatisfied --label run-unsatisfied -- run verify_all --target "$GARDEN_VERIFY_DIR/sample-cli" --json`. Exit code `1`; `"verdict": "UNSATISFIED"`.
- **Prove INCONCLUSIVE verdict.** Mutate with `.agents/skills/verify-garden/scripts/verify-garden-harness.py mutate set-app TEMPFAIL`, then run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature run-inconclusive --label run-inconclusive -- run verify_all --target "$GARDEN_VERIFY_DIR/sample-cli" --json`. Exit code `3`; `"verdict": "INCONCLUSIVE"`.
- **Prove NEEDS_HUMAN on draft spec.** Restore `OK` (`mutate set-app OK`), mark spec draft (`mutate set-spec-status draft`), and run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature run-unapproved-spec --label run-needs-human -- run verify_all --target "$GARDEN_VERIFY_DIR/sample-cli" --json`. Exit code `4`; `"verdict": "NEEDS_HUMAN"` with `"reason": "spec_not_approved"`.

## Gotchas

- `garden run` executes inside an isolated `git worktree` of `HEAD` by default; uncommitted edits to `src/app.txt` will not affect the run unless committed (which `mutate` does) or `--no-worktree` is passed.
- Always snapshot `run_dir` before calling `cleanup`, because `.garden/runs/` lives inside `$GARDEN_VERIFY_DIR`.
- `UNSATISFIED` and `INCONCLUSIVE` runs are intentionally never duplicate-skipped so retries work immediately after fixing code or environment issues.
