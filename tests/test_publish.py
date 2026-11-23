import json
from pathlib import Path
import tempfile
import unittest

from garden.publish import evaluate_reflect_gate
from tests.helpers import git, run_cli
from tests.test_engine import _build_fixture_repo


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
            "verified_commit": "b" * 40,
            "agent_changed_files": ["src/app.txt"],
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
        self.assertEqual(decision.verified_commit, "b" * 40)

    def test_missing_or_identical_verified_commit_returns_none(self):
        no_commit = evaluate_reflect_gate(self._base_run(verified_commit=None))
        self.assertEqual(no_commit.allowed_action, "none")
        self.assertIn("no_verified_commit_diff", no_commit.reasons)

        same_commit = evaluate_reflect_gate(self._base_run(verified_commit="a" * 40))
        self.assertEqual(same_commit.allowed_action, "none")
        self.assertIn("no_verified_commit_diff", same_commit.reasons)

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

    def test_end_to_end_run_to_reflect_preserves_verified_commit_and_detects_noop_and_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Case 1: app_value="BROKEN" -> agent fixes file -> committed on garden/<run-id>
            broken_repo = _build_fixture_repo(tmp_path / "broken", app_value="BROKEN")
            run_fix = run_cli("run", "verify_and_fix", "--target", str(broken_repo), "--json")
            self.assertEqual(run_fix.returncode, 0, run_fix.stderr)
            fix_payload = json.loads(run_fix.stdout)
            self.assertEqual(fix_payload["verdict"], "SATISFIED")

            run_data = fix_payload["run"]
            branch = run_data["worktree_branch"]
            verified_sha = run_data["verified_commit"]
            target_sha = run_data["target"]["commit"]
            self.assertIsNotNone(branch)
            self.assertIsNotNone(verified_sha)
            self.assertNotEqual(verified_sha, target_sha)

            # Branch tip must still contain the verified fix even after worktree cleanup
            tip = git(broken_repo, "rev-parse", f"refs/heads/{branch}").strip()
            self.assertEqual(tip, verified_sha)
            diff_out = git(broken_repo, "diff", target_sha, branch)
            self.assertIn("-BROKEN", diff_out)
            self.assertIn("+OK", diff_out)

            ref_ok = run_cli("reflect", fix_payload["run_dir"])
            self.assertEqual(ref_ok.returncode, 0, ref_ok.stderr)
            ref_ok_data = json.loads(ref_ok.stdout)
            self.assertEqual(ref_ok_data["allowed_action"], "auto_merge")
            self.assertTrue(ref_ok_data["git_verified"])
            self.assertEqual(ref_ok_data["verified_commit"], verified_sha)

            # Case 1b: If the branch tip is moved away from verified_commit, reflect MUST block
            git(broken_repo, "update-ref", f"refs/heads/{branch}", target_sha)
            ref_tampered = run_cli("reflect", fix_payload["run_dir"])
            self.assertEqual(ref_tampered.returncode, 1)
            tampered_data = json.loads(ref_tampered.stdout)
            self.assertEqual(tampered_data["allowed_action"], "blocked")
            self.assertIn("branch_tip_mismatch_verified_commit", tampered_data["reasons"])

            # Case 2: app_value="OK" -> agent never runs -> no diff -> reflect returns "none"
            ok_repo = _build_fixture_repo(tmp_path / "already-ok", app_value="OK")
            run_noop = run_cli("run", "verify_and_fix", "--target", str(ok_repo), "--json")
            self.assertEqual(run_noop.returncode, 0, run_noop.stderr)
            noop_payload = json.loads(run_noop.stdout)
            self.assertEqual(noop_payload["verdict"], "SATISFIED")
            self.assertIsNone(noop_payload["run"]["verified_commit"])
            self.assertIsNone(noop_payload["run"]["worktree_branch"])

            ref_noop = run_cli("reflect", noop_payload["run_dir"])
            self.assertEqual(ref_noop.returncode, 0, ref_noop.stderr)
            noop_ref_data = json.loads(ref_noop.stdout)
            self.assertEqual(noop_ref_data["allowed_action"], "none")

    def test_reflect_blocks_unreviewed_contributor_pr_branch_and_unapproved_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _build_fixture_repo(tmp_path / "pr-attack", app_value="OK")

            # Contributor PR branch adds src/backdoor.txt and breaks src/app.txt
            git(repo, "checkout", "-b", "contributor-pr")
            (repo / "src" / "backdoor.txt").write_text("unreviewed\n", encoding="utf-8")
            (repo / "src" / "app.txt").write_text("BROKEN\n", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-m", "contributor pr with backdoor")

            run_fix = run_cli(
                "run", "verify_and_fix", "--target", str(repo), "--approval-branch", "main", "--json"
            )
            self.assertEqual(run_fix.returncode, 0, run_fix.stderr)
            payload = json.loads(run_fix.stdout)
            self.assertEqual(payload["verdict"], "SATISFIED")
            self.assertEqual(payload["run"]["agent_changed_files"], ["src/app.txt"])

            # Reflecting against --base main MUST be blocked because target_commit is not on main
            ref_main = run_cli("reflect", payload["run_dir"], "--base", "main")
            self.assertEqual(ref_main.returncode, 1)
            main_data = json.loads(ref_main.stdout)
            self.assertEqual(main_data["allowed_action"], "blocked")
            self.assertIn("target_commit_not_on_base_branch:main", main_data["reasons"])

            # Reflecting against the PR's own branch (--base contributor-pr) succeeds because
            # target_commit IS on contributor-pr and diff contains only guard-approved src/app.txt
            ref_pr = run_cli("reflect", payload["run_dir"], "--base", "contributor-pr")
            self.assertEqual(ref_pr.returncode, 0, ref_pr.stderr)
            pr_data = json.loads(ref_pr.stdout)
            self.assertEqual(pr_data["allowed_action"], "auto_merge")
            self.assertTrue(pr_data["git_verified"])

            # Tampering run.json to omit src/app.txt from agent_changed_files MUST block
            run_json = Path(payload["run_dir"]) / "run.json"
            mutated = json.loads(run_json.read_text(encoding="utf-8"))
            mutated["agent_changed_files"] = ["src/other.txt"]
            run_json.write_text(json.dumps(mutated), encoding="utf-8")

            ref_unapproved = run_cli("reflect", payload["run_dir"], "--base", "contributor-pr")
            self.assertEqual(ref_unapproved.returncode, 1)
            unapp_data = json.loads(ref_unapproved.stdout)
            self.assertEqual(unapp_data["allowed_action"], "blocked")
            self.assertTrue(
                any(r.startswith("unapproved_files_in_verified_commit:") for r in unapp_data["reasons"])
            )
