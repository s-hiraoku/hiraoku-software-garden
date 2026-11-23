import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


GARDEN = Path(__file__).with_name("garden.py")
FETCH_URL = "https://example.invalid/project.git"


class GardenCheckTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.checkout = self.workspace / "project"
        self.checkout.mkdir()
        self.git("-c", "init.templateDir=", "init", "--quiet")
        self.git(
            "-c", "user.name=Garden Test",
            "-c", "user.email=tests@example.invalid",
            "-c", "commit.gpgsign=false",
            "commit", "--quiet", "--allow-empty", "-m", "Initial commit",
        )
        self.git("remote", "add", "upstream", FETCH_URL)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.checkout), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def run_check(self, remote=FETCH_URL):
        command = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('check-ran').touch()",
        ]
        catalog = self.workspace / "garden.toml"
        catalog.write_text(
            "[repositories.project]\n"
            'path = "project"\n'
            f"remote = {json.dumps(remote)}\n"
            'description = "Test project"\n'
            f"check = {json.dumps(command)}\n",
            encoding="utf-8",
        )
        run = subprocess.run(
            [
                sys.executable, str(GARDEN),
                "--catalog", str(catalog),
                "--workspace", str(self.workspace),
                "check", "project",
            ],
            capture_output=True,
            text=True,
        )
        return run, json.loads(run.stdout)[0]

    def test_rejects_mismatched_fetch_remote_before_running_check(self):
        expected = "https://example.invalid/another-project.git"
        self.git("remote", "set-url", "--push", "upstream", expected)

        run, result = self.run_check(remote=expected)

        self.assertEqual(run.returncode, 1)
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse((self.checkout / "check-ran").exists())

    def test_marks_untracked_files_dirty_even_when_git_hides_them(self):
        self.git("config", "status.showUntrackedFiles", "no")
        (self.checkout / "untracked.txt").touch()

        run, result = self.run_check()

        self.assertEqual(run.returncode, 0)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["dirty"])
