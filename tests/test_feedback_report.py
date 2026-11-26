import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from garden.artifacts import render_summary_markdown
from garden.report import label_transition_for, post_github_report
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

    def test_summary_renders_artifacts_url_and_clean_usd_formatting(self):
        run_data = {
            "schema_version": 1,
            "run_id": "20261125T000000Z-abcd1234",
            "idempotency_key": "abcd1234abcd1234",
            "flow": "verify_relay",
            "flow_scope": "all_specs",
            "verdict": "SATISFIED",
            "target": {"commit": "886c624000000000", "evaluation_criteria_changed": False},
            "versions": {"specs": []},
            "criteria_results": {},
            "escalation": None,
            "duration_sec": 5.1,
            "retries_used": 0,
            "usage": {"cost_usd": 0.0},
            "artifacts_url": "https://github.com/s-hiraoku/hiraoku-agent-base/actions/runs/45514998327",
        }
        md = render_summary_markdown(run_data)
        self.assertIn("費用 $0.00", md)
        self.assertNotIn("\\$0.00", md)
        self.assertIn(
            "- **証拠 Artifacts**: https://github.com/s-hiraoku/hiraoku-agent-base/actions/runs/45514998327",
            md,
        )

        # Fallback from GitHub Actions environment variables when artifacts_url is absent
        run_no_url = dict(run_data)
        run_no_url.pop("artifacts_url")
        with patch.dict(
            os.environ,
            {
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_REPOSITORY": "s-hiraoku/hiraoku-software-garden",
                "GITHUB_RUN_ID": "999",
            },
        ):
            md_env = render_summary_markdown(run_no_url)
        self.assertIn(
            "- **証拠 Artifacts**: https://github.com/s-hiraoku/hiraoku-software-garden/actions/runs/999",
            md_env,
        )

    def test_post_github_report_creates_missing_labels_and_only_removes_attached_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            run_dir.mkdir()
            run_data = {
                "schema_version": 1,
                "run_id": "20261125T000000Z-abcd1234",
                "idempotency_key": "abcd1234abcd1234",
                "flow": "verify_relay",
                "flow_scope": "all_specs",
                "verdict": "SATISFIED",
                "target": {"commit": "886c624000000000", "evaluation_criteria_changed": False},
                "versions": {"specs": []},
                "criteria_results": {},
                "escalation": None,
                "duration_sec": 5.1,
                "retries_used": 0,
                "usage": {"cost_usd": 0.0},
            }
            (run_dir / "run.json").write_text(json.dumps(run_data), encoding="utf-8")

            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                if cmd[:3] == ["gh", "issue", "comment"]:
                    return subprocess.CompletedProcess(
                        cmd, 0, "https://github.com/o/r/pull/1#issuecomment-1\n", ""
                    )
                if cmd[:3] == ["gh", "issue", "view"]:
                    return subprocess.CompletedProcess(
                        cmd, 0, json.dumps({"labels": [{"name": "garden:needs-human"}]}), ""
                    )
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with patch("garden.report.subprocess.run", side_effect=fake_run):
                res = post_github_report(run_dir, 1)

            self.assertIsNone(res["label_warning"])
            self.assertEqual(res["added_labels"], ["garden:harvest-ready"])
            self.assertEqual(res["removed_labels"], ["garden:needs-human"])
            self.assertTrue(
                any(c[:4] == ["gh", "label", "create", "garden:harvest-ready"] for c in calls)
            )
            self.assertTrue(
                any(
                    c == ["gh", "issue", "edit", "1", "--add-label", "garden:harvest-ready"]
                    for c in calls
                )
            )
            self.assertTrue(
                any(
                    c == ["gh", "issue", "edit", "1", "--remove-label", "garden:needs-human"]
                    for c in calls
                )
            )
