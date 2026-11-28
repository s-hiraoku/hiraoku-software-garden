import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from garden.pr_watch import (
    PrCheckItem,
    PrReviewFinding,
    PrSnapshot,
    _build_scrubbed_env,
    build_pr_snapshot,
    classify_pr_snapshot,
    parse_pr_number_from_url,
    watch_pr,
)
from garden.report import parse_garden_markers
from garden.work import work_issue
from tests.helpers import PSTACK_SHA, commit_all, init_target_repo, run_cli, sample_spec


def _setup_repo_with_bare_origin(
    base: Path,
    *,
    agent_script: Path,
    app_content: str = "BROKEN\n",
    delegation_mode: str = "propose_pr",
    check_cmd: list[str] | None = None,
) -> tuple[Path, Path]:
    bare = base / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)

    repo = init_target_repo(base / "repo", remote=str(bare))
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app.txt").write_text(app_content, encoding="utf-8")
    (repo / "docs" / "specs").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "specs" / "app.md").write_text(
        sample_spec("approved"),
        encoding="utf-8",
    )
    (repo / ".garden").mkdir(parents=True, exist_ok=True)
    (repo / ".garden" / ".gitignore").write_text("runs/\nworktrees/\n", encoding="utf-8")
    chk = check_cmd or ["python3", "-c", "assert open('src/app.txt').read().strip() == 'READY'"]
    (repo / ".garden" / "garden.toml").write_text(
        f"""schema_version = 1
[template]
name = "cli"
version = 1
[common_harness]
name = "pstack"
ref = "{PSTACK_SHA}"
[target_harness]
name = "prw-h"
version = 1
[spec]
paths = ["docs/specs/app.md"]
[delegation]
mode = "{delegation_mode}"
allowed_paths = ["src/**"]
max_retries = 2
max_duration_sec = 120
max_cost_usd = 2.0
require_checks = ["verify_ready"]
[checks.verify_ready]
command = {json.dumps(chk)}
covers = ["AC-001", "AC-002", "INV-001"]
[agents.mock_fixer]
adapter = "mock"
command = [{json.dumps(sys.executable)}, {json.dumps(str(agent_script))}]
timeout_sec = 30
[flows.implement_issue]
scope = "all_specs"
[[flows.implement_issue.steps]]
id = "impl"
type = "agent"
agent = "mock_fixer"
purpose = "implement"
[[flows.implement_issue.steps]]
id = "verify"
type = "check"
check = "verify_ready"
""",
        encoding="utf-8",
    )
    commit_all(repo, "init repo")
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "main"], check=True, capture_output=True)
    return repo, bare


class PrWatchClassificationTests(unittest.TestCase):
    def test_snapshot_filters_untrusted_read_collaborators_and_checks_bot_author_and_head(self):
        head_oid = "a" * 40
        pr_view = {
            "number": 55,
            "url": "https://github.com/s-hiraoku/sample/pull/55",
            "state": "OPEN",
            "isDraft": False,
            "isCrossRepository": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "APPROVED",
            "headRefName": "feat/demo",
            "baseRefName": "main",
            "headRefOid": head_oid,
            "statusCheckRollup": [
                {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"__typename": "StatusContext", "context": "CodeRabbit", "state": "SUCCESS"},
            ],
            "reviews": [],
            "comments": [
                # Spoofed CodeRabbit text from a random human MUST NOT set review_pending
                {
                    "author": {"login": "random-human"},
                    "body": "<!-- This is an auto-generated comment: review in progress by coderabbit.ai -->",
                },
                # Old HEAD Codex running marker MUST NOT block current HEAD
                {
                    "author": {"login": "chatgpt-codex-connector[bot]"},
                    "body": '<!-- codex-review-state:v1 {"security_review":{"commit_sha":"bbbb","status":"running"}} -->',
                },
            ],
        }
        threads = [
            {
                "id": "t_resolved",
                "isResolved": True,
                "isOutdated": False,
                "path": "src/a.py",
                "line": 10,
                "comments": {
                    "nodes": [
                        {
                            "id": "c1",
                            "author": {"login": "s-hiraoku", "__typename": "User"},
                            "authorAssociation": "OWNER",
                            "body": "Already resolved",
                        }
                    ]
                },
            },
            {
                # COLLABORATOR without verified write permission MUST be excluded!
                "id": "t_readonly_collab",
                "isResolved": False,
                "isOutdated": False,
                "path": "src/a.py",
                "line": 12,
                "comments": {
                    "nodes": [
                        {
                            "id": "c2",
                            "author": {"login": "triage-only", "__typename": "User"},
                            "authorAssociation": "COLLABORATOR",
                            "body": "Read-only collaborator comment",
                        }
                    ]
                },
            },
            {
                "id": "t_coderabbit",
                "isResolved": False,
                "isOutdated": False,
                "path": "src/a.py",
                "line": 20,
                "comments": {
                    "nodes": [
                        {
                            "id": "c3",
                            "author": {"login": "coderabbitai", "__typename": "Bot"},
                            "authorAssociation": "NONE",
                            "body": "Handle TimeoutExpired cleanly.",
                        }
                    ]
                },
            },
            {
                "id": "t_garden_marker",
                "isResolved": False,
                "isOutdated": False,
                "path": "src/a.py",
                "line": 21,
                "comments": {
                    "nodes": [
                        {
                            "id": "c4",
                            "author": {"login": "s-hiraoku", "__typename": "User"},
                            "authorAssociation": "OWNER",
                            "body": "<!-- garden:v1 kind=harvest run_id=r1 idempotency_key=k1 verdict=SATISFIED commit=abc -->\nok",
                        }
                    ]
                },
            },
        ]
        snap = build_pr_snapshot(pr_view, review_threads=threads, trusted_humans=frozenset())
        self.assertFalse(snap.review_pending)
        self.assertEqual([f.finding_id for f in snap.findings], ["t_coderabbit"])
        self.assertEqual(classify_pr_snapshot(snap)["status"], "needs_fix")

        # When triage-only IS in verified write-access trusted_humans, it IS included
        snap_write = build_pr_snapshot(
            pr_view, review_threads=threads, trusted_humans=frozenset({"triage-only"})
        )
        self.assertEqual(
            {f.finding_id for f in snap_write.findings}, {"t_readonly_collab", "t_coderabbit"}
        )

        # Codex running on CURRENT HEAD from chatgpt-codex-connector -> waiting!
        pr_codex_running = dict(pr_view)
        pr_codex_running["comments"] = [
            {
                "author": {"login": "chatgpt-codex-connector"},
                "body": f'<!-- codex-review-state:v1 {{"security_review":{{"commit_sha":"{head_oid}","status":"running"}}}} -->',
            }
        ]
        snap_running = build_pr_snapshot(pr_codex_running, review_threads=[])
        self.assertTrue(snap_running.review_pending)
        self.assertEqual(classify_pr_snapshot(snap_running)["status"], "waiting")

    def test_classify_enforces_merge_state_review_decision_and_complete_threads(self):
        base = dict(
            number=10,
            url="https://github.com/o/r/pull/10",
            state="OPEN",
            is_draft=False,
            is_cross_repository=False,
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
            review_decision="",
            head_ref_name="feat/x",
            base_ref_name="main",
            head_ref_oid="b" * 40,
            checks=(PrCheckItem("ci", "passed", "SUCCESS"),),
            findings=(),
            review_pending=False,
            review_threads_complete=True,
        )
        self.assertEqual(classify_pr_snapshot(PrSnapshot(**base))["status"], "merge_ready")
        # BLOCKED or REVIEW_REQUIRED must NOT be merge_ready
        self.assertEqual(
            classify_pr_snapshot(PrSnapshot(**(base | {"merge_state_status": "BLOCKED"})))["status"],
            "needs_human",
        )
        self.assertEqual(
            classify_pr_snapshot(PrSnapshot(**(base | {"review_decision": "REVIEW_REQUIRED"})))["status"],
            "needs_human",
        )
        # BEHIND base branch triggers needs_fix (update with base)
        self.assertEqual(
            classify_pr_snapshot(PrSnapshot(**(base | {"merge_state_status": "BEHIND"})))["status"],
            "needs_fix",
        )
        # Incomplete review threads MUST NEVER return merge_ready -> inconclusive!
        self.assertEqual(
            classify_pr_snapshot(PrSnapshot(**(base | {"review_threads_complete": False})))["status"],
            "inconclusive",
        )
        self.assertEqual(
            classify_pr_snapshot(PrSnapshot(**(base | {"state": "MERGED"})))["status"],
            "merged",
        )
        self.assertEqual(
            classify_pr_snapshot(PrSnapshot(**(base | {"is_cross_repository": True})))["status"],
            "needs_human",
        )
        self.assertEqual(parse_pr_number_from_url("https://github.com/o/r/pull/98"), 98)


class PrWatchAndWorkIntegrationTests(unittest.TestCase):
    def test_pr_watch_multi_round_fix_commit_first_env_scrub_and_pr_ready_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Round 1 sets PARTIAL (passes check), GitHub reports new CI failure on new HEAD -> Round 2 sets READY!
            # Also assert host secrets are scrubbed from agent env!
            fixer = base / "fixer.py"
            fixer.write_text(
                "import json, os, pathlib\n"
                "assert 'GH_TOKEN' not in os.environ\n"
                "assert 'AWS_SECRET_ACCESS_KEY' not in os.environ\n"
                "prompt = pathlib.Path(os.environ['GARDEN_AGENT_PROMPT']).read_text(encoding='utf-8')\n"
                "wt = pathlib.Path(os.environ['GARDEN_WORKTREE'])\n"
                "if 'ROUND2_REVIEW' in prompt:\n"
                "    (wt / 'src' / 'app.txt').write_text('READY\\n# round2\\n', encoding='utf-8')\n"
                "else:\n"
                "    (wt / 'src' / 'app.txt').write_text('READY\\n', encoding='utf-8')\n"
                "pathlib.Path(os.environ['GARDEN_AGENT_RESPONSE']).write_text(json.dumps({\n"
                "  'schema_version': 1,\n"
                "  'outcome': 'completed',\n"
                "  'summary': 'applied fix',\n"
                "  'changed_files': ['src/app.txt'],\n"
                "  'usage': {'input_tokens': 100, 'output_tokens': 40, 'cost_usd': 0.02},\n"
                "}))\n",
                encoding="utf-8",
            )
            # Check mutates src/app.txt AFTER reading it and also verifies GH_TOKEN is scrubbed!
            mutating_check = [
                sys.executable,
                "-c",
                "import os, pathlib; "
                "assert 'GH_TOKEN' not in os.environ; "
                "p = pathlib.Path('src/app.txt'); "
                "assert p.read_text().splitlines()[0].strip() == 'READY'; "
                "p.write_text('MUTATED_BY_CHECK\\n')",
            ]
            repo, bare = _setup_repo_with_bare_origin(
                base, agent_script=fixer, check_cmd=mutating_check
            )

            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feat/pr-42"], check=True, capture_output=True)
            (repo / "src" / "app.txt").write_text("STILL_BROKEN\n", encoding="utf-8")
            commit_all(repo, "pr initial commit")
            pr_head_1 = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()
            subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "feat/pr-42"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True)

            poll_count = {"n": 0}
            label_calls: list[str | None] = []

            def _fake_fetch(pr_num, *, cwd, gh_repo, trusted_bots, trusted_reviewers, permission_cache, fetch_failed_logs=True):
                poll_count["n"] += 1
                cur_remote = subprocess.run(
                    ["git", "--git-dir", str(bare), "rev-parse", "refs/heads/feat/pr-42"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if poll_count["n"] == 1:
                    return PrSnapshot(
                        number=42,
                        url="https://github.com/example/app/pull/42",
                        state="OPEN",
                        is_draft=False,
                        is_cross_repository=False,
                        mergeable="MERGEABLE",
                        merge_state_status="UNSTABLE",
                        head_ref_name="feat/pr-42",
                        base_ref_name="main",
                        head_ref_oid=pr_head_1,
                        checks=(PrCheckItem("verify_ready", "failed", "FAILURE", "", "AssertionError"),),
                        findings=(
                            PrReviewFinding(
                                finding_id="t1",
                                author="coderabbitai",
                                author_association="NONE",
                                path="src/app.txt",
                                line=1,
                                body="Set app.txt to READY.",
                            ),
                        ),
                    )
                if poll_count["n"] == 2:
                    # New HEAD has a follow-up reply on t1 -> MUST trigger Round 2 (NOT stuck waiting!)
                    return PrSnapshot(
                        number=42,
                        url="https://github.com/example/app/pull/42",
                        state="OPEN",
                        is_draft=False,
                        is_cross_repository=False,
                        mergeable="MERGEABLE",
                        merge_state_status="UNSTABLE",
                        head_ref_name="feat/pr-42",
                        base_ref_name="main",
                        head_ref_oid=cur_remote,
                        checks=(PrCheckItem("verify_ready", "failed", "FAILURE"),),
                        findings=(
                            PrReviewFinding(
                                finding_id="t1",
                                author="coderabbitai",
                                author_association="NONE",
                                path="src/app.txt",
                                line=1,
                                body="Set app.txt to READY.\n\n[Follow-up]: ROUND2_REVIEW add comment",
                            ),
                        ),
                    )
                return PrSnapshot(
                    number=42,
                    url="https://github.com/example/app/pull/42",
                    state="OPEN",
                    is_draft=False,
                    is_cross_repository=False,
                    mergeable="MERGEABLE",
                    merge_state_status="CLEAN",
                    head_ref_name="feat/pr-42",
                    base_ref_name="main",
                    head_ref_oid=cur_remote,
                    checks=(PrCheckItem("verify_ready", "passed", "SUCCESS"),),
                    findings=(),
                )

            def _fake_label(pr_num, target_label, *, cwd, gh_repo=None):
                label_calls.append(target_label)
                return {"added_labels": [target_label] if target_label else [], "removed_labels": [], "label_warning": None}

            with (
                patch.dict(os.environ, {"GH_TOKEN": "secret-gh", "AWS_SECRET_ACCESS_KEY": "secret-aws"}),
                patch("garden.pr_watch.fetch_pr_snapshot", side_effect=_fake_fetch),
                patch("garden.pr_watch._set_pr_managed_label", side_effect=_fake_label),
                patch("garden.pr_watch.time.sleep"),
            ):
                res = watch_pr(repo, 42, poll_interval_sec=1, max_rounds=3, trust_pr_code=True)

            self.assertEqual(res["exit_code"], 0)
            self.assertEqual(res["status"], "merge_ready")
            self.assertEqual(res["rounds_used"], 2)
            # Dedicated PR-ready label (NOT garden:harvest-ready!)
            self.assertEqual(label_calls, ["garden:pr-ready"])
            # Pushed commit MUST contain exact verified content (NOT MUTATED_BY_CHECK!)
            pushed_val = subprocess.run(
                ["git", "--git-dir", str(bare), "show", "refs/heads/feat/pr-42:src/app.txt"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(pushed_val, "READY\n# round2\n")

    def test_pr_watch_fails_closed_when_sandbox_unavailable_and_detects_remote_approval_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixer = base / "fixer.py"
            fixer.write_text("raise SystemExit('should never execute')\n", encoding="utf-8")
            repo, bare = _setup_repo_with_bare_origin(base, agent_script=fixer)

            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feat/pr-sbx"], check=True, capture_output=True)
            (repo / "src" / "app.txt").write_text("BAD\n", encoding="utf-8")
            commit_all(repo, "pr commit")
            pr_head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()
            subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "feat/pr-sbx"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True)

            snap = PrSnapshot(
                number=47,
                url="https://github.com/example/app/pull/47",
                state="OPEN",
                is_draft=False,
                is_cross_repository=False,
                mergeable="MERGEABLE",
                merge_state_status="UNSTABLE",
                head_ref_name="feat/pr-sbx",
                base_ref_name="main",
                head_ref_oid=pr_head,
                checks=(PrCheckItem("verify_ready", "failed", "FAILURE"),),
                findings=(),
            )
            # 1) No sandbox available & trust_pr_code=False -> fail-closed pr_sandbox_unavailable!
            with (
                patch("garden.pr_watch.fetch_pr_snapshot", return_value=snap),
                patch("garden.pr_watch._resolve_pr_sandbox_prefix", return_value=None),
                patch("garden.pr_watch._set_pr_managed_label", return_value={}),
                patch("garden.pr_watch._post_pr_comment", return_value=None),
            ):
                res_sbx = watch_pr(repo, 47, poll_interval_sec=1, max_rounds=1, trust_pr_code=False)
            self.assertEqual(res_sbx["exit_code"], 4)
            self.assertIn("pr_sandbox_unavailable", res_sbx["reasons"])

            # 2) Remote origin/main tightens mode to observe_only while local main stays stale propose_pr
            other = base / "other-clone"
            subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(other), "config", "user.name", "T"], check=True)
            subprocess.run(["git", "-C", str(other), "config", "user.email", "t@e.invalid"], check=True)
            gpath = other / ".garden" / "garden.toml"
            gpath.write_text(
                gpath.read_text(encoding="utf-8")
                .replace('mode = "propose_pr"', 'mode = "observe_only"')
                .replace('allowed_paths = ["src/**"]', "allowed_paths = []"),
                encoding="utf-8",
            )
            commit_all(other, "tighten remote main to observe_only")
            subprocess.run(["git", "-C", str(other), "push", "origin", "main"], check=True, capture_output=True)

            # Local repo/main is STILL propose_pr, but pr-watch MUST fetch & enforce origin/main!
            with (
                patch("garden.pr_watch.fetch_pr_snapshot", return_value=snap),
                patch("garden.pr_watch._set_pr_managed_label", return_value={}),
                patch("garden.pr_watch._post_pr_comment", return_value=None),
            ):
                res_remote = watch_pr(repo, 47, poll_interval_sec=1, max_rounds=1, trust_pr_code=True)
            self.assertEqual(res_remote["exit_code"], 4)
            self.assertIn("evaluation_criteria_changed_in_pr", res_remote["reasons"])

    def test_pr_watch_merge_conflict_ignores_clean_base_files_and_handles_japanese_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            resolver = base / "resolver.py"
            resolver.write_text(
                "import json, os, pathlib\n"
                "req = json.loads(pathlib.Path(os.environ['GARDEN_AGENT_REQUEST']).read_text(encoding='utf-8'))\n"
                "assert 'src/仕様メモ.txt' in req['context']['pr_fix']['conflict_files']\n"
                "wt = pathlib.Path(os.environ['GARDEN_WORKTREE'])\n"
                "(wt / 'src' / '仕様メモ.txt').write_text('RESOLVED\\n', encoding='utf-8')\n"
                "(wt / 'src' / 'app.txt').write_text('READY\\n', encoding='utf-8')\n"
                "pathlib.Path(os.environ['GARDEN_AGENT_RESPONSE']).write_text(json.dumps({\n"
                "  'schema_version': 1,\n"
                "  'outcome': 'completed',\n"
                "  'summary': 'resolved Japanese filename conflict',\n"
                "  'changed_files': ['src/仕様メモ.txt', 'src/app.txt'],\n"
                "  'usage': {'cost_usd': 0.01},\n"
                "}))\n",
                encoding="utf-8",
            )
            repo, bare = _setup_repo_with_bare_origin(base, agent_script=resolver)
            (repo / "src" / "仕様メモ.txt").write_text("BASE\n", encoding="utf-8")
            commit_all(repo, "add japanese file")
            subprocess.run(["git", "-C", str(repo), "push", "origin", "main"], check=True, capture_output=True)

            # Feature branch modifies Japanese file
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feat/jp-conflict"], check=True, capture_output=True)
            (repo / "src" / "仕様メモ.txt").write_text("FEATURE\n", encoding="utf-8")
            commit_all(repo, "feature edit")
            conflict_head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()
            subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "feat/jp-conflict"], check=True, capture_output=True)

            # Main adds README.md (OUTSIDE allowed_paths=['src/**']!) AND conflicts on src/仕様メモ.txt
            subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True)
            (repo / "README.md").write_text("# Main Readme\n", encoding="utf-8")
            (repo / "src" / "仕様メモ.txt").write_text("MAIN_CONFLICT\n", encoding="utf-8")
            commit_all(repo, "main adds README and conflicts japanese file")
            subprocess.run(["git", "-C", str(repo), "push", "origin", "main"], check=True, capture_output=True)

            conflict_snap = PrSnapshot(
                number=45,
                url="https://github.com/example/app/pull/45",
                state="OPEN",
                is_draft=False,
                is_cross_repository=False,
                mergeable="CONFLICTING",
                merge_state_status="DIRTY",
                head_ref_name="feat/jp-conflict",
                base_ref_name="main",
                head_ref_oid=conflict_head,
                checks=(PrCheckItem("verify_ready", "passed", "SUCCESS"),),
                findings=(),
            )
            with (
                patch("garden.pr_watch.fetch_pr_snapshot", return_value=conflict_snap),
                patch("garden.pr_watch._set_pr_managed_label", return_value={}),
            ):
                res_conf = watch_pr(
                    repo, 45, poll_interval_sec=1, max_rounds=2, once=True, trust_pr_code=True
                )
            self.assertEqual(res_conf["exit_code"], 0, res_conf)
            self.assertEqual(res_conf["status"], "fix_pushed")
            merged_jp = subprocess.run(
                ["git", "--git-dir", str(bare), "show", "refs/heads/feat/jp-conflict:src/仕様メモ.txt"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(merged_jp, "RESOLVED")
            merged_readme = subprocess.run(
                ["git", "--git-dir", str(bare), "show", "refs/heads/feat/jp-conflict:README.md"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(merged_readme, "# Main Readme")

    def test_pr_watch_blocks_origin_repo_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dummy = base / "dummy.py"
            dummy.write_text("pass\n", encoding="utf-8")
            repo, _ = _setup_repo_with_bare_origin(base, agent_script=dummy)
            subprocess.run(
                ["git", "-C", str(repo), "remote", "set-url", "origin", "https://github.com/fork-user/app.git"],
                check=True,
                capture_output=True,
            )
            upstream_snap = PrSnapshot(
                number=99,
                url="https://github.com/upstream-org/app/pull/99",
                state="OPEN",
                is_draft=False,
                is_cross_repository=False,
                mergeable="MERGEABLE",
                merge_state_status="CLEAN",
                head_ref_name="feat/x",
                base_ref_name="main",
                head_ref_oid="c" * 40,
                checks=(),
                findings=(),
            )
            with patch("garden.pr_watch.fetch_pr_snapshot", return_value=upstream_snap):
                res = watch_pr(repo, 99, poll_interval_sec=1, max_rounds=1)
            self.assertEqual(res["exit_code"], 4)
            self.assertIn("pr_origin_repo_mismatch", res["reasons"])

    def test_work_issue_caps_auto_merge_validates_upfront_and_resumes_existing_pr(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            coder = base / "coder.py"
            coder.write_text(
                "import json, os, pathlib\n"
                "wt = pathlib.Path(os.environ['GARDEN_WORKTREE'])\n"
                "(wt / 'src' / 'app.txt').write_text('READY\\n', encoding='utf-8')\n"
                "pathlib.Path(os.environ['GARDEN_AGENT_RESPONSE']).write_text(json.dumps({\n"
                "  'schema_version': 1,\n"
                "  'outcome': 'completed',\n"
                "  'summary': 'implemented issue',\n"
                "  'changed_files': ['src/app.txt'],\n"
                "  'usage': {'cost_usd': 0.03},\n"
                "}))\n",
                encoding="utf-8",
            )
            # Configure flow with delegation.mode = "auto_merge" to prove `work` caps it to propose_pr!
            repo, _ = _setup_repo_with_bare_origin(
                base, agent_script=coder, delegation_mode="auto_merge"
            )
            issue_file = base / "issue91.json"
            issue_file.write_text(
                json.dumps(
                    {
                        "number": 91,
                        "title": "Make app READY",
                        "body": "Update src/app.txt to READY.",
                        "url": "https://github.com/example/app/issues/91",
                    }
                ),
                encoding="utf-8",
            )

            # 1) Invalid --watch-max-rounds MUST fail BEFORE running agent or creating PR!
            with patch("garden.work.run_flow") as mock_run_flow:
                with self.assertRaises(ValueError):
                    work_issue(
                        repo,
                        issue_file=issue_file,
                        watch=True,
                        watch_max_rounds=-1,
                        post_issue_updates=False,
                    )
                mock_run_flow.assert_not_called()

            # 2) Even when delegation.mode == "auto_merge", `work_issue` MUST NEVER call `gh pr merge`!
            real_run = subprocess.run
            gh_calls: list[list[str]] = []

            def _fake_publish_run(argv, *args, **kwargs):
                if argv and argv[0] == "gh":
                    gh_calls.append(list(argv))
                if argv[:2] == ["git", "push"]:
                    return subprocess.CompletedProcess(argv, 0, "", "")
                if argv[:3] == ["gh", "pr", "create"]:
                    return subprocess.CompletedProcess(
                        argv, 0, "https://github.com/example/app/pull/105\n", ""
                    )
                return real_run(argv, *args, **kwargs)

            posted_issue_notes: list[str] = []

            def _fake_note(num, body, *, cwd, gh_repo=None):
                posted_issue_notes.append(body)

            watch_calls: list[int] = []

            def _fake_watch(root_arg, pr_num, **kwargs):
                watch_calls.append(pr_num)
                return {"pr_number": pr_num, "status": "merge_ready", "exit_code": 0}

            with (
                patch("garden.work._find_existing_open_pr", return_value=None),
                patch("garden.work.mark_github_running", return_value={}),
                patch("garden.work.clear_github_running", return_value={}),
                patch("garden.work._post_issue_note", side_effect=_fake_note),
                patch("garden.publish.subprocess.run", side_effect=_fake_publish_run),
                patch("garden.work.watch_pr", side_effect=_fake_watch),
            ):
                full_res = work_issue(
                    repo,
                    issue_file=issue_file,
                    post_issue_updates=True,
                )

            self.assertEqual(full_res["exit_code"], 0)
            self.assertEqual(full_res["pr_number"], 105)
            self.assertEqual(full_res["reflect"]["allowed_action"], "propose_pr")
            self.assertFalse(full_res["reflect"]["auto_merge_enabled"])
            self.assertFalse(any(cmd[:3] == ["gh", "pr", "merge"] for cmd in gh_calls))
            # Issue PR creation note carries Garden marker so future fetches exclude it!
            self.assertTrue(posted_issue_notes and parse_garden_markers(posted_issue_notes[0]))

            # 3) Retrying `work_issue` after PR creation resumes existing PR without re-running reflect_run!
            watch_calls.clear()
            with (
                patch("garden.work._find_existing_open_pr", return_value=(105, "https://github.com/example/app/pull/105")),
                patch("garden.work.reflect_run") as mock_reflect,
                patch("garden.work.watch_pr", side_effect=_fake_watch),
            ):
                retry_res = work_issue(
                    repo,
                    issue_file=issue_file,
                    post_issue_updates=False,
                )
            self.assertEqual(retry_res["exit_code"], 0)
            self.assertTrue(retry_res["resumed_existing_pr"])
            self.assertEqual(retry_res["pr_number"], 105)
            self.assertEqual(watch_calls, [105])
            mock_reflect.assert_not_called()

            # 4) Scrubbed env strips host tokens/secrets
            with patch.dict(os.environ, {"GH_TOKEN": "t", "CUSTOM_API_KEY": "k", "SAFE_VAR": "ok"}):
                chk_env = _build_scrubbed_env(kind="check")
                self.assertNotIn("GH_TOKEN", chk_env)
                self.assertNotIn("CUSTOM_API_KEY", chk_env)
                self.assertEqual(chk_env.get("SAFE_VAR"), "ok")

            # 5) CLI argument validation
            bad_pw = run_cli("pr-watch", "0", "--target", str(repo))
            self.assertEqual(bad_pw.returncode, 2)
            bad_work = run_cli("work", "--target", str(repo))
            self.assertEqual(bad_work.returncode, 2)
