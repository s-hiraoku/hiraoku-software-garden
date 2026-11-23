import json
from pathlib import Path
import tempfile
import unittest

from garden.report import label_transition_for
from tests.helpers import run_cli


class FeedbackAndReportTests(unittest.TestCase):
    def test_feedback_add_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            p1 = run_cli(
                "feedback",
                "add",
                "--target",
                str(repo),
                "--run-id",
                "20261123T090000Z-11111111",
                "--category",
                "harness_bug",
                "--summary",
                "process crash misclassified as timeout",
                "--triage-step",
                "checked daemon logs",
                "--preventive-action",
                "record startup exit code",
                "--status",
                "verified",
            )
            self.assertEqual(p1.returncode, 0, p1.stderr)
            rec = json.loads(p1.stdout)["record"]
            self.assertEqual(rec["category"], "harness_bug")
            self.assertEqual(rec["status"], "verified")

            p2 = run_cli("feedback", "summary", "--target", str(repo))
            self.assertEqual(p2.returncode, 0, p2.stderr)
            summary = json.loads(p2.stdout)
            self.assertEqual(summary["total"], 1)
            self.assertEqual(summary["by_category"]["harness_bug"], 1)
            self.assertEqual(summary["by_status"]["verified"], 1)

    def test_report_renders_md_json_and_label_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            run_dir.mkdir()
            run_data = {
                "schema_version": 1,
                "run_id": "20261123T090000Z-abcd1234",
                "idempotency_key": "abcd1234abcd1234",
                "flow": "verify_and_fix",
                "flow_scope": "all_specs",
                "verdict": "SATISFIED",
                "target": {
                    "commit": "0123456789abcdef",
                    "evaluation_criteria_changed": False,
                },
                "versions": {"specs": []},
                "criteria_results": {},
                "escalation": None,
                "duration_sec": 1.2,
                "retries_used": 0,
                "usage": {"cost_usd": 0.0},
            }
            (run_dir / "run.json").write_text(json.dumps(run_data), encoding="utf-8")

            add_l, rem_l = label_transition_for(run_data)
            self.assertEqual(add_l, ["garden:harvest-ready"])
            self.assertIn("garden:running", rem_l)
            self.assertIn("garden:needs-human", rem_l)

            md_proc = run_cli("report", str(run_dir), "--format", "md")
            self.assertEqual(md_proc.returncode, 0, md_proc.stderr)
            self.assertIn("<!-- garden:v1 kind=harvest ", md_proc.stdout)

            js_proc = run_cli("report", str(run_dir), "--format", "json")
            self.assertEqual(js_proc.returncode, 0, js_proc.stderr)
            self.assertEqual(json.loads(js_proc.stdout)["verdict"], "SATISFIED")
