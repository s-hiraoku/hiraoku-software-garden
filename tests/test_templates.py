import json
from pathlib import Path
import shutil
import tempfile
import unittest

from garden.definition import load_definition
from garden.templates import apply_upgrade, default_nursery_root, plan_upgrade
from tests.helpers import PSTACK_SHA, commit_all, init_target_repo, run_cli


class TemplateLifecycleTests(unittest.TestCase):
    def test_mvp3_fresh_init_doctor_and_run_pass_and_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_target_repo(Path(tmp) / "my-cli-tool")
            (repo / "pyproject.toml").write_text(
                "[project]\nname = 'my-cli'\n[project.scripts]\nmy-cli = 'pkg:main'\n",
                encoding="utf-8",
            )
            commit_all(repo, "base cli repo")

            # Auto-detect cli template and initialize
            init_proc = run_cli("init", "--target", str(repo))
            self.assertEqual(init_proc.returncode, 0, init_proc.stderr)
            init_data = json.loads(init_proc.stdout)
            self.assertEqual(init_data["template"], "cli")
            self.assertEqual(init_data["version"], 1)

            # Approve initial spec on main
            spec_path = repo / "docs" / "specs" / "my-cli-tool.md"
            spec_text = spec_path.read_text(encoding="utf-8").replace(
                "status: draft", "status: approved"
            )
            spec_path.write_text(spec_text, encoding="utf-8")
            # Remove INV-001 from initial spec or keep AC-001 covered by smoke
            # (smoke covers AC-001; for full-pass test update smoke to cover INV-001 too)
            gpath = repo / ".garden" / "garden.toml"
            gpath.write_text(
                gpath.read_text(encoding="utf-8").replace(
                    'covers = ["AC-001"]', 'covers = ["AC-001", "INV-001"]'
                ),
                encoding="utf-8",
            )
            commit_all(repo, "approve spec and garden")

            doc_proc = run_cli("doctor", "--target", str(repo), "--skip-network", "--json")
            self.assertEqual(doc_proc.returncode, 0, doc_proc.stdout + doc_proc.stderr)

            # Passing run -> SATISFIED
            run_ok = run_cli("run", "verify_only", "--target", str(repo), "--json")
            self.assertEqual(run_ok.returncode, 0, run_ok.stderr)
            self.assertEqual(json.loads(run_ok.stdout)["verdict"], "SATISFIED")

            # Failing run -> UNSATISFIED
            gpath.write_text(
                gpath.read_text(encoding="utf-8").replace(
                    "print('replace with CLI smoke check')",
                    "import sys; sys.exit(1)",
                ),
                encoding="utf-8",
            )
            commit_all(repo, "make smoke fail")
            run_fail = run_cli("run", "verify_only", "--target", str(repo), "--force", "--json")
            self.assertEqual(run_fail.returncode, 1)
            self.assertEqual(json.loads(run_fail.stdout)["verdict"], "UNSATISFIED")

    def test_mvp3_upgrade_preserves_custom_checks_and_proposes_new_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Create a temporary Nursery copy with cli/v1 and a candidate cli/v2
            nursery_copy = tmp_path / "nursery"
            shutil.copytree(
                default_nursery_root() / "templates", nursery_copy / "templates"
            )
            v2_dir = nursery_copy / "templates" / "cli" / "v2"
            shutil.copytree(nursery_copy / "templates" / "cli" / "v1", v2_dir)
            (v2_dir / "template.toml").write_text(
                f"""
schema_version = 1
name = "cli"
version = 2
summary = "CLI template v2"

[common_harness]
name = "pstack"
ref = "{PSTACK_SHA}"

[defaults.checks.smoke]
kind = "check"
command = ["python3", "-c", "print('replace with CLI smoke check')"]
covers = ["AC-001"]

[defaults.checks.help_flag]
kind = "check"
command = ["python3", "-c", "print('--help ok')"]
timeout_sec = 60
covers = ["AC-001"]

[defaults.checks.a11y_output]
kind = "check"
command = ["python3", "-c", "print('no-color ok')"]
timeout_sec = 60
covers = ["AC-099"]
""",
                encoding="utf-8",
            )

            # Target repo initialized at v1 with operator custom check + modified smoke
            repo = init_target_repo(tmp_path / "target-cli")
            (repo / "docs" / "specs").mkdir(parents=True)
            (repo / "docs" / "specs" / "app.md").write_text(
                "---\nspec_id: app\nversion: 1\nstatus: approved\ntitle: App\n---\n\n- **AC-001**: ok\n",
                encoding="utf-8",
            )
            (repo / ".garden").mkdir()
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
name = "custom"
version = 1

[spec]
paths = ["docs/specs/app.md"]

[delegation]
mode = "observe_only"
max_retries = 0
max_duration_sec = 120
max_cost_usd = 0.0

[checks.smoke]
kind = "check"
command = ["python3", "-c", "print('operator customized smoke')"]
covers = ["AC-001"]

[checks.operator_custom]
kind = "check"
command = ["python3", "-c", "print('custom check')"]
covers = ["AC-001"]

[flows.verify_only]
scope = "all_specs"
[[flows.verify_only.steps]]
id = "s1"
type = "check"
check = "operator_custom"
""",
                encoding="utf-8",
            )
            commit_all(repo, "v1 customized")

            plan = plan_upgrade(repo, to_version=2, nursery_root=nursery_copy)
            self.assertEqual(plan.preserved_custom_checks, ("operator_custom",))
            self.assertEqual(plan.modified_template_checks, ("smoke",))
            proposed_by_name = {p.name: p for p in plan.proposed_new_checks}
            self.assertEqual(
                proposed_by_name["help_flag"].classification, "within_existing_spec"
            )
            self.assertEqual(
                proposed_by_name["a11y_output"].classification, "requires_spec_change"
            )
            self.assertEqual(proposed_by_name["a11y_output"].missing_criteria, ("AC-099",))

            apply_upgrade(repo, to_version=2, nursery_root=nursery_copy)
            upgraded = load_definition(repo / ".garden" / "garden.toml")
            self.assertEqual(upgraded.template.version, 2)
            self.assertIn("operator_custom", upgraded.checks)
            self.assertEqual(
                upgraded.checks["smoke"].command,
                ("python3", "-c", "print('operator customized smoke')"),
            )
            self.assertEqual(upgraded.checks["help_flag"].status, "proposed")
            self.assertEqual(upgraded.checks["a11y_output"].status, "proposed")

            # After committing upgrade, existing flow still runs and passes cleanly
            commit_all(repo, "upgrade to v2")
            run_proc = run_cli("run", "verify_only", "--target", str(repo), "--json")
            self.assertEqual(run_proc.returncode, 0, run_proc.stderr)
            self.assertEqual(json.loads(run_proc.stdout)["verdict"], "SATISFIED")

    def test_all_four_templates_autodetect_init_doctor_and_run_pass_and_fail(self):
        cases = [
            (
                "cli",
                "sample-cli",
                {"pyproject.toml": '[project]\nname="x"\n[project.scripts]\nx="x:main"\n'},
                "replace with CLI smoke check",
            ),
            (
                "web-frontend",
                "sample-web",
                {"package.json": '{"name":"w","dependencies":{"react":"^19.0.0","vite":"^6.0.0"}}'},
                "replace with Playwright/UI smoke check",
            ),
            (
                "http-api",
                "sample-api",
                {"pyproject.toml": '[project]\nname="a"\ndependencies=["fastapi"]\n', "openapi.yaml": "openapi: 3.1.0\n"},
                "replace with HTTP API contract check",
            ),
            (
                "library",
                "sample-lib",
                {"pyproject.toml": '[build-system]\nrequires=["setuptools"]\n[project]\nname="lib"\n'},
                "replace with public API contract check",
            ),
        ]
        for expected_type, repo_name, seed_files, placeholder in cases:
            with self.subTest(template=expected_type):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = init_target_repo(Path(tmp) / repo_name)
                    for rel, content in seed_files.items():
                        (repo / rel).write_text(content, encoding="utf-8")
                    commit_all(repo, f"seed {expected_type}")

                    init_proc = run_cli("init", "--target", str(repo))
                    self.assertEqual(init_proc.returncode, 0, init_proc.stderr)
                    init_out = json.loads(init_proc.stdout)
                    self.assertEqual(init_out["template"], expected_type)
                    self.assertEqual(init_out["version"], 1)

                    spec_path = repo / "docs" / "specs" / f"{repo_name}.md"
                    spec_path.write_text(
                        spec_path.read_text(encoding="utf-8").replace(
                            "status: draft", "status: approved"
                        ),
                        encoding="utf-8",
                    )
                    gpath = repo / ".garden" / "garden.toml"
                    gpath.write_text(
                        gpath.read_text(encoding="utf-8").replace(
                            'covers = ["AC-001"]', 'covers = ["AC-001", "INV-001"]'
                        ),
                        encoding="utf-8",
                    )
                    commit_all(repo, "approve spec")

                    doc_proc = run_cli(
                        "doctor", "--target", str(repo), "--skip-network", "--json"
                    )
                    self.assertEqual(doc_proc.returncode, 0, doc_proc.stdout + doc_proc.stderr)

                    ok_proc = run_cli("run", "verify_only", "--target", str(repo), "--json")
                    self.assertEqual(ok_proc.returncode, 0, ok_proc.stderr)
                    self.assertEqual(json.loads(ok_proc.stdout)["verdict"], "SATISFIED")

                    gpath.write_text(
                        gpath.read_text(encoding="utf-8").replace(
                            f"print('{placeholder}')", "import sys; sys.exit(1)"
                        ),
                        encoding="utf-8",
                    )
                    commit_all(repo, "break check")
                    fail_proc = run_cli(
                        "run", "verify_only", "--target", str(repo), "--force", "--json"
                    )
                    self.assertEqual(fail_proc.returncode, 1)
                    self.assertEqual(json.loads(fail_proc.stdout)["verdict"], "UNSATISFIED")

    def test_init_supports_nursery_ref_version_pinning_and_rejects_invalid_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_target_repo(Path(tmp) / "pinned-cli")
            (repo / "README.md").write_text("# pinned-cli\n", encoding="utf-8")
            commit_all(repo, "base")

            bad = run_cli(
                "init", "--target", str(repo), "--type", "cli", "--nursery-ref", "../bad"
            )
            self.assertEqual(bad.returncode, 2)

            ok = run_cli(
                "init", "--target", str(repo), "--type", "cli", "--nursery-ref", "v0.1.0"
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)
            out = json.loads(ok.stdout)
            self.assertEqual(out["nursery_ref"], "v0.1.0")

            wf_text = (repo / ".github" / "workflows" / "garden.yml").read_text(
                encoding="utf-8"
            )
            self.assertIn("garden-reusable.yml@v0.1.0", wf_text)
            self.assertIn("garden-feedback-reusable.yml@v0.1.0", wf_text)
            self.assertIn('nursery_ref: "v0.1.0"', wf_text)
            self.assertNotIn("__NURSERY_REF__", wf_text)
