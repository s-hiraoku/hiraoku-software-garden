# hiraoku-software-garden verification map

This directory is the maintained source for verifying the user-facing behavior of the `garden.py` Nursery CLI. Read this index before driving the CLI, then follow the matching feature file as the literal recipe.

## Baseline preconditions

- Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py launch` with unique `GARDEN_VERIFY_DIR=/tmp/garden-verify-$RUN_ID` and `GARDEN_PROOF_DIR=/tmp/garden-proof-$RUN_ID`.
- Keep `GARDEN_VERIFY_DIR` and `GARDEN_PROOF_DIR` disjoint so cleanup never removes proof artifacts.
- Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py doctor` and require exit code `0` with `"healthy": true`.
- Never run mutating verification commands directly against the Nursery repository's own `garden.toml` or working tree.

## Driving conventions

- Start every recipe from a freshly launched baseline (`launch` + `doctor`) unless its preconditions state a specific mutation.
- Treat every command and flag as literal.
- Drive all CLI actions through `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature <id> --label <name> -- <args...>`.
- Snapshot generated Run Artifacts, specs, definitions, or intervention files into `$GARDEN_PROOF_DIR` before running `cleanup`.
- Always run `cleanup` after both passing and failing runs, then verify `$GARDEN_PROOF_DIR` still holds the captured files.

## Proof and skip reporting

- Capture both the user command and the resulting observable state (stdout, stderr, exit code, and filesystem artifacts).
- CLI proof lives in `$GARDEN_PROOF_DIR/<label>.json` alongside snapshotted directories/files.
- Mutation proof requires a second read-only check confirming persisted state (e.g., `feedback summary`, `doctor --json`, or reading `run.json` and `summary.md`).
- Record the feature ID and entry point used with every artifact.
- Report any skipped path (such as live GitHub API posting or live branch-protection lookup) explicitly with the unmet precondition; never claim it was verified through another path.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. It then uses exactly four H2 sections in this order:

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every user entry point.
3. `Driving it with verify-garden-harness` starts with `Preconditions:` and uses labeled bullets that pair each user action with an exact command and observable result.
4. `Gotchas` lists traps that can waste or invalidate a verification run.

## Features

- [Catalog list and check](./catalog-check.md) covers listing registered repositories, running registered checks, dirty tracking, and mismatched-remote rejection.
- [Garden init and doctor](./garden-init-doctor.md) covers auto-detecting a CLI target repository, generating safe initial Garden files, and running the 7-point diagnostic check.
- [Flow execution and verdicts](./flow-run-verdicts.md) covers deterministic `garden run` execution across `SATISFIED`, `UNSATISFIED`, `INCONCLUSIVE`, and `NEEDS_HUMAN` outcomes, Run Artifacts, and duplicate skipping.
- [Agent fix loop and post-agent guard](./agent-fix-and-guard.md) covers `fix_and_retry` recovery in an isolated worktree, forbidden-path violation blocking, and `needs_spec_decision` escalation.
- [Report rendering, feedback, and upgrade](./report-and-feedback.md) covers `garden report`, recording and summarizing human interventions (`garden feedback`), and planning template upgrades (`garden upgrade`).
