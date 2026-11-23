"""Dogfooding verification for the Nursery repository's own .garden/garden.toml and spec."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from garden.checks import run_check
from garden.definition import GARDEN_DEF_REL_PATH, load_definition
from garden.spec import parse_spec_markdown, validate_covers

REPO_ROOT = Path(__file__).resolve().parent.parent


class NurseryDogfoodTests(unittest.TestCase):
    def test_nursery_own_garden_definition_and_spec_are_valid_and_aligned(self):
        definition = load_definition(REPO_ROOT / GARDEN_DEF_REL_PATH)
        self.assertEqual(definition.template.name, "cli")
        self.assertEqual(definition.delegation.mode, "observe_only")
        self.assertIn("verify_all", definition.flows)
        self.assertEqual(definition.flows["verify_all"].scope, "all_specs")

        specs = [
            parse_spec_markdown(
                (REPO_ROOT / p).read_text(encoding="utf-8"), p
            )
            for p in definition.spec.paths
        ]
        validate_covers(definition, specs)

        # Every AC-* and INV-* in the Nursery spec must be covered by an active check
        covered: set[str] = set()
        for chk in definition.checks.values():
            if chk.status == "active":
                covered.update(chk.covers)
        for doc in specs:
            for cid in doc.all_criteria_ids:
                self.assertIn(cid, covered, f"uncovered criterion in Nursery spec: {cid}")

    def test_nursery_formal_check_emits_valid_formal_json_and_passes(self):
        definition = load_definition(REPO_ROOT / GARDEN_DEF_REL_PATH)
        formal_chk = definition.checks["formal_invariants"]
        self.assertEqual(formal_chk.kind, "formal_check")
        self.assertEqual(formal_chk.tool, "python-exhaustive")

        with tempfile.TemporaryDirectory() as tmp:
            step_dir = Path(tmp) / "formal_step"
            res = run_check(
                formal_chk,
                worktree=REPO_ROOT,
                run_id="dogfood-test",
                step_id="run_formal_invariants",
                step_dir=step_dir,
            )
            self.assertEqual(res.status, "passed", res.detail)
            self.assertIsNotNone(res.formal)
            assert res.formal is not None
            self.assertTrue(res.formal["completed"])
            self.assertEqual(res.formal["result"], "proved")
            self.assertEqual(res.formal["invariants"], ["INV-001", "INV-002"])
            formal_file = step_dir / "evidence" / "formal.json"
            self.assertTrue(formal_file.is_file())
            parsed = json.loads(formal_file.read_text(encoding="utf-8"))
            self.assertEqual(parsed["tool"], "python-exhaustive")
