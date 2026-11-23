import json
from pathlib import Path
import tempfile
import unittest

from garden.publish import evaluate_reflect_gate
from tests.helpers import run_cli


class ReflectGateTests(unittest.TestCase):
    def _base_run(self, **overrides) -> dict:
        data = {
            "schema_version": 1,
            "run_id": "20261123T100000Z-12345678",
            "idempotency_key": "1234567812345678",
            "flow": "verify_and_fix",
            "flow_scope": "all_specs",
            "verdict": "SATISFIED",
            "worktree_branch": "garden/20261123T100000Z-12345678",
            "target": {
                "commit": "a" * 40,
                "evaluation_criteria_changed": False,
            },
            "effective_delegation": {"mode": "auto_merge"},
            "criteria_results": {
                "AC-001": {"verdict": "satisfied", "checks": ["unit"]},
            },
        }
        data.update(overrides)
        return data

    def test_auto_merge_allowed_only_when_every_gate_passes(self):
        decision = evaluate_reflect_gate(self._base_run())
        self.assertEqual(decision.allowed_action, "auto_merge")

    def test_evaluation_criteria_changed_downgrades_auto_merge_to_propose_pr(self):
        run = self._base_run(
            target={"commit": "a" * 40, "evaluation_criteria_changed": True}
        )
        decision = evaluate_reflect_gate(run)
        self.assertEqual(decision.allowed_action, "propose_pr")
        self.assertIn("evaluation_criteria_changed_blocks_auto_merge", decision.reasons)

    def test_partial_flow_scope_downgrades_auto_merge_to_propose_pr(self):
        decision = evaluate_reflect_gate(self._base_run(flow_scope="flow_checks"))
        self.assertEqual(decision.allowed_action, "propose_pr")
        self.assertIn("partial_flow_scope_blocks_auto_merge", decision.reasons)

    def test_non_satisfied_or_observe_only_is_always_blocked(self):
        for verdict in ("UNSATISFIED", "INCONCLUSIVE", "NEEDS_HUMAN", "ABORTED"):
            decision = evaluate_reflect_gate(self._base_run(verdict=verdict))
            self.assertEqual(decision.allowed_action, "blocked")

        obs = evaluate_reflect_gate(
            self._base_run(effective_delegation={"mode": "observe_only"})
        )
        self.assertEqual(obs.allowed_action, "blocked")

    def test_cli_reflect_dry_run_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok_dir = Path(tmp) / "ok"
            ok_dir.mkdir()
            (ok_dir / "run.json").write_text(
                json.dumps(self._base_run()), encoding="utf-8"
            )
            p_ok = run_cli("reflect", str(ok_dir))
            self.assertEqual(p_ok.returncode, 0, p_ok.stderr)
            out = json.loads(p_ok.stdout)
            self.assertEqual(out["allowed_action"], "auto_merge")
            self.assertTrue(out["dry_run"])
            self.assertFalse(out["executed"])

            blk_dir = Path(tmp) / "blk"
            blk_dir.mkdir()
            (blk_dir / "run.json").write_text(
                json.dumps(self._base_run(verdict="INCONCLUSIVE")), encoding="utf-8"
            )
            p_blk = run_cli("reflect", str(blk_dir))
            self.assertEqual(p_blk.returncode, 1)
            self.assertEqual(json.loads(p_blk.stdout)["allowed_action"], "blocked")
