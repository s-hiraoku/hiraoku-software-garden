from pathlib import Path
import tempfile
import unittest

from garden.definition import DefinitionError
from garden.spec import SpecError, parse_spec_markdown, verify_trust_anchor
from tests.helpers import PSTACK_SHA, commit_all, git, init_target_repo, sample_spec


class SpecAndTrustAnchorTests(unittest.TestCase):
    def test_parse_spec_extracts_criteria_and_rejects_duplicates(self):
        doc = parse_spec_markdown(sample_spec("approved"), "docs/specs/app.md")
        self.assertEqual(doc.spec_id, "sample-app")
        self.assertEqual(doc.version, 1)
        self.assertEqual(doc.status, "approved")
        self.assertEqual(set(doc.acceptance_criteria.keys()), {"AC-001", "AC-002"})
        self.assertEqual(set(doc.invariants.keys()), {"INV-001"})

        dup = sample_spec("approved") + "\n- **AC-001**: duplicate\n"
        with self.assertRaisesRegex(SpecError, "duplicate acceptance criterion"):
            parse_spec_markdown(dup, "docs/specs/app.md")

    def test_trust_anchor_uses_approval_branch_definition_and_flags_any_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_target_repo(Path(tmp) / "repo")
            (repo / "docs" / "specs").mkdir(parents=True)
            (repo / "docs" / "specs" / "app.md").write_text(
                sample_spec("approved"), encoding="utf-8"
            )
            (repo / ".garden").mkdir()
            def_text = f"""
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
approval_branch = "main"
[delegation]
mode = "auto_merge"
allowed_paths = ["src/**"]
forbidden_paths = []
max_retries = 1
max_duration_sec = 300
max_cost_usd = 2.0
[checks.c1]
command = ["python3", "-c", "pass"]
covers = ["AC-001"]
[flows.verify]
scope = "flow_checks"
[[flows.verify.steps]]
id = "s1"
type = "check"
check = "c1"
[[flows.verify.steps]]
id = "g1"
type = "gate"
require_checks = ["c1"]
"""
            (repo / ".garden" / "garden.toml").write_text(def_text, encoding="utf-8")
            commit_all(repo, "initial approved")

            # On a feature branch, tamper with flow steps (drop gate)
            git(repo, "switch", "-c", "feat/tamper")
            tampered = def_text.replace(
                '[[flows.verify.steps]]\nid = "g1"\ntype = "gate"\nrequire_checks = ["c1"]\n',
                "",
            )
            (repo / ".garden" / "garden.toml").write_text(tampered, encoding="utf-8")
            commit_all(repo, "tamper flow")

            anchor = verify_trust_anchor(repo)
            self.assertTrue(anchor.specs_approved)
            self.assertTrue(anchor.evaluation_criteria_changed)
            # Active definition MUST come from approval_branch (gate still present!)
            step_ids = [s.id for s in anchor.active_definition.flows["verify"].steps]
            self.assertIn("g1", step_ids)

    def test_trust_anchor_flags_unapproved_draft_spec(self):
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
            commit_all(repo, "draft spec")

            anchor = verify_trust_anchor(repo)
            self.assertFalse(anchor.specs_approved)
            self.assertIn("status='draft'", anchor.spec_failure_reason or "")

    def test_trust_anchor_rejects_unknown_cover_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_target_repo(Path(tmp) / "repo")
            (repo / "docs" / "specs").mkdir(parents=True)
            (repo / "docs" / "specs" / "app.md").write_text(
                sample_spec("approved"), encoding="utf-8"
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
command = ["python3", "-c", "pass"]
covers = ["AC-999"]
[flows.verify]
[[flows.verify.steps]]
id = "s1"
type = "check"
check = "c1"
""",
                encoding="utf-8",
            )
            commit_all(repo, "bad cover")
            with self.assertRaisesRegex(DefinitionError, "AC-999"):
                verify_trust_anchor(repo)
