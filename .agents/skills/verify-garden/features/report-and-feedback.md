# Report rendering, feedback, and upgrade

Report rendering, feedback, and upgrade lets an operator render structured Issue/PR summaries from Run Artifacts, record and aggregate human intervention reasons for Phase 7 improvement, and inspect non-destructive template upgrade plans.

## Sub-features

- `report-render` prints Markdown (with HTML machine-readable marker) or JSON from a completed `.garden/runs/<run-id>/` directory.
- `feedback-record-summary` writes a structured intervention record under `.garden/interventions/` and aggregates counts by category and status.
- `upgrade-plan` compares the target repository's `[template].version` against available Nursery template versions without modifying custom checks.

## How to get to it (user POV)

- Run `python3 garden.py report <run-dir> --format md|json` in a terminal.
- Run `python3 garden.py feedback add --target <repo> --run-id <id> --category <cat> --summary <text>` and `python3 garden.py feedback summary --target <repo>` in a terminal.
- Run `python3 garden.py upgrade --target <repo> [--to <ver>] [--apply]` in a terminal.

## Driving it with verify-garden-harness

Preconditions:

- `launch` and `doctor` succeeded (`$GARDEN_VERIFY_DIR/sample-cli` is ready).
- At least one completed `run_dir` exists from `garden run verify_all --target "$GARDEN_VERIFY_DIR/sample-cli" --json`.

- **Render Markdown and JSON report.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature report-render --label report-md -- report "<run_dir>" --format md` and `--label report-json -- report "<run_dir>" --format json`. Both exit `0`; Markdown contains `<!-- garden:v1 kind=harvest `, JSON parses with `"verdict": "SATISFIED"`.
- **Record human intervention.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature feedback-record-summary --label feedback-add -- feedback add --target "$GARDEN_VERIFY_DIR/sample-cli" --run-id "<run_id>" --category harness_bug --summary "startup crash misclassified" --triage-step "inspected stderr" --preventive-action "check exit status" --status verified`. Exit code `0`; stdout JSON includes `path` under `.garden/interventions/`.
- **Confirm persisted aggregation.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature feedback-record-summary --label feedback-summary -- feedback summary --target "$GARDEN_VERIFY_DIR/sample-cli"`. Exit code `0`; stdout JSON shows `"total": 1`, `"by_category": {"harness_bug": 1}`, `"by_status": {"verified": 1}`. Snapshot `$GARDEN_VERIFY_DIR/sample-cli/.garden/interventions`.
- **Inspect template upgrade plan.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature upgrade-plan --label upgrade-plan -- upgrade --target "$GARDEN_VERIFY_DIR/sample-cli"`. Exit code `0`; stdout JSON shows `"template": "cli"`, `"from_version": 1`, `"to_version": 1`, `"applied": false`, and lists custom checks in `preserved_custom_checks`.

## Gotchas

- Do not pass `--post-github-issue` against a live repository during local verification; report that live GitHub posting is unverified unless run against a dedicated sandbox issue.
- `garden upgrade` without `--apply` is strictly read-only; verify `.garden/garden.toml` remains unchanged after planning.
