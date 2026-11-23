"""Unit and CLI tests for /garden feedback comment parsing, updates, and operational metrics."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from garden.feedback import add_intervention, parse_feedback_comment, update_intervention
from garden.metrics import collect_garden_metrics
from tests.helpers import run_cli


class FeedbackAndMetricsTests(unittest.TestCase):
    def test_parse_feedback_comment_supports_inline_flags_and_multiline_fields(self):
        body = """\
Thanks for the run!

/garden feedback harness_bug "受信プロセスの起動失敗をタイムアウトと誤認していた" --triage "check exit code"
run_id: 20261123T084500Z-a1b2c3d4
triage: inspect stderr log
preventive: verify_relay.py に起動確認と終了コード記録を追加
ref: commit:abc1234
status: implemented
"""
        parsed = parse_feedback_comment(body)
        self.assertEqual(parsed["category"], "harness_bug")
        self.assertEqual(
            parsed["summary"], "受信プロセスの起動失敗をタイムアウトと誤認していた"
        )
        self.assertEqual(parsed["run_id"], "20261123T084500Z-a1b2c3d4")
        self.assertEqual(
            parsed["manual_triage_steps"], ["check exit code", "inspect stderr log"]
        )
        self.assertEqual(
            parsed["preventive_action"],
            "verify_relay.py に起動確認と終了コード記録を追加",
        )
        self.assertEqual(parsed["preventive_check_ref"], "commit:abc1234")
        self.assertEqual(parsed["status"], "implemented")

    def test_parse_feedback_comment_rejects_invalid_inputs(self):
        with self.assertRaises(ValueError):
            parse_feedback_comment("no command here")
        with self.assertRaises(ValueError):
            parse_feedback_comment("/garden feedback invalid_cat summary", default_run_id="r1")
        with self.assertRaises(ValueError):
            parse_feedback_comment("/garden feedback harness_bug missing run id")

    def test_update_intervention_and_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, rec = add_intervention(
                root,
                run_id="run-1",
                category="harness_bug",
                summary="timeout misclassified",
            )
            iid = rec["intervention_id"]
            _, updated = update_intervention(
                root,
                iid,
                status="verified",
                preventive_action="check startup code",
                preventive_check_ref="commit:deadbee",
                add_triage_steps=["verified with replay"],
            )
            self.assertEqual(updated["status"], "verified")
            self.assertEqual(updated["preventive_action"], "check startup code")
            self.assertEqual(updated["preventive_check_ref"], "commit:deadbee")
            self.assertIn("verified with replay", updated["manual_triage_steps"])
            self.assertIn("updated_at", updated)

            with self.assertRaises(ValueError):
                update_intervention(root, "../escape", status="verified")
            with self.assertRaises(ValueError):
                update_intervention(root, "does-not-exist", status="verified")

    def test_cli_feedback_from_comment_update_and_metrics_aggregation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_dir = root / ".garden" / "runs"
            sample_runs = [
                {
                    "schema_version": 1,
                    "run_id": "20261123T010000Z-aaaa1111",
                    "flow": "verify_all",
                    "target": {"commit": "1111111abcdef"},
                    "started_at": "2026-11-23T01:00:00Z",
                    "duration_sec": 10.0,
                    "retries_used": 0,
                    "usage": {"cost_usd": 0.0},
                    "verdict": "UNSATISFIED",
                    "criteria_results": {
                        "AC-001": {"verdict": "unsatisfied", "checks": ["c1"]},
                        "AC-002": {"verdict": "satisfied", "checks": ["c2"]},
                    },
                    "escalation": {"reason": "check_failed"},
                },
                {
                    "schema_version": 1,
                    "run_id": "20261123T020000Z-bbbb2222",
                    "flow": "verify_all",
                    "target": {"commit": "2222222abcdef"},
                    "started_at": "2026-11-23T02:00:00Z",
                    "duration_sec": 20.0,
                    "retries_used": 1,
                    "usage": {"cost_usd": 0.25},
                    "verdict": "INCONCLUSIVE",
                    "criteria_results": {
                        "AC-001": {"verdict": "inconclusive", "checks": ["c1"]},
                        "AC-002": {"verdict": "satisfied", "checks": ["c2"]},
                    },
                    "escalation": {"reason": "check_inconclusive"},
                },
                {
                    "schema_version": 1,
                    "run_id": "20261123T030000Z-cccc3333",
                    "flow": "verify_all",
                    "target": {"commit": "3333333abcdef"},
                    "started_at": "2026-11-23T03:00:00Z",
                    "duration_sec": 15.0,
                    "retries_used": 1,
                    "usage": {"cost_usd": 0.50},
                    "verdict": "SATISFIED",
                    "criteria_results": {
                        "AC-001": {"verdict": "satisfied", "checks": ["c1"]},
                        "AC-002": {"verdict": "satisfied", "checks": ["c2"]},
                    },
                    "escalation": None,
                },
            ]
            for run in sample_runs:
                rdir = runs_dir / run["run_id"]
                rdir.mkdir(parents=True)
                (rdir / "run.json").write_text(json.dumps(run), encoding="utf-8")

            # Record intervention from comment via CLI, then update to verified
            cmt_proc = run_cli(
                "feedback",
                "from-comment",
                "--target",
                str(root),
                "--run-id",
                "20261123T020000Z-bbbb2222",
                "--body",
                '/garden feedback flaky_env "receiver socket busy"',
            )
            self.assertEqual(cmt_proc.returncode, 0, cmt_proc.stderr)
            iid = json.loads(cmt_proc.stdout)["record"]["intervention_id"]

            upd_proc = run_cli(
                "feedback",
                "update",
                "--target",
                str(root),
                "--id",
                iid,
                "--status",
                "verified",
                "--preventive-action",
                "allocate ephemeral port",
                "--preventive-check-ref",
                "commit:3333333",
            )
            self.assertEqual(upd_proc.returncode, 0, upd_proc.stderr)

            # Full metrics via CLI
            met_proc = run_cli("metrics", "--target", str(root))
            self.assertEqual(met_proc.returncode, 0, met_proc.stderr)
            data = json.loads(met_proc.stdout)
            self.assertEqual(data["window_size"], 3)
            self.assertEqual(data["total_runs_recorded"], 3)
            self.assertEqual(data["total_retries"], 2)
            self.assertAlmostEqual(data["avg_duration_sec"], 15.0)
            self.assertAlmostEqual(data["total_cost_usd"], 0.75)
            self.assertEqual(data["recurring_failure_criteria"], ["AC-001"])
            self.assertEqual(data["interventions"]["total"], 1)
            self.assertEqual(data["interventions"]["verified_count"], 1)
            self.assertAlmostEqual(data["interventions"]["verified_rate"], 1.0)

            # Rolling window limit
            windowed = collect_garden_metrics(root, limit=1)
            self.assertEqual(windowed["window_size"], 1)
            self.assertEqual(windowed["verdict_counts"], {"SATISFIED": 1})
            self.assertEqual(windowed["rates"]["satisfied"], 1.0)
