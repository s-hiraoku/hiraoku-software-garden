"""End-to-end verification of Design §6's three completion criteria (message-relay scenario)."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tests.helpers import PSTACK_SHA, commit_all, init_target_repo, run_cli


RELAY_SPEC = """---
spec_id: message-relay
version: 1
status: approved
title: メッセージ送受信確認ツールの振る舞い仕様
owner: "@s-hiraoku"
---

## 目的
送信コマンドで送ったメッセージに対して、受信側が期待する返信を返すことを自動で確認する。

## 受け入れ条件
- **AC-001**: 受信プロセスが起動しており、疎通確認メッセージを受信できる。
- **AC-002**: 送信したメッセージに対して期待どおりの返信本文を受け取り、証跡ファイルへ記録する。
"""

RELAY_CHECK_V1 = """\
import json, os, pathlib, sys

mode = pathlib.Path("relay_mode.txt").read_text(encoding="utf-8").strip()
ev_dir = pathlib.Path(os.environ["GARDEN_EVIDENCE_DIR"])

if mode == "receiver_down":
    # Environment / startup problem -> exit 75 (INCONCLUSIVE)
    print("receiver process did not respond", file=sys.stderr)
    sys.exit(75)

if mode == "bad_reply":
    (ev_dir / "relay-transcript.json").write_text(
        json.dumps({"sent": "ping", "received": "corrupt-reply"}),
        encoding="utf-8",
    )
    print("unexpected reply payload: corrupt-reply", file=sys.stderr)
    sys.exit(1)

(ev_dir / "relay-transcript.json").write_text(
    json.dumps({"sent": "ping", "received": "pong", "receiver_pid": 4242}),
    encoding="utf-8",
)
print("relay ok: ping -> pong")
"""

RELAY_CHECK_V2 = """\
import json, os, pathlib, sys

mode = pathlib.Path("relay_mode.txt").read_text(encoding="utf-8").strip()
ev_dir = pathlib.Path(os.environ["GARDEN_EVIDENCE_DIR"])

if mode == "receiver_down":
    # Improved harness (from human intervention): captures startup exit code explicitly
    (ev_dir / "startup-diagnostic.json").write_text(
        json.dumps({"receiver_started": False, "startup_exit_code": 127, "reason": "binary missing"}),
        encoding="utf-8",
    )
    print("receiver startup failed with exit_code=127 (binary missing)", file=sys.stderr)
    sys.exit(75)

(ev_dir / "relay-transcript.json").write_text(
    json.dumps({"sent": "ping", "received": "pong", "receiver_pid": 4242}),
    encoding="utf-8",
)
print("relay ok: ping -> pong")
"""


def _garden_toml(check_script: str) -> str:
    return f"""\
schema_version = 1

[template]
name = "cli"
version = 1

[common_harness]
name = "pstack"
ref = "{PSTACK_SHA}"

[target_harness]
name = "message-relay-harness"
version = 1

[spec]
paths = ["docs/specs/message-relay.md"]
approval_branch = "main"

[delegation]
mode = "observe_only"
allowed_paths = []
forbidden_paths = [".garden/**", "docs/specs/**", ".github/workflows/**"]
max_retries = 0
max_duration_sec = 120
max_cost_usd = 0.0
require_checks = ["startup_probe", "relay_e2e"]

[checks.startup_probe]
kind = "check"
command = ["python3", "scripts/check_startup.py"]
timeout_sec = 30
covers = ["AC-001"]
inconclusive_exit_codes = [75]
status = "active"

[checks.relay_e2e]
kind = "check"
command = ["python3", "{check_script}"]
timeout_sec = 30
covers = ["AC-002"]
required_evidence = ["relay-transcript.json"]
inconclusive_exit_codes = [75]
status = "active"

[flows.verify_relay]
description = "メッセージ送受信の起動確認と往復確認を行う"
scope = "all_specs"

[[flows.verify_relay.steps]]
id = "check_startup"
type = "check"
check = "startup_probe"
on_fail = "escalate"
on_inconclusive = "escalate"

[[flows.verify_relay.steps]]
id = "check_relay"
type = "check"
check = "relay_e2e"
on_fail = "escalate"
on_inconclusive = "escalate"

[[flows.verify_relay.steps]]
id = "final_gate"
type = "gate"
require_checks = ["startup_probe", "relay_e2e"]

[[flows.verify_relay.steps]]
id = "emit_report"
type = "report"
kind = "auto"
"""


STARTUP_CHECK = """\
import pathlib, sys
mode = pathlib.Path("relay_mode.txt").read_text(encoding="utf-8").strip()
if mode == "receiver_down":
    print("startup probe passed precheck; handoff to relay_e2e")
print("startup probe ok")
"""


class DesignSection6CompletionCriteriaTests(unittest.TestCase):
    def test_all_three_design_section_6_conditions_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_target_repo(Path(tmp) / "message-relay")
            (repo / "docs" / "specs").mkdir(parents=True)
            (repo / ".garden").mkdir(parents=True)
            (repo / "scripts").mkdir(parents=True)
            (repo / ".garden" / ".gitignore").write_text("runs/\nworktrees/\n", encoding="utf-8")
            (repo / "docs" / "specs" / "message-relay.md").write_text(RELAY_SPEC, encoding="utf-8")
            (repo / "scripts" / "check_startup.py").write_text(STARTUP_CHECK, encoding="utf-8")
            (repo / "scripts" / "verify_relay.py").write_text(RELAY_CHECK_V1, encoding="utf-8")
            (repo / ".garden" / "garden.toml").write_text(
                _garden_toml("scripts/verify_relay.py"), encoding="utf-8"
            )
            (repo / "relay_mode.txt").write_text("ok\n", encoding="utf-8")
            commit_all(repo, "initial approved relay garden")

            # Condition 1: 普段の確認を任せられる (SATISFIED + harvest summary + evidence)
            ok_proc = run_cli(
                "run",
                "verify_relay",
                "--target",
                str(repo),
                "--trigger-event",
                "push",
                "--json",
            )
            self.assertEqual(ok_proc.returncode, 0, ok_proc.stderr)
            ok_payload = json.loads(ok_proc.stdout)
            self.assertEqual(ok_payload["verdict"], "SATISFIED")
            ok_dir = Path(ok_payload["run_dir"])
            ok_summary = (ok_dir / "summary.md").read_text(encoding="utf-8")
            self.assertIn("kind=harvest", ok_summary)
            transcript = json.loads(
                (ok_dir / "steps" / "02-check_relay" / "evidence" / "relay-transcript.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(transcript["received"], "pong")

            # Condition 2a: 問題時に判断を引き継げる — 挙動不一致 -> UNSATISFIED (exit 1)
            (repo / "relay_mode.txt").write_text("bad_reply\n", encoding="utf-8")
            commit_all(repo, "simulate broken reply")
            fail_proc = run_cli("run", "verify_relay", "--target", str(repo), "--json")
            self.assertEqual(fail_proc.returncode, 1)
            fail_payload = json.loads(fail_proc.stdout)
            self.assertEqual(fail_payload["verdict"], "UNSATISFIED")
            self.assertEqual(fail_payload["run"]["criteria_results"]["AC-001"]["verdict"], "satisfied")
            self.assertEqual(fail_payload["run"]["criteria_results"]["AC-002"]["verdict"], "unsatisfied")

            # Condition 2b: 環境不備 -> INCONCLUSIVE (exit 3) with confirmed vs unconfirmed split
            (repo / "relay_mode.txt").write_text("receiver_down\n", encoding="utf-8")
            commit_all(repo, "simulate receiver startup failure")
            inc_proc = run_cli("run", "verify_relay", "--target", str(repo), "--json")
            self.assertEqual(inc_proc.returncode, 3)
            inc_payload = json.loads(inc_proc.stdout)
            self.assertEqual(inc_payload["verdict"], "INCONCLUSIVE")
            self.assertEqual(inc_payload["run"]["criteria_results"]["AC-001"]["verdict"], "satisfied")
            self.assertEqual(inc_payload["run"]["criteria_results"]["AC-002"]["verdict"], "inconclusive")
            inc_run_id = inc_payload["run_id"]

            # Condition 3: 人の手直しが次に効く
            # Record human intervention via /garden feedback comment
            fb_proc = run_cli(
                "feedback",
                "from-comment",
                "--target",
                str(repo),
                "--run-id",
                inc_run_id,
                "--body",
                '/garden feedback harness_bug "受信プロセスの起動失敗を応答なしと誤認" '
                '--triage "startup exit code を確認" '
                '--preventive "verify_relay.py に起動診断 JSON 出力を追加"',
            )
            self.assertEqual(fb_proc.returncode, 0, fb_proc.stderr)
            iid = json.loads(fb_proc.stdout)["record"]["intervention_id"]

            # Apply preventive harness improvement and re-run the EXACT same failure scenario
            (repo / "scripts" / "verify_relay.py").write_text(RELAY_CHECK_V2, encoding="utf-8")
            commit_all(repo, "add startup exit-code diagnostic to verify_relay.py")
            replay_proc = run_cli("run", "verify_relay", "--target", str(repo), "--json")
            self.assertEqual(replay_proc.returncode, 3)
            replay_payload = json.loads(replay_proc.stdout)
            replay_dir = Path(replay_payload["run_dir"])
            diag = json.loads(
                (
                    replay_dir
                    / "steps"
                    / "02-check_relay"
                    / "evidence"
                    / "startup-diagnostic.json"
                ).read_text(encoding="utf-8")
            )
            self.assertFalse(diag["receiver_started"])
            self.assertEqual(diag["startup_exit_code"], 127)

            # Mark intervention verified and confirm operational metrics reflect closure
            upd_proc = run_cli(
                "feedback",
                "update",
                "--target",
                str(repo),
                "--id",
                iid,
                "--status",
                "verified",
                "--preventive-check-ref",
                f"run:{replay_payload['run_id']}",
            )
            self.assertEqual(upd_proc.returncode, 0, upd_proc.stderr)

            met_proc = run_cli("metrics", "--target", str(repo))
            self.assertEqual(met_proc.returncode, 0, met_proc.stderr)
            metrics = json.loads(met_proc.stdout)
            self.assertEqual(metrics["total_runs_recorded"], 4)
            self.assertEqual(metrics["verdict_counts"]["SATISFIED"], 1)
            self.assertEqual(metrics["verdict_counts"]["UNSATISFIED"], 1)
            self.assertEqual(metrics["verdict_counts"]["INCONCLUSIVE"], 2)
            self.assertIn("AC-002", metrics["recurring_failure_criteria"])
            self.assertEqual(metrics["interventions"]["verified_count"], 1)
            self.assertAlmostEqual(metrics["interventions"]["verified_rate"], 1.0)
