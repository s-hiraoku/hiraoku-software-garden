# Catalog list and check

Catalog list and check lets an operator inspect repositories registered in a Nursery catalog (`garden.toml`) and execute each target repository's registered check command with fetch-URL validation and dirty-tree reporting.

## Sub-features

- `catalog-list` prints the registered `[repositories]` JSON map from a catalog file.
- `catalog-check-pass` executes a registered repository's check command and reports `passed` with `commit` and `dirty` status.
- `catalog-check-dirty` detects untracked files as `"dirty": true` even when Git hides untracked files via config.
- `catalog-check-remote-mismatch` refuses to execute the check command and reports `unavailable` when no fetch remote matches the catalog `remote`.

## How to get to it (user POV)

- Run `python3 garden.py [--catalog <file>] list` in a terminal.
- Run `python3 garden.py [--catalog <file>] [--workspace <dir>] check <repo-id> [<repo-id> ...]` in a terminal.

## Driving it with verify-garden-harness

Preconditions:

- `launch` and `doctor` succeeded (`$GARDEN_VERIFY_DIR/catalog.toml` registers `sample-cli`).
- `$GARDEN_VERIFY_DIR/sample-cli` is clean on `main` with `src/app.txt` containing `OK`.

- **List catalog entries.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature catalog-list --label catalog-list -- --catalog "$GARDEN_VERIFY_DIR/catalog.toml" list`. Exit code `0`; stdout JSON contains `"sample-cli"` with `path`, `remote`, `description`, and `check`.
- **Run passing catalog check.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature catalog-check-pass --label catalog-check-pass -- --catalog "$GARDEN_VERIFY_DIR/catalog.toml" --workspace "$GARDEN_VERIFY_DIR" check sample-cli`. Exit code `0`; stdout JSON array entry has `"status": "passed"`, `"dirty": false`, and a 40-char `"commit"`.
- **Verify dirty detection.** Create an untracked file with `touch "$GARDEN_VERIFY_DIR/sample-cli/untracked.tmp"` and re-run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature catalog-check-dirty --label catalog-check-dirty -- --catalog "$GARDEN_VERIFY_DIR/catalog.toml" --workspace "$GARDEN_VERIFY_DIR" check sample-cli`. Exit code `0`; stdout JSON reports `"dirty": true`. Remove the temp file afterward (`rm "$GARDEN_VERIFY_DIR/sample-cli/untracked.tmp"`).
- **Verify remote-mismatch protection.** Copy `$GARDEN_VERIFY_DIR/catalog.toml` to `$GARDEN_VERIFY_DIR/bad-catalog.toml`, replace the remote URL with `https://example.invalid/wrong.git`, and run `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature catalog-check-remote-mismatch --label catalog-check-mismatch -- --catalog "$GARDEN_VERIFY_DIR/bad-catalog.toml" --workspace "$GARDEN_VERIFY_DIR" check sample-cli`. Exit code `1`; stdout JSON reports `"status": "unavailable"` and `detail` mentions fetch URL mismatch.

## Gotchas

- `--catalog` and `--workspace` must appear **before** the `list` or `check` subcommand (`argparse` rejects them after the subcommand).
- Passing `--catalog` with the Nursery's default empty `garden.toml` causes `check` to exit `2` because no repositories are registered yet.
- Check stdout is JSON on `stdout` while the target check command's output streams to `stderr`.
