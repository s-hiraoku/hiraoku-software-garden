from pathlib import Path
import tempfile
import unittest

from garden.definition import DelegationConfig
from garden.guard import match_glob, snapshot_untracked_files, verify_post_agent_guard
from tests.helpers import commit_all, git, init_target_repo


class PostAgentGuardTests(unittest.TestCase):
    def _make_repo(self, tmp: str) -> tuple[Path, str]:
        repo = init_target_repo(Path(tmp) / "repo")
        (repo / "src").mkdir()
        (repo / "docs" / "specs").mkdir(parents=True)
        (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "docs" / "specs" / "app.md").write_text("spec\n", encoding="utf-8")
        base_commit = commit_all(repo, "base")
        return repo, base_commit

    def _delegation(self, mode: str = "propose_pr") -> DelegationConfig:
        return DelegationConfig(
            mode=mode,
            allowed_paths=("src/**",) if mode != "observe_only" else (),
            forbidden_paths=(".garden/**", "docs/specs/**"),
            max_retries=2,
            max_duration_sec=300,
            max_cost_usd=3.0,
        )

    def test_catches_committed_forbidden_edit_against_target_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, base_commit = self._make_repo(tmp)
            # Agent edits forbidden spec AND commits it (HEAD diff would be empty!)
            (repo / "docs" / "specs" / "app.md").write_text("tampered\n", encoding="utf-8")
            commit_all(repo, "sneaky agent commit")

            guard = verify_post_agent_guard(repo, base_commit, self._delegation())
            self.assertFalse(guard.ok)
            self.assertTrue(
                any("forbidden_path_violation" in v for v in guard.violations)
            )

    def test_catches_rename_of_forbidden_file_into_allowed_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, base_commit = self._make_repo(tmp)
            git(repo, "mv", "docs/specs/app.md", "src/moved.md")
            commit_all(repo, "sneaky rename")

            guard = verify_post_agent_guard(repo, base_commit, self._delegation())
            self.assertFalse(guard.ok)
            self.assertIn("docs/specs/app.md", guard.changed_files)
            self.assertTrue(
                any("forbidden_path_violation" in v for v in guard.violations)
            )

    def test_catches_history_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self._make_repo(tmp)
            (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
            target_commit = commit_all(repo, "second")
            git(repo, "reset", "--hard", "HEAD~1")

            guard = verify_post_agent_guard(repo, target_commit, self._delegation())
            self.assertFalse(guard.ok)
            self.assertTrue(any("history_rewritten" in v for v in guard.violations))

    def test_catches_cost_and_duration_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, base_commit = self._make_repo(tmp)
            guard = verify_post_agent_guard(
                repo,
                base_commit,
                self._delegation(),
                step_cost_usd=1.5,
                agent_max_cost_usd=1.0,
                cumulative_cost_usd=3.5,
                elapsed_sec=400.0,
            )
            self.assertFalse(guard.ok)
            self.assertTrue(any("agent_cost_limit_exceeded" in v for v in guard.violations))
            self.assertTrue(any("flow_cost_limit_exceeded" in v for v in guard.violations))
            self.assertTrue(any("duration_limit_exceeded" in v for v in guard.violations))

    def test_match_glob_preserves_leading_dot_in_hidden_directories(self):
        self.assertTrue(match_glob(".garden/garden.toml", ".garden/**"))
        self.assertTrue(match_glob("./.garden/garden.toml", ".garden/**"))
        self.assertFalse(match_glob("garden/x.py", ".garden/**"))
        self.assertFalse(match_glob("github/workflows/a.yml", ".github/workflows/**"))
        self.assertTrue(match_glob(".github/workflows/a.yml", ".github/workflows/**"))

    def test_baseline_untracked_and_ignore_prefixes_do_not_trigger_false_positives(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, base_commit = self._make_repo(tmp)
            # Pre-existing untracked scratch file (e.g., CI workspace tooling)
            (repo / ".garden-nursery").mkdir()
            nursery_file = repo / ".garden-nursery" / "garden.py"
            nursery_file.write_text("# cli\n", encoding="utf-8")
            baseline = snapshot_untracked_files(repo)

            # Run artifacts written during this run under ignore_prefixes
            (repo / ".garden" / "runs" / "r1").mkdir(parents=True)
            (repo / ".garden" / "runs" / "r1" / "run.json").write_text("{}", encoding="utf-8")

            guard = verify_post_agent_guard(
                repo,
                base_commit,
                self._delegation(),
                baseline_untracked=baseline,
                ignore_prefixes=(".garden/runs/r1/",),
            )
            self.assertTrue(guard.ok, guard.violations)
            self.assertEqual(guard.changed_files, ())

            # Modifying content of a pre-existing untracked file MUST be caught!
            nursery_file.write_text("# tampered by agent\n", encoding="utf-8")
            guard2 = verify_post_agent_guard(
                repo,
                base_commit,
                self._delegation(),
                baseline_untracked=baseline,
                ignore_prefixes=(".garden/runs/r1/",),
            )
            self.assertFalse(guard2.ok)
            self.assertIn(".garden-nursery/garden.py", guard2.changed_files)
