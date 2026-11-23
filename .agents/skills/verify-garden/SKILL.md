---
name: verify-garden
description: "Drive and prove real user-facing behavior of the hiraoku-software-garden Nursery CLI (`garden.py`: list, check, init, doctor, upgrade, run, report, feedback) against isolated disposable Git target repositories and retain durable proof artifacts."
---

# Verify hiraoku-software-garden CLI

This skill drives the real `garden.py` CLI in `hiraoku-software-garden` against disposable local Git target repositories and captures reproducible evidence (commands, stdout, stderr, exit codes, Run Artifacts, and filesystem side effects). It never mutates the Nursery repository's own working tree or `garden.toml`.

Read [`features/README.md`](features/README.md) first, then follow the matching feature file for the behavior you are proving.

## Launch

`garden.py` is a short-lived Python 3.11+ CLI with zero third-party dependencies (no server daemon or build step). Launch creates an isolated disposable workspace (`GARDEN_VERIFY_DIR`) containing seeded Git target repositories (`sample-cli` and `fresh-cli`) plus a disposable `catalog.toml`, and prepares a separate proof directory (`GARDEN_PROOF_DIR`) that survives cleanup.

```sh
export RUN_ID="verify-$(date -u +%Y%m%dT%H%M%SZ)-$$"
export GARDEN_VERIFY_DIR="/tmp/garden-verify-${RUN_ID}"
export GARDEN_PROOF_DIR="/tmp/garden-proof-${RUN_ID}"
.agents/skills/verify-garden/scripts/verify-garden-harness.py launch
```

Ready condition: `launch` exits `0` and prints JSON containing `sample_repo`, `fresh_repo`, `catalog`, and `nursery_commit`.

Parallel isolation: because every run uses distinct `GARDEN_VERIFY_DIR` and `GARDEN_PROOF_DIR` paths, multiple verification runs can execute concurrently without sharing state or touching the Nursery working tree.

## Doctor

Run this read-only check before driving any feature (and whenever a run looks unexpected):

```sh
.agents/skills/verify-garden/scripts/verify-garden-harness.py doctor
```

Required result: exit code `0` and `"healthy": true`, confirming Python >= 3.11, Git on `PATH`, `garden.py` present, current `nursery_commit`, seeded `sample-cli` / `fresh-cli` / `catalog.toml` ready, and `GARDEN_PROOF_DIR` writable. Do not proceed if `doctor` fails.

## Drive

Drive every user-facing command through `verify-garden-harness.py cli`, which invokes the real `python3 garden.py <subcommand>` process from the repository root and saves structured proof JSON to `$GARDEN_PROOF_DIR/<label>.json`:

```sh
.agents/skills/verify-garden/scripts/verify-garden-harness.py cli \
  --feature <feature-id> \
  --label <step-label> \
  -- <garden.py arguments...>
```

Copy generated side effects (such as `.garden/runs/<run-id>/`, `.garden/garden.toml`, or `.garden/interventions/`) into `$GARDEN_PROOF_DIR` before cleanup:

```sh
.agents/skills/verify-garden/scripts/verify-garden-harness.py snapshot \
  "<source-path-inside-verify-dir>" "<proof-subdir-name>"
```

Use `mutate` to set up negative/recovery preconditions on the disposable `sample-cli` repository (commits real Git history so Trust Anchor and post-agent guards run against authentic commits):

```sh
.agents/skills/verify-garden/scripts/verify-garden-harness.py mutate set-app BROKEN
.agents/skills/verify-garden/scripts/verify-garden-harness.py mutate set-app TEMPFAIL
.agents/skills/verify-garden/scripts/verify-garden-harness.py mutate set-spec-status draft
.agents/skills/verify-garden/scripts/verify-garden-harness.py mutate set-agent-mode tamper
.agents/skills/verify-garden/scripts/verify-garden-harness.py mutate set-agent-mode question
```

## Evidence

All proof artifacts live under `$GARDEN_PROOF_DIR` (outside `$GARDEN_VERIFY_DIR`).

Proof standards:
- Exercise the real `python3 garden.py` entrypoint; never bypass it with internal Python imports for end-to-end verification.
- Capture both the action and the resulting state: every `cli` invocation records the exact command array, exit code, stdout, stderr, and UTC timestamp in `$GARDEN_PROOF_DIR/<label>.json`.
- Verify filesystem side effects alongside stdout by snapshotting generated artifacts (`run.json`, `summary.md`, `steps/`, `interventions/`, generated `.garden/garden.toml`) into `$GARDEN_PROOF_DIR` and inspecting them with a second read-only check.
- Use `mock` agent scripts only at the production `Agent Adapter Protocol` boundary (`GARDEN_AGENT_REQUEST` / `GARDEN_AGENT_RESPONSE` / `GARDEN_WORKTREE`); never mock `guard.py` or `git`.
- Never execute `garden report --post-github-issue` against a live repository during local verification; prove report formatting and label transitions locally via `garden report <run-dir> --format md|json` and state that live GitHub API posting was not exercised.

## Cleanup

Always run cleanup after both passing and failed runs:

```sh
.agents/skills/verify-garden/scripts/verify-garden-harness.py cleanup
```

Cleanup removes `$GARDEN_VERIFY_DIR` (all disposable target repos, worktrees, and temporary catalogs) and **never touches `$GARDEN_PROOF_DIR`**. Confirm `"verify_dir_exists": false` and `"proof_files_retained" > 0` in the cleanup output.

## Helpers

- [scripts/verify-garden-harness.py](scripts/verify-garden-harness.py) — Executable harness script (`chmod +x`). Subcommands:
  - `launch` — Seeds `$GARDEN_VERIFY_DIR/{sample-cli,fresh-cli,catalog.toml}` and initializes `$GARDEN_PROOF_DIR`.
  - `doctor` — Read-only environment and fixture readiness check.
  - `cli --feature <id> --label <name> -- <args...>` — Runs real `python3 garden.py <args...>` and writes `$GARDEN_PROOF_DIR/<name>.json`.
  - `snapshot <src> <dst-name>` — Copies a file or directory into `$GARDEN_PROOF_DIR/<dst-name>`.
  - `mutate <set-app|set-agent-mode|set-spec-status> <value>` — Commits a fixture state change in `$GARDEN_VERIFY_DIR/sample-cli`.
  - `cleanup` — Deletes `$GARDEN_VERIFY_DIR` while preserving `$GARDEN_PROOF_DIR`.
