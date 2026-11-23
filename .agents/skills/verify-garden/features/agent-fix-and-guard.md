# Agent fix loop and post-agent guard

Agent fix loop and post-agent guard lets a flow invoke an external agent adapter to fix a failing check inside an isolated worktree while deterministically enforcing path boundaries, cost caps, and spec-decision escalations.

## Sub-features

- `agent-fix-recovery` runs `fix_and_retry`, lets the adapter repair an allowed file, invalidates cached check results, and passes the final gate (`SATISFIED`, exit `0`).
- `guard-blocks-forbidden` catches forbidden-path edits (`docs/specs/**`) via `git diff --no-renames <target_commit>` and halts with `NEEDS_HUMAN` (exit `4`).
- `agent-spec-question` transitions to `NEEDS_HUMAN` (exit `4`) with a `kind=spec_question` summary when the adapter returns `outcome: "needs_spec_decision"`.

## How to get to it (user POV)

- Run `python3 garden.py run verify_and_fix --target <repo> [--keep-worktree] [--json]` in a terminal.
- Inspect the resulting `run.json`, `summary.md`, and `steps/*-fix_unit/{request.json,response.json,step.json}`.

## Driving it with verify-garden-harness

Preconditions:

- `launch` and `doctor` succeeded (`$GARDEN_VERIFY_DIR/sample-cli` has `verify_and_fix` wired to `scripts/mock_agent.py`).

- **Prove fix_and_retry recovery.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py mutate set-app BROKEN` and `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature agent-fix-recovery --label agent-fix-ok -- run verify_and_fix --target "$GARDEN_VERIFY_DIR/sample-cli" --json`. Exit code `0`; `"verdict": "SATISFIED"`, `"retries_used": 1`, and the original `$GARDEN_VERIFY_DIR/sample-cli/src/app.txt` remains `BROKEN` (confirming worktree isolation).
- **Prove forbidden-path guard.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py mutate set-agent-mode tamper` and `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature guard-blocks-forbidden --label agent-guard-block -- run verify_and_fix --target "$GARDEN_VERIFY_DIR/sample-cli" --json`. Exit code `4`; `"verdict": "NEEDS_HUMAN"` with `"reason": "delegation_violation"`.
- **Prove spec-decision escalation.** Run `.agents/skills/verify-garden/scripts/verify-garden-harness.py mutate set-agent-mode question` and `.agents/skills/verify-garden/scripts/verify-garden-harness.py cli --feature agent-spec-question --label agent-spec-question -- run verify_and_fix --target "$GARDEN_VERIFY_DIR/sample-cli" --json`. Exit code `4`; snapshot `run_dir` and verify `summary.md` starts with `<!-- garden:v1 kind=spec_question ` and contains `Keep latest or merge?`.

## Gotchas

- `guard.py` never trusts `response.json`'s `changed_files` list; even if the adapter reports `changed_files: []` or commits its edits, `git diff --name-only --no-renames <target_commit>` still catches the violation.
- If `verify_and_fix` is run while `src/app.txt` is already `OK`, `s_unit` passes on the first try and `fix_unit` (`run_on_demand = true`) is never invoked. Always run `mutate set-app BROKEN` first.
