"""Tests for registering target repositories into the Nursery catalog (garden register)."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tests.helpers import commit_all, init_target_repo, run_cli


class CatalogRegisterTests(unittest.TestCase):
    def test_register_list_and_check_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            catalog = ws / "garden.toml"
            catalog.write_text("[repositories]\n", encoding="utf-8")

            repo = ws / "My Relay Tool"
            init_target_repo(repo)
            (repo / "README.md").write_text("# relay\n", encoding="utf-8")
            commit_all(repo, "init")

            # Without .garden/garden.toml and without --check, register rejects cleanly
            bad = run_cli(
                "--catalog", str(catalog), "--workspace", str(ws), "register", str(repo)
            )
            self.assertEqual(bad.returncode, 2)

            # With explicit --check, register succeeds and derives slug ID
            reg = run_cli(
                "--catalog",
                str(catalog),
                "--workspace",
                str(ws),
                "register",
                str(repo),
                "--description",
                "Relay CLI",
                "--check",
                "python3",
                "-c",
                "print('relay ok')",
            )
            self.assertEqual(reg.returncode, 0, reg.stderr)
            reg_data = json.loads(reg.stdout)
            self.assertEqual(reg_data["repo"], "My-Relay-Tool")
            self.assertEqual(reg_data["entry"]["path"], "My Relay Tool")

            # Duplicate registration without --force fails with exit 2
            dup = run_cli(
                "--catalog",
                str(catalog),
                "--workspace",
                str(ws),
                "register",
                str(repo),
                "--check",
                "python3",
                "-c",
                "print('x')",
            )
            self.assertEqual(dup.returncode, 2)

            # Overwrite with --force succeeds
            upd = run_cli(
                "--catalog",
                str(catalog),
                "--workspace",
                str(ws),
                "register",
                str(repo),
                "--id",
                "My-Relay-Tool",
                "--description",
                "Updated Relay CLI",
                "--force",
                "--check",
                "python3",
                "-c",
                "print('updated ok')",
            )
            self.assertEqual(upd.returncode, 0, upd.stderr)

            # Existing `garden list` and `garden check` work seamlessly with registered entry
            listed = run_cli("--catalog", str(catalog), "--workspace", str(ws), "list")
            self.assertEqual(listed.returncode, 0)
            self.assertIn("My-Relay-Tool", json.loads(listed.stdout))

            checked = run_cli(
                "--catalog",
                str(catalog),
                "--workspace",
                str(ws),
                "check",
                "My-Relay-Tool",
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(json.loads(checked.stdout)[0]["status"], "passed")

    def test_register_defaults_check_when_garden_toml_exists_and_runs_without_path_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            # Simulate a Nursery checkout next to the target repo (no `garden` binary on PATH)
            nursery_dir = ws / "nursery"
            nursery_dir.mkdir()
            repo_root = Path(__file__).resolve().parent.parent
            # Lightweight wrapper forwarding to the real repo's garden.py
            (nursery_dir / "garden.py").write_text(
                f"import runpy, sys\nsys.path.insert(0, {str(repo_root)!r})\n"
                f"runpy.run_path({str(repo_root / 'garden.py')!r}, run_name='__main__')\n",
                encoding="utf-8",
            )
            catalog = nursery_dir / "garden.toml"
            catalog.write_text("[repositories]\n", encoding="utf-8")

            repo = ws / "garden-ready"
            init_target_repo(repo)
            init_proc = run_cli("init", "--target", str(repo), "--type", "cli")
            self.assertEqual(init_proc.returncode, 0, init_proc.stderr)
            spec_file = repo / "docs" / "specs" / "garden-ready.md"
            spec_file.write_text(
                spec_file.read_text(encoding="utf-8").replace(
                    "status: draft", "status: approved"
                ),
                encoding="utf-8",
            )
            commit_all(repo, "init approved garden")

            reg = run_cli(
                "--catalog",
                str(catalog),
                "--workspace",
                str(ws),
                "register",
                str(repo),
            )
            self.assertEqual(reg.returncode, 0, reg.stderr)
            entry = json.loads(reg.stdout)["entry"]
            self.assertEqual(
                entry["check"],
                ["python3", "../nursery/garden.py", "doctor", "--target", ".", "--skip-network"],
            )

            # Executing `garden check garden-ready` MUST pass without `garden` on PATH
            chk = run_cli(
                "--catalog",
                str(catalog),
                "--workspace",
                str(ws),
                "check",
                "garden-ready",
            )
            self.assertEqual(chk.returncode, 0, chk.stdout + chk.stderr)
            self.assertEqual(json.loads(chk.stdout)[0]["status"], "passed")
