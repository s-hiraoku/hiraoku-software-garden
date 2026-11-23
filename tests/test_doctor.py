import json
from pathlib import Path
import tempfile
import unittest

from tests.helpers import PSTACK_SHA, commit_all, init_target_repo, run_cli, sample_spec


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_ok_and_warns_cleanly_on_valid_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_target_repo(Path(tmp) / "repo")
            (repo / "docs" / "specs").mkdir(parents=True)
            (repo / "docs" / "specs" / "app.md").write_text(
                sample_spec("approved"), encoding="utf-8"
            )
            (repo / ".garden").mkdir()
            (repo / ".garden" / ".gitignore").write_text("runs/\nworktrees/\n", encoding="utf-8")
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
name = "h"
version = 1
[spec]
paths = ["docs/specs/app.md"]
[delegation]
mode = "observe_only"
max_retries = 0
max_duration_sec = 60
max_cost_usd = 0.0
[checks.c1]
command = ["python3", "-c", "pass"]
covers = ["AC-001"]
[flows.verify]
[[flows.verify.steps]]
id = "s1"
type = "check"
check = "c1"
""",
                encoding="utf-8",
            )
            commit_all(repo, "init")

            proc = run_cli("doctor", "--target", str(repo), "--skip-network", "--json")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = json.loads(proc.stdout)
            by_id = {item["id"]: item["status"] for item in data["items"]}
            self.assertEqual(by_id["git_repo"], "ok")
            self.assertEqual(by_id["garden_definition"], "ok")
            self.assertEqual(by_id["specs_and_trust"], "ok")
            self.assertEqual(by_id["check_commands"], "ok")
            self.assertEqual(by_id["garden_gitignore"], "ok")
            self.assertIn("branch_protection", by_id)

    def test_doctor_flags_missing_executable_and_unapproved_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_target_repo(Path(tmp) / "repo")
            (repo / "docs" / "specs").mkdir(parents=True)
            (repo / "docs" / "specs" / "app.md").write_text(
                sample_spec("draft"), encoding="utf-8"
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
name = "h"
version = 1
[spec]
paths = ["docs/specs/app.md"]
[delegation]
mode = "observe_only"
max_retries = 0
max_duration_sec = 60
max_cost_usd = 0.0
[checks.c1]
command = ["definitely-missing-cmd-xyz"]
covers = ["AC-001"]
[flows.verify]
[[flows.verify.steps]]
id = "s1"
type = "check"
check = "c1"
""",
                encoding="utf-8",
            )
            commit_all(repo, "draft")

            proc = run_cli("doctor", "--target", str(repo), "--skip-network", "--json")
            self.assertEqual(proc.returncode, 1)
            by_id = {item["id"]: item["status"] for item in json.loads(proc.stdout)["items"]}
            self.assertEqual(by_id["specs_and_trust"], "error")
            self.assertEqual(by_id["check_commands"], "error")

    def test_doctor_warns_when_spec_approval_branch_differs_from_trusted_arg(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_target_repo(Path(tmp) / "repo")
            (repo / "docs" / "specs").mkdir(parents=True)
            (repo / "docs" / "specs" / "app.md").write_text(
                sample_spec("approved"), encoding="utf-8"
            )
            (repo / ".garden").mkdir()
            (repo / ".garden" / ".gitignore").write_text("runs/\nworktrees/\n", encoding="utf-8")
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
name = "h"
version = 1
[spec]
paths = ["docs/specs/app.md"]
approval_branch = "other-branch"
[delegation]
mode = "observe_only"
max_retries = 0
max_duration_sec = 60
max_cost_usd = 0.0
[checks.c1]
command = ["python3", "-c", "pass"]
covers = ["AC-001"]
[flows.verify]
[[flows.verify.steps]]
id = "s1"
type = "check"
check = "c1"
""",
                encoding="utf-8",
            )
            commit_all(repo, "mismatched approval_branch")

            proc = run_cli("doctor", "--target", str(repo), "--skip-network", "--json")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            items = json.loads(proc.stdout)["items"]
            by_id = {item["id"]: item for item in items}
            self.assertEqual(by_id["approval_branch_alignment"]["status"], "warn")
            self.assertIn("for 'main'", by_id["branch_protection"]["detail"])
