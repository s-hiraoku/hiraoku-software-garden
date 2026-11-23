"""Tests for the template release gate and release-evidence.json generation."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from garden.release_gate import run_template_release_gate
from tests.helpers import run_cli


class TemplateReleaseGateTests(unittest.TestCase):
    def test_release_gate_passes_for_all_four_templates_and_writes_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence_path = Path(tmp) / "out" / "release-evidence.json"
            evidence = run_template_release_gate(
                all_templates=True,
                output_path=evidence_path,
            )
            self.assertTrue(evidence["overall_passed"], evidence)
            names = [r["template"] for r in evidence["results"]]
            self.assertEqual(names, ["cli", "http-api", "library", "web-frontend"])

            for item in evidence["results"]:
                self.assertTrue(item["passed"], item)
                self.assertEqual(item["fresh_init"]["doctor_exit_code"], 0)
                self.assertEqual(item["fresh_init"]["pass_verdict"], "SATISFIED")
                self.assertEqual(item["fresh_init"]["fail_verdict"], "UNSATISFIED")
                self.assertIn(
                    "custom_operator_check", item["upgrade"]["preserved_custom_checks"]
                )
                self.assertIn(
                    "release_gate_candidate", item["upgrade"]["proposed_new_checks"]
                )

            self.assertTrue(evidence_path.is_file())
            loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["schema_version"], 1)
            self.assertTrue(loaded["overall_passed"])

    def test_cli_template_gate_single_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "cli-evidence.json"
            proc = run_cli("template-gate", "--type", "cli", "--output", str(out_file))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["overall_passed"])
            self.assertEqual(len(payload["results"]), 1)
            self.assertTrue(out_file.is_file())
