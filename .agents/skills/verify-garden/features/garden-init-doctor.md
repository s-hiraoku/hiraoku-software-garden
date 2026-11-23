# Garden init and doctor

Garden init and doctor lets an operator onboard a target repository by auto-detecting a Nursery template, scaffolding safe initial Garden files, and running the 7-point diagnostic check before executing flows.

## Sub-features

- `init-autodetect` inspects an uninitialized repository, selects the `cli` template, and generates `.garden/garden.toml`, `.garden/.gitignore`, `docs/specs/<slug>.md`, and `.github/workflows/garden.yml`.
- `init-overwrite-guard` refuses to overwrite existing generated files unless `--force` is passed.
- `doctor-unapproved-draft` flags freshly generated `status: draft` specs as an error before approval.
- `doctor-healthy` reports `exit_code: 0` once the spec and `.garden/garden.toml` are approved and committed on `main`.

## How to get to it (user POV)

- Run `python3 garden.py init --target <repo> [--type <template>] [--force]` in a terminal.
- Run `python3 garden.py doctor --target <repo> [--json] [--skip-network]` in a terminal.

## Driving it with verify-garden-harness

Preconditions:

- `launch` and `doctor` succeeded (`$GARDEN_VERIFY_DIR/fresh-cli` is an uninitialized Git repo with `pyproject.toml`).

- **Initialize fresh repository.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature init-autodetect --label init-fresh -- init --target "$GARDEN_VERIFY_DIR/fresh-cli"`. Exit code `0`; stdout JSON shows `"template": "cli"`, `"version": 1`, and 4 created files.
- **Verify overwrite guard.** Re-run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature init-overwrite-guard --label init-no-overwrite -- init --target "$GARDEN_VERIFY_DIR/fresh-cli"`. Exit code `2`; stderr refuses to overwrite existing files without `--force`.
- **Snapshot generated definition.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py snapshot "$GARDEN_VERIFY_DIR/fresh-cli/.garden/garden.toml" fresh-init-garden.toml`. Confirm `mode = "observe_only"` in the snapshotted file.
- **Run doctor on unapproved draft.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature doctor-unapproved-draft --label doctor-draft -- doctor --target "$GARDEN_VERIFY_DIR/fresh-cli" --skip-network --json`. Exit code `1`; `specs_and_trust` has `"status": "error"` because the spec is not yet approved on `main`.
- **Run doctor on approved sample-cli.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature doctor-healthy --label doctor-sample -- doctor --target "$GARDEN_VERIFY_DIR/sample-cli" --skip-network --json`. Exit code `0`; `git_repo`, `garden_definition`, `specs_and_trust`, `check_commands`, and `garden_gitignore` all report `"ok"`.

## Gotchas

- Omit `--skip-network` only when `gh` is authenticated and the target remote is a real GitHub repository; against disposable `example.invalid` remotes, `branch_protection` reports `"warn"` (which does not fail `doctor`).
- `garden init` always starts in `observe_only` mode with a `draft` spec so nothing runs with write authority before human review.
