import json
from pathlib import Path
import sys
import tempfile
import unittest

from tests.helpers import PSTACK_SHA, commit_all, git, init_target_repo, run_cli, sample_spec


def _build_fixture_repo(
    root: Path,
    *,
    app_value: str = "OK",
    spec_status: str = "approved",
    unit_script: str = "import sys; from pathlib import Path; sys.exit(0 if Path('src/app.txt').read_text().strip() == 'OK' else 1)",
    evidence_script: str = (
        "import os; from pathlib import Path; "
        "(Path(os.environ['GARDEN_EVIDENCE_DIR']) / 'transcript.json').write_text('{\"ok\":true}')"
    ),
    formal_script: str = (
        "import json, os; from pathlib import Path; "
        "(Path(os.environ['GARDEN_EVIDENCE_DIR']) / 'formal.json').write_text("
        "json.dumps({'schema_version':1,'tool':'quint','model_path':'formal/m.qnt',"
        "'invariants':['INV-001'],'assumptions':['n=2'],'scope':'steps=5','completed':True,'result':'proved'}))"
    ),
    agent_script: str = (
        "import json, os; from pathlib import Path; "
        "wt = Path(os.environ['GARDEN_WORKTREE']); "
        "(wt / 'src' / 'app.txt').write_text('OK\\n'); "
        "Path(os.environ['GARDEN_AGENT_RESPONSE']).write_text("
        "json.dumps({'schema_version':1,'outcome':'completed','summary':'fixed app.txt',"
        "'changed_files':['src/app.txt'],'usage':{'input_tokens':100,'output_tokens':50,'cost_usd':0.05}}))"
    ),
    extra_checks_toml: str = "",
    extra_flows_toml: str = "",
) -> Path:
    init_target_repo(root)
    (root / "src").mkdir()
    (root / "scripts").mkdir()
    (root / "formal").mkdir()
    (root / "docs" / "specs").mkdir(parents=True)
    (root / ".garden").mkdir()

    (root / "src" / "app.txt").write_text(f"{app_value}\n", encoding="utf-8")
    (root / "formal" / "m.qnt").write_text("module m {}\n", encoding="utf-8")
    (root / "scripts" / "unit.py").write_text(unit_script + "\n", encoding="utf-8")
    (root / "scripts" / "e2e.py").write_text(evidence_script + "\n", encoding="utf-8")
    (root / "scripts" / "formal.py").write_text(formal_script + "\n", encoding="utf-8")
    (root / "scripts" / "agent.py").write_text(agent_script + "\n", encoding="utf-8")
    (root / "docs" / "specs" / "app.md").write_text(
        sample_spec(spec_status), encoding="utf-8"
    )
    (root / ".garden" / ".gitignore").write_text("runs/\nworktrees/\n", encoding="utf-8")

    py = json.dumps(sys.executable)
    garden_toml = f"""
schema_version = 1

[template]
name = "cli"
version = 1

[common_harness]
name = "pstack"
ref = "{PSTACK_SHA}"

[target_harness]
name = "fixture-harness"
version = 1

[spec]
paths = ["docs/specs/app.md"]
approval_branch = "main"

[delegation]
mode = "auto_merge"
allowed_paths = ["src/**"]
forbidden_paths = [".github/workflows/**"]
max_retries = 2
max_duration_sec = 120
max_cost_usd = 2.0
require_checks = ["unit"]

[checks.unit]
kind = "check"
command = [{py}, "scripts/unit.py"]
timeout_sec = 30
covers = ["AC-001"]
inconclusive_exit_codes = [75]

[checks.e2e]
kind = "check"
command = [{py}, "scripts/e2e.py"]
timeout_sec = 30
covers = ["AC-002"]
required_evidence = ["transcript.json"]
inconclusive_exit_codes = [75]

[checks.formal_inv]
kind = "formal_check"
tool = "quint"
model_path = "formal/m.qnt"
command = [{py}, "scripts/formal.py"]
timeout_sec = 30
covers = ["INV-001"]
invariants = ["INV-001"]
{extra_checks_toml}

[agents.default]
adapter = "mock"
command = [{py}, "scripts/agent.py"]
timeout_sec = 30
max_cost_usd = 1.0

[flows.full_verify]
scope = "all_specs"

[flows.full_verify.delegation]
mode = "observe_only"
allowed_paths = []
max_retries = 0

[[flows.full_verify.steps]]
id = "s_unit"
type = "check"
check = "unit"
on_fail = "escalate"

[[flows.full_verify.steps]]
id = "final_gate"
type = "gate"
require_checks = ["unit", "e2e", "formal_inv"]

[[flows.full_verify.steps]]
id = "rep"
type = "report"
kind = "auto"

[flows.partial_verify]
scope = "flow_checks"

[flows.partial_verify.delegation]
mode = "observe_only"
allowed_paths = []
max_retries = 0

[[flows.partial_verify.steps]]
id = "s_unit"
type = "check"
check = "unit"
on_fail = "abort"

[flows.verify_and_fix]
scope = "all_specs"

[[flows.verify_and_fix.steps]]
id = "s_unit"
type = "check"
check = "unit"
on_fail = "fix_and_retry"
fix_step = "fix_code"
max_retries = 2

[[flows.verify_and_fix.steps]]
id = "fix_code"
type = "agent"
agent = "default"
purpose = "fix"
run_on_demand = true

[[flows.verify_and_fix.steps]]
id = "final_gate"
type = "gate"
require_checks = ["unit", "e2e", "formal_inv"]
{extra_flows_toml}
"""
    (root / ".garden" / "garden.toml").write_text(garden_toml, encoding="utf-8")
    commit_all(root, "initial fixture")
    return root


class EngineIntegrationTests(unittest.TestCase):
    def test_mvp1_satisfied_full_flow_and_harvest_marker_and_duplicate_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_fixture_repo(Path(tmp) / "repo")
            proc = run_cli("run", "full_verify", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["verdict"], "SATISFIED")
            self.assertFalse(out["skipped_duplicate"])

            run_dir = Path(out["run_dir"])
            run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run_json["criteria_results"]["AC-001"]["verdict"], "satisfied")
            self.assertEqual(run_json["criteria_results"]["AC-002"]["verdict"], "satisfied")
            self.assertEqual(run_json["criteria_results"]["INV-001"]["verdict"], "satisfied")

            # Gate must have reused unit and executed e2e & formal_inv
            gate_step = [p for p in run_json["steps"] if p.endswith("-final_gate/step.json")][0]
            gate_data = json.loads((run_dir / gate_step).read_text(encoding="utf-8"))
            self.assertTrue(gate_data["checks"]["unit"]["reused"])
            self.assertFalse(gate_data["checks"]["e2e"]["reused"])
            self.assertFalse(gate_data["checks"]["formal_inv"]["reused"])

            summary = (run_dir / "summary.md").read_text(encoding="utf-8")
            self.assertIn("<!-- garden:v1 kind=harvest ", summary)

            # Second identical run must be skipped as duplicate
            proc2 = run_cli("run", "full_verify", "--target", str(repo), "--json")
            self.assertEqual(proc2.returncode, 0)
            out2 = json.loads(proc2.stdout)
            self.assertTrue(out2["skipped_duplicate"])

            # --force overrides duplicate skip
            proc3 = run_cli("run", "full_verify", "--target", str(repo), "--force", "--json")
            self.assertEqual(proc3.returncode, 0)
            self.assertFalse(json.loads(proc3.stdout)["skipped_duplicate"])

    def test_mvp1_unsatisfied_and_retry_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_fixture_repo(Path(tmp) / "repo", app_value="BROKEN")
            proc = run_cli("run", "full_verify", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(json.loads(proc.stdout)["verdict"], "UNSATISFIED")

            # Non-SATISFIED runs must NOT be skipped on retry
            proc2 = run_cli("run", "full_verify", "--target", str(repo), "--json")
            self.assertEqual(proc2.returncode, 1)
            self.assertFalse(json.loads(proc2.stdout)["skipped_duplicate"])

    def test_mvp1_inconclusive_cases(self):
        # 1) Exit code 75
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_fixture_repo(Path(tmp) / "r1", unit_script="import sys; sys.exit(75)")
            proc = run_cli("run", "full_verify", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 3)
            self.assertEqual(json.loads(proc.stdout)["verdict"], "INCONCLUSIVE")

        # 2) Missing required evidence
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_fixture_repo(Path(tmp) / "r2", evidence_script="pass")
            proc = run_cli("run", "full_verify", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 3)
            self.assertEqual(json.loads(proc.stdout)["verdict"], "INCONCLUSIVE")

        # 3) Formal check completed=False
        with tempfile.TemporaryDirectory() as tmp:
            incomplete_formal = (
                "import json, os; from pathlib import Path; "
                "(Path(os.environ['GARDEN_EVIDENCE_DIR']) / 'formal.json').write_text("
                "json.dumps({'schema_version':1,'tool':'quint','model_path':'formal/m.qnt',"
                "'invariants':['INV-001'],'assumptions':[],'scope':'s','completed':False,'result':'inconclusive'}))"
            )
            repo = _build_fixture_repo(Path(tmp) / "r3", formal_script=incomplete_formal)
            proc = run_cli("run", "full_verify", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 3)
            self.assertEqual(json.loads(proc.stdout)["verdict"], "INCONCLUSIVE")

        # 4) Command unavailable (missing binary)
        with tempfile.TemporaryDirectory() as tmp:
            extra_checks = """
[checks.missing_bin]
kind = "check"
command = ["nonexistent-garden-binary-xyz-999"]
covers = ["AC-001"]
"""
            extra_flows = """
[flows.bad_bin]
[[flows.bad_bin.steps]]
id = "s_bad"
type = "check"
check = "missing_bin"
"""
            repo = _build_fixture_repo(
                Path(tmp) / "r4",
                extra_checks_toml=extra_checks,
                extra_flows_toml=extra_flows,
            )
            proc = run_cli("run", "bad_bin", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 3)
            self.assertEqual(json.loads(proc.stdout)["verdict"], "INCONCLUSIVE")

        # 5) Timeout
        with tempfile.TemporaryDirectory() as tmp:
            py = json.dumps(sys.executable)
            extra_checks = f"""
[checks.slow]
kind = "check"
command = [{py}, "-c", "import time; time.sleep(3)"]
timeout_sec = 1
covers = ["AC-001"]
"""
            extra_flows = """
[flows.timeout_flow]
[[flows.timeout_flow.steps]]
id = "s_slow"
type = "check"
check = "slow"
"""
            repo = _build_fixture_repo(
                Path(tmp) / "r5",
                extra_checks_toml=extra_checks,
                extra_flows_toml=extra_flows,
            )
            proc = run_cli("run", "timeout_flow", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 3)
            self.assertEqual(json.loads(proc.stdout)["verdict"], "INCONCLUSIVE")

    def test_mvp1_unapproved_spec_returns_needs_human_exit_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_fixture_repo(Path(tmp) / "repo", spec_status="draft")
            proc = run_cli("run", "full_verify", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 4)
            out = json.loads(proc.stdout)
            self.assertEqual(out["verdict"], "NEEDS_HUMAN")
            self.assertEqual(out["run"]["escalation"]["reason"], "spec_not_approved")

    def test_partial_flow_scope_and_abort_exit_5(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_fixture_repo(Path(tmp) / "repo", app_value="OK")
            proc = run_cli("run", "partial_verify", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["verdict"], "SATISFIED")
            self.assertEqual(
                out["run"]["criteria_results"]["INV-001"]["verdict"], "not_evaluated"
            )

        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_fixture_repo(Path(tmp) / "repo_fail", app_value="BROKEN")
            proc = run_cli("run", "partial_verify", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 5)
            self.assertEqual(json.loads(proc.stdout)["verdict"], "ABORTED")

    def test_mvp2_fix_and_retry_succeeds_and_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_fixture_repo(Path(tmp) / "repo", app_value="BROKEN")
            proc = run_cli("run", "verify_and_fix", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["verdict"], "SATISFIED")
            self.assertEqual(out["run"]["retries_used"], 1)
            self.assertAlmostEqual(out["run"]["usage"]["cost_usd"], 0.05, places=4)
            # Original working tree remains clean/unchanged because execution ran in isolated worktree
            self.assertEqual((repo / "src" / "app.txt").read_text().strip(), "BROKEN")

    def test_mvp2_agent_forbidden_path_edit_caught_even_if_committed(self):
        sneaky_agent = (
            "import json, os, subprocess; from pathlib import Path; "
            "wt = Path(os.environ['GARDEN_WORKTREE']); "
            "(wt / 'docs' / 'specs' / 'app.md').write_text('hacked\\n'); "
            "subprocess.run(['git','-C',str(wt),'add','-A'], check=True); "
            "subprocess.run(['git','-C',str(wt),'commit','-m','sneaky'], check=True); "
            "Path(os.environ['GARDEN_AGENT_RESPONSE']).write_text("
            "json.dumps({'schema_version':1,'outcome':'completed','summary':'edited',"
            "'changed_files':[],'usage':{'cost_usd':0.01}}))"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_fixture_repo(
                Path(tmp) / "repo", app_value="BROKEN", agent_script=sneaky_agent
            )
            proc = run_cli("run", "verify_and_fix", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 4)
            out = json.loads(proc.stdout)
            self.assertEqual(out["verdict"], "NEEDS_HUMAN")
            self.assertEqual(out["run"]["escalation"]["reason"], "delegation_violation")

    def test_mvp2_agent_needs_spec_decision_emits_spec_question(self):
        question_agent = (
            "import json, os; from pathlib import Path; "
            "Path(os.environ['GARDEN_AGENT_RESPONSE']).write_text("
            "json.dumps({'schema_version':1,'outcome':'needs_spec_decision',"
            "'summary':'conflict behavior unspecified',"
            "'question':{'prompt':'Last write wins or merge?','options':['LWW','merge'],"
            "'impact':'affects concurrent users','blocked_work':'fix_code'}}))"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_fixture_repo(
                Path(tmp) / "repo", app_value="BROKEN", agent_script=question_agent
            )
            proc = run_cli("run", "verify_and_fix", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 4)
            out = json.loads(proc.stdout)
            self.assertEqual(out["verdict"], "NEEDS_HUMAN")
            summary = (Path(out["run_dir"]) / "summary.md").read_text(encoding="utf-8")
            self.assertIn("<!-- garden:v1 kind=spec_question ", summary)
            self.assertIn("Last write wins or merge?", summary)

    def test_pr_tampering_with_garden_toml_disables_auto_merge_and_harvest(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_fixture_repo(Path(tmp) / "repo", app_value="OK")
            git(repo, "switch", "-c", "feat/modify-def")
            def_file = repo / ".garden" / "garden.toml"
            def_file.write_text(
                def_file.read_text(encoding="utf-8") + "\n# minor comment change\n",
                encoding="utf-8",
            )
            commit_all(repo, "touch garden.toml")

            proc = run_cli("run", "verify_and_fix", "--target", str(repo), "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertTrue(out["run"]["target"]["evaluation_criteria_changed"])
            # auto_merge must be downgraded to propose_pr
            self.assertEqual(out["run"]["effective_delegation"]["mode"], "propose_pr")
            summary = (Path(out["run_dir"]) / "summary.md").read_text(encoding="utf-8")
            # Must NOT emit harvest when evaluation criteria changed
            self.assertIn("<!-- garden:v1 kind=verified ", summary)
