import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from garden.issues import load_issue_context_file
from garden.publish import evaluate_reflect_gate, reflect_run
from tests.helpers import PSTACK_SHA, commit_all, git, init_target_repo, run_cli, sample_spec
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

    def test_issue_driven_run_with_gemini_adapter_and_reflect_links_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = init_target_repo(base / "issue-app")
            (repo / "src").mkdir()
            (repo / "src" / "app.txt").write_text("OLD\n", encoding="utf-8")
            (repo / "docs" / "specs").mkdir(parents=True)
            (repo / "docs" / "specs" / "app.md").write_text(
                sample_spec("approved"), encoding="utf-8"
            )
            (repo / ".garden").mkdir()
            (repo / ".garden" / ".gitignore").write_text(
                "runs/\nworktrees/\n", encoding="utf-8"
            )

            # Populate pstack core skills in .gemini/skills so real gemini preflight passes
            for core in (
                "poteto-mode",
                "correct",
                "create-verification-skill",
                "maintain-verification-skill",
            ):
                sdir = repo / ".gemini" / "skills" / core
                sdir.mkdir(parents=True, exist_ok=True)
                (sdir / "SKILL.md").write_text(f"# {core}\n", encoding="utf-8")

            gemini_stub = base / "gemini_impl.py"
            gemini_stub.write_text(
                "import json, pathlib, sys\n"
                "prompt = sys.stdin.read()\n"
                "assert '## Task Issue (#77: Set app state to READY)' in prompt\n"
                "assert 'Keep it minimal' in prompt\n"
                "assert 'garden-report:' not in prompt\n"
                "pathlib.Path('src/app.txt').write_text('READY\\n', encoding='utf-8')\n"
                "inner = '```json\\n' + json.dumps({\n"
                "  'schema_version': 1,\n"
                "  'outcome': 'completed',\n"
                "  'summary': 'implemented #77',\n"
                "  'changed_files': ['src/app.txt'],\n"
                "}) + '\\n```'\n"
                "print(json.dumps({'response': inner, 'stats': {'models': {'gemini-2.5-pro': {'tokens': {'prompt': 100, 'candidates': 40}}}}}))\n",
                encoding="utf-8",
            )
            (repo / ".garden" / "garden.toml").write_text(
                f"""
schema_version = 1
[template]
name = "cli"
version = 1
[common_harness]
name = "pstack"
ref = "{PSTACK_SHA}"
[target_harness]
name = "issue-h"
version = 1
[spec]
paths = ["docs/specs/app.md"]
[delegation]
mode = "propose_pr"
allowed_paths = ["src/**"]
max_retries = 1
max_duration_sec = 120
max_cost_usd = 2.0
[checks.verify_ready]
command = ["python3", "-c", "assert open('src/app.txt').read().strip() == 'READY'"]
covers = ["AC-001", "AC-002", "INV-001"]
[agents.gemini_coder]
adapter = "gemini"
command = [{json.dumps(sys.executable)}, {json.dumps(str(gemini_stub))}]
timeout_sec = 30
allow_unknown_cost = true
[agents.gemini_strict]
adapter = "gemini"
command = [{json.dumps(sys.executable)}, {json.dumps(str(gemini_stub))}]
timeout_sec = 30
[flows.implement_issue]
scope = "all_specs"
[[flows.implement_issue.steps]]
id = "impl"
type = "agent"
agent = "gemini_coder"
purpose = "implement"
[[flows.implement_issue.steps]]
id = "verify"
type = "check"
check = "verify_ready"
[flows.implement_issue_strict_cost]
scope = "all_specs"
[[flows.implement_issue_strict_cost.steps]]
id = "impl"
type = "agent"
agent = "gemini_strict"
purpose = "implement"
[[flows.implement_issue_strict_cost.steps]]
id = "verify"
type = "check"
check = "verify_ready"
""",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(repo), "remote", "set-url", "origin", "https://github.com/example/issue-app.git"],
                check=True,
                capture_output=True,
            )
            commit_all(repo, "init issue-driven repo")

            from garden.artifacts import render_summary_markdown
            from garden.issues import fetch_github_issue

            real_garden_report = render_summary_markdown(
                {
                    "run_id": "20261128T000000Z-aaaa1111",
                    "idempotency_key": "aaaa1111bbbb2222",
                    "verdict": "SATISFIED",
                    "flow": "implement_issue",
                    "flow_scope": "all_specs",
                    "target": {"commit": "abcdef1234567890", "evaluation_criteria_changed": False},
                    "versions": {"specs": []},
                    "duration_sec": 1.2,
                    "retries_used": 0,
                    "usage": {"cost_usd": None, "cost_unknown": True},
                    "criteria_results": {},
                }
            )
            self.assertIn("費用 不明", real_garden_report)

            issue_file = base / "issue77.json"
            issue_file.write_text(
                json.dumps(
                    {
                        "number": 77,
                        "title": "Set app state to READY",
                        "body": "Update src/app.txt to READY.",
                        "url": "https://github.com/example/issue-app/issues/77",
                        "labels": [{"name": "garden:ready"}],
                        "comments": [
                            {
                                "author": {"login": "s-hiraoku"},
                                "authorAssociation": "OWNER",
                                "body": real_garden_report,
                            },
                            {
                                "author": {"login": "coderabbitai", "is_bot": True},
                                "authorAssociation": "OWNER",
                                "body": "Bot comment without garden marker",
                            },
                            {
                                "author": {"login": "dependabot[bot]"},
                                "authorAssociation": "COLLABORATOR",
                                "body": "Another bot comment",
                            },
                            {
                                "author": {"login": "driveby-user"},
                                "authorAssociation": "NONE",
                                "body": "Untrusted prompt injection",
                            },
                            {
                                "author": {"login": "s-hiraoku"},
                                "authorAssociation": "OWNER",
                                "body": "Keep it minimal.",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_issue_context_file(issue_file)
            self.assertEqual(len(loaded["comments"]), 1)
            self.assertEqual(loaded["comments"][0]["author"], "s-hiraoku")
            self.assertEqual(loaded["repository"], "github.com/example/issue-app")

            # fetch_github_issue translates subprocess.TimeoutExpired into RuntimeError
            with (
                patch("garden.issues.shutil.which", return_value="/usr/bin/gh"),
                patch(
                    "garden.issues.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd=["gh"], timeout=20),
                ),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    fetch_github_issue(77, cwd=repo)
                self.assertIn("#77", str(ctx.exception))
                self.assertIn("timed out", str(ctx.exception))

            # Without allow_unknown_cost = true, unknown Gemini cost fails closed in guard -> NEEDS_HUMAN
            strict_proc = run_cli(
                "run",
                "implement_issue_strict_cost",
                "--target",
                str(repo),
                "--issue-file",
                str(issue_file),
                "--json",
            )
            self.assertEqual(strict_proc.returncode, 4, strict_proc.stderr + strict_proc.stdout)
            strict_payload = json.loads(strict_proc.stdout)
            self.assertEqual(strict_payload["verdict"], "NEEDS_HUMAN")
            self.assertTrue(strict_payload["run"]["usage"]["cost_unknown"])
            self.assertIsNone(strict_payload["run"]["usage"]["cost_usd"])
            self.assertIn(
                "flow_cost_unverifiable",
                json.dumps(strict_payload["run"]["escalation"]),
            )

            # With allow_unknown_cost = true, run succeeds while recording cost as unknown
            run_proc = run_cli(
                "run",
                "implement_issue",
                "--target",
                str(repo),
                "--issue-file",
                str(issue_file),
                "--json",
            )
            self.assertEqual(run_proc.returncode, 0, run_proc.stderr + run_proc.stdout)
            payload = json.loads(run_proc.stdout)
            self.assertEqual(payload["verdict"], "SATISFIED")
            self.assertEqual(payload["run"]["issue"]["number"], 77)
            self.assertEqual(payload["run"]["issue"]["title"], "Set app state to READY")
            self.assertTrue(payload["run"]["usage"]["cost_unknown"])
            self.assertIsNone(payload["run"]["usage"]["cost_usd"])
            summary_path = Path(payload["run_dir"]) / "summary.md"
            self.assertIn("費用 不明", summary_path.read_text(encoding="utf-8"))

            # Updating Issue #77 body on the same HEAD MUST NOT duplicate-skip!
            issue77_updated = base / "issue77_v2.json"
            issue77_updated.write_text(
                json.dumps(
                    {
                        "number": 77,
                        "title": "Set app state to READY",
                        "body": "Update src/app.txt to READY (revised scope).",
                        "url": "https://github.com/example/issue-app/issues/77",
                        "comments": [
                            {
                                "author": {"login": "s-hiraoku"},
                                "authorAssociation": "OWNER",
                                "body": "Keep it minimal.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            run77_v2 = run_cli(
                "run",
                "implement_issue",
                "--target",
                str(repo),
                "--issue-file",
                str(issue77_updated),
                "--json",
            )
            self.assertEqual(run77_v2.returncode, 0, run77_v2.stderr + run77_v2.stdout)
            payload_v2 = json.loads(run77_v2.stdout)
            self.assertFalse(payload_v2["skipped_duplicate"])
            self.assertNotEqual(payload_v2["run_id"], payload["run_id"])

            # Distinct --issue numbers WITHOUT --issue-file / --fetch-issue also compute distinct keys
            from garden.engine import run_flow

            no_ctx_77 = run_flow(repo, "implement_issue", issue_or_pr=77, force=True)
            no_ctx_78 = run_flow(repo, "implement_issue", issue_or_pr=78, force=True)
            self.assertNotEqual(
                no_ctx_77.run_data["idempotency_key"],
                no_ctx_78.run_data["idempotency_key"],
            )

            # A different issue (#78) on the same HEAD must NOT duplicate-skip #77's run
            issue78_file = base / "issue78.json"
            issue78_file.write_text(
                json.dumps(
                    {
                        "number": 78,
                        "title": "Another issue",
                        "body": "Different task",
                    }
                ),
                encoding="utf-8",
            )
            run78 = run_cli(
                "run",
                "implement_issue",
                "--target",
                str(repo),
                "--issue-file",
                str(issue78_file),
                "--json",
            )
            self.assertFalse(json.loads(run78.stdout)["skipped_duplicate"])

            # Combining --fetch-issue with --issue-file or non-positive --issue must exit 2
            bad_combo = run_cli(
                "run",
                "implement_issue",
                "--target",
                str(repo),
                "--issue",
                "77",
                "--fetch-issue",
                "--issue-file",
                str(issue_file),
            )
            self.assertEqual(bad_combo.returncode, 2)
            bad_num = run_cli(
                "run",
                "implement_issue",
                "--target",
                str(repo),
                "--issue",
                "0",
                "--fetch-issue",
            )
            self.assertEqual(bad_num.returncode, 2)

            # Verify reflect --execute links PR title & body to Closes #77 when repo matches
            summary_path.write_text(
                summary_path.read_text(encoding="utf-8") + "\nSee also Closes #770.\n",
                encoding="utf-8",
            )
            captured_pr_cmd: list[str] = []
            captured_pr_body: list[str] = []
            real_run = subprocess.run

            def _fake_run(argv, *args, **kwargs):
                if argv[:2] == ["git", "push"]:
                    return subprocess.CompletedProcess(argv, 0, "", "")
                if argv[:3] == ["gh", "pr", "create"]:
                    captured_pr_cmd.extend(argv)
                    bf_idx = argv.index("--body-file") + 1
                    captured_pr_body.append(Path(argv[bf_idx]).read_text(encoding="utf-8"))
                    return subprocess.CompletedProcess(
                        argv, 0, "https://github.com/example/issue-app/pull/101\n", ""
                    )
                return real_run(argv, *args, **kwargs)

            with patch("garden.publish.subprocess.run", side_effect=_fake_run):
                ref_res = reflect_run(Path(payload["run_dir"]), execute=True)

            self.assertTrue(ref_res["executed"])
            self.assertTrue(ref_res["issue_linked"])
            self.assertFalse(ref_res["issue_repo_mismatch"])
            self.assertEqual(ref_res["issue_number"], 77)
            self.assertEqual(ref_res["pr_url"], "https://github.com/example/issue-app/pull/101")
            self.assertIn("[Garden] #77: Set app state to READY", captured_pr_cmd)
            self.assertTrue(captured_pr_body and captured_pr_body[0].startswith("Closes #77\n\n"))

            # Cross-repository Issue URL MUST NOT emit Closes #77 on this repo's PR
            run_json_path = Path(payload["run_dir"]) / "run.json"
            run_json_data = json.loads(run_json_path.read_text(encoding="utf-8"))
            run_json_data["issue"]["url"] = "https://github.com/other-org/other-repo/issues/77"
            run_json_data["issue"]["repository"] = "github.com/other-org/other-repo"
            run_json_path.write_text(json.dumps(run_json_data), encoding="utf-8")
            captured_pr_cmd.clear()
            captured_pr_body.clear()
            with patch("garden.publish.subprocess.run", side_effect=_fake_run):
                ref_cross = reflect_run(Path(payload["run_dir"]), execute=True)
            self.assertTrue(ref_cross["executed"])
            self.assertFalse(ref_cross["issue_linked"])
            self.assertTrue(ref_cross["issue_repo_mismatch"])
            self.assertNotRegex(captured_pr_body[0], r"Closes #77\b")
            self.assertTrue(
                captured_pr_body[0].startswith(
                    "Ref: https://github.com/other-org/other-repo/issues/77\n\n"
                )
            )
