"""Tests for criterion-level specification diff and check/flow impact analysis."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tests.helpers import PSTACK_SHA, commit_all, git, init_target_repo, run_cli


SPEC_V1 = """---
spec_id: drafts
version: 1
status: approved
title: 下書き保存機能
owner: "@s-hiraoku"
---

## 受け入れ条件
- **AC-001**: フォーム入力を下書き保存し、リロード後に復元できる。
- **AC-002**: 送信完了後に下書きを削除する。

## 不変条件
- **INV-001**: 他ユーザーの下書きを読み書きできない。
"""

SPEC_V2 = """---
spec_id: drafts
version: 2
status: approved
title: 下書き保存機能（マルチ端末対応）
owner: "@s-hiraoku"
---

## 受け入れ条件
- **AC-001**: フォーム入力を下書き保存し、同一端末および別端末から復元できる。
- **AC-003**: 同時編集競合時は最新保存を優先し、通知を表示する。

## 不変条件
- **INV-001**: 他ユーザーの下書きを読み書きできない。
"""


def _build_spec_repo(repo: Path) -> None:
    init_target_repo(repo)
    (repo / ".garden").mkdir()
    (repo / ".garden" / ".gitignore").write_text("runs/\nworktrees/\n", encoding="utf-8")
    (repo / "docs" / "specs").mkdir(parents=True)
    (repo / "docs" / "specs" / "drafts.md").write_text(SPEC_V1, encoding="utf-8")
    (repo / ".garden" / "garden.toml").write_text(
        f"""\
schema_version = 1

[template]
name = "cli"
version = 1

[common_harness]
name = "pstack"
ref = "{PSTACK_SHA}"

[target_harness]
name = "drafts-harness"
version = 1

[spec]
paths = ["docs/specs/drafts.md"]
approval_branch = "main"

[delegation]
mode = "observe_only"
allowed_paths = []
forbidden_paths = [".garden/**", "docs/specs/**", ".github/workflows/**"]
max_retries = 0
max_duration_sec = 60
max_cost_usd = 0.0
require_checks = ["restore_check", "cleanup_check", "auth_inv"]

[checks.restore_check]
kind = "check"
command = ["python3", "-c", "print('ok')"]
timeout_sec = 30
covers = ["AC-001"]
status = "active"

[checks.cleanup_check]
kind = "check"
command = ["python3", "-c", "print('ok')"]
timeout_sec = 30
covers = ["AC-002"]
status = "active"

[checks.auth_inv]
kind = "check"
command = ["python3", "-c", "print('ok')"]
timeout_sec = 30
covers = ["INV-001"]
status = "active"

[flows.fast_restore]
scope = "flow_checks"

[[flows.fast_restore.steps]]
id = "run_restore"
type = "check"
check = "restore_check"
on_fail = "escalate"
on_inconclusive = "escalate"

[flows.full_verify]
scope = "all_specs"

[[flows.full_verify.steps]]
id = "run_restore"
type = "check"
check = "restore_check"
on_fail = "escalate"
on_inconclusive = "escalate"

[[flows.full_verify.steps]]
id = "run_cleanup"
type = "check"
check = "cleanup_check"
on_fail = "escalate"
on_inconclusive = "escalate"

[[flows.full_verify.steps]]
id = "run_auth"
type = "check"
check = "auth_inv"
on_fail = "escalate"
on_inconclusive = "escalate"

[[flows.full_verify.steps]]
id = "gate"
type = "gate"
require_checks = ["restore_check", "cleanup_check", "auth_inv"]
""",
        encoding="utf-8",
    )
    commit_all(repo, "spec v1 approved")


class SpecDiffTests(unittest.TestCase):
    def test_unchanged_spec_returns_zero_with_no_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "drafts"
            _build_spec_repo(repo)
            proc = run_cli("spec-diff", "--target", str(repo), "--base", "main")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            self.assertFalse(data["has_spec_changes"])
            self.assertFalse(data["impact"]["requires_garden_update"])
            self.assertEqual(data["summary"]["unchanged_count"], 3)

    def test_spec_revision_detects_added_removed_modified_and_maps_impacted_checks_and_flows(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "drafts"
            _build_spec_repo(repo)
            v1_sha = git(repo, "rev-parse", "HEAD").strip()

            # Edit spec in working tree (simulating a spec revision PR)
            (repo / "docs" / "specs" / "drafts.md").write_text(SPEC_V2, encoding="utf-8")
            proc = run_cli("spec-diff", "--target", str(repo), "--base", "main")
            # Exit 1 because AC-003 is uncovered and cleanup_check is orphaned (AC-002 removed)
            self.assertEqual(proc.returncode, 1)
            data = json.loads(proc.stdout)
            self.assertTrue(data["has_spec_changes"])
            self.assertEqual(
                data["summary"],
                {"added_count": 1, "removed_count": 1, "modified_count": 1, "unchanged_count": 1},
            )
            self.assertEqual(data["impact"]["uncovered_added_criteria"], ["AC-003"])
            self.assertEqual(
                [o["check"] for o in data["impact"]["orphaned_checks"]],
                ["cleanup_check"],
            )
            impacted_checks = {c["check"]: c for c in data["impact"]["impacted_checks"]}
            self.assertIn("restore_check", impacted_checks)
            self.assertEqual(impacted_checks["restore_check"]["modified_criteria"], ["AC-001"])
            self.assertNotIn("auth_inv", impacted_checks)

            impacted_flows = {f["flow"] for f in data["impact"]["impacted_flows"]}
            self.assertEqual(impacted_flows, {"fast_restore", "full_verify"})
            self.assertTrue(data["impact"]["requires_garden_update"])

            # Commit v2 on main and verify --base <v1_sha> --head main works identically
            commit_all(repo, "approve spec v2")
            hist_proc = run_cli(
                "spec-diff", "--target", str(repo), "--base", v1_sha, "--head", "main"
            )
            self.assertEqual(hist_proc.returncode, 1)
            hist_data = json.loads(hist_proc.stdout)
            self.assertEqual(hist_data["impact"]["uncovered_added_criteria"], ["AC-003"])

    def test_shared_criterion_id_removed_from_one_spec_does_not_orphan_check_covered_by_another_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "multi-spec"
            init_target_repo(repo)
            (repo / ".garden").mkdir()
            (repo / ".garden" / ".gitignore").write_text("runs/\nworktrees/\n", encoding="utf-8")
            (repo / "docs" / "specs").mkdir(parents=True)
            (repo / "docs" / "specs" / "spec_a.md").write_text(
                "---\nspec_id: spec-a\nversion: 1\nstatus: approved\ntitle: Spec A\n---\n\n"
                "- **AC-001**: shared id in A\n- **AC-002**: only in A\n",
                encoding="utf-8",
            )
            (repo / "docs" / "specs" / "spec_b.md").write_text(
                "---\nspec_id: spec-b\nversion: 1\nstatus: approved\ntitle: Spec B\n---\n\n"
                "- **AC-001**: shared id in B\n",
                encoding="utf-8",
            )
            (repo / ".garden" / "garden.toml").write_text(
                f"""\
schema_version = 1

[template]
name = "cli"
version = 1

[common_harness]
name = "pstack"
ref = "{PSTACK_SHA}"

[target_harness]
name = "multi-harness"
version = 1

[spec]
paths = ["docs/specs/spec_a.md", "docs/specs/spec_b.md"]
approval_branch = "main"

[delegation]
mode = "observe_only"
allowed_paths = []
forbidden_paths = [".garden/**", "docs/specs/**", ".github/workflows/**"]
max_retries = 0
max_duration_sec = 60
max_cost_usd = 0.0
require_checks = ["check_ac1", "check_ac2"]

[checks.check_ac1]
kind = "check"
command = ["python3", "-c", "print('ok')"]
timeout_sec = 30
covers = ["AC-001"]
status = "active"

[checks.check_ac2]
kind = "check"
command = ["python3", "-c", "print('ok')"]
timeout_sec = 30
covers = ["AC-002"]
status = "active"

[flows.verify]
scope = "all_specs"

[[flows.verify.steps]]
id = "c1"
type = "check"
check = "check_ac1"
on_fail = "escalate"
on_inconclusive = "escalate"

[[flows.verify.steps]]
id = "c2"
type = "check"
check = "check_ac2"
on_fail = "escalate"
on_inconclusive = "escalate"
""",
                encoding="utf-8",
            )
            commit_all(repo, "initial two specs")

            # Delete AC-001 from spec_a.md only; AC-001 remains defined in spec_b.md
            (repo / "docs" / "specs" / "spec_a.md").write_text(
                "---\nspec_id: spec-a\nversion: 2\nstatus: approved\ntitle: Spec A\n---\n\n"
                "- **AC-002**: only in A\n",
                encoding="utf-8",
            )

            proc = run_cli("spec-diff", "--target", str(repo), "--base", "main")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = json.loads(proc.stdout)
            self.assertTrue(data["has_spec_changes"])
            self.assertEqual(data["impact"]["orphaned_checks"], [])
            self.assertFalse(data["impact"]["requires_garden_update"])
            spec_a_diff = next(s for s in data["specs"] if s["spec_id"] == "spec-a")
            self.assertEqual([r["id"] for r in spec_a_diff["removed"]], ["AC-001"])
