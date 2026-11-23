"""Tests for multi-Garden Nursery fleet status dashboard (garden status)."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from garden.feedback import add_intervention
from garden.fleet import collect_fleet_status
from tests.helpers import PSTACK_SHA, commit_all, init_target_repo, run_cli


def _create_configured_garden(repo: Path, *, approved: bool = True) -> None:
    init_target_repo(repo)
    (repo / ".garden").mkdir()
    (repo / ".garden" / ".gitignore").write_text("runs/\nworktrees/\n", encoding="utf-8")
    (repo / "docs" / "specs").mkdir(parents=True)
    status = "approved" if approved else "draft"
    (repo / "docs" / "specs" / "app.md").write_text(
        f"---\nspec_id: app\nversion: 1\nstatus: {status}\ntitle: App\n---\n\n- **AC-001**: ok\n",
        encoding="utf-8",
    )
    (repo / ".garden" / "garden.toml").write_text(
        f"""\
schema_version = 1

[template]
name = "cli"
version = 1

[common_harness]
name = "pstack"
ref = "{PSTACK_SHA}"

[target_harness]
name = "app-harness"
version = 1

[spec]
paths = ["docs/specs/app.md"]
approval_branch = "main"

[delegation]
mode = "observe_only"
allowed_paths = []
forbidden_paths = [".garden/**", "docs/specs/**", ".github/workflows/**"]
max_retries = 0
max_duration_sec = 60
max_cost_usd = 0.0
require_checks = ["smoke"]

[checks.smoke]
kind = "check"
command = ["python3", "-c", "print('ok')"]
timeout_sec = 30
covers = ["AC-001"]
status = "active"

[flows.verify_only]
scope = "all_specs"

[[flows.verify_only.steps]]
id = "run_smoke"
type = "check"
check = "smoke"
on_fail = "escalate"
on_inconclusive = "escalate"

[[flows.verify_only.steps]]
id = "final_gate"
type = "gate"
require_checks = ["smoke"]
""",
        encoding="utf-8",
    )
    commit_all(repo, "init garden")


class FleetStatusTests(unittest.TestCase):
    def test_fleet_status_aggregates_healthy_warn_unconfigured_and_unavailable_gardens(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            healthy = ws / "healthy-app"
            _create_configured_garden(healthy, approved=True)
            run_ok = run_cli("run", "verify_only", "--target", str(healthy), "--json")
            self.assertEqual(run_ok.returncode, 0, run_ok.stderr)

            warn_repo = ws / "warn-app"
            _create_configured_garden(warn_repo, approved=True)
            add_intervention(
                warn_repo,
                run_id="r1",
                category="harness_bug",
                summary="needs follow-up",
                status="open",
            )

            unconf = ws / "plain-repo"
            init_target_repo(unconf)
            (unconf / "README.md").write_text("# plain\n", encoding="utf-8")
            commit_all(unconf, "init plain")

            catalog = ws / "garden.toml"
            catalog.write_text(
                """\
[repositories.healthy]
path = "healthy-app"
remote = "https://example.invalid/healthy-app.git"
description = "Healthy Garden"
check = ["python3", "-c", "print('ok')"]

[repositories.warn_app]
path = "warn-app"
remote = "https://example.invalid/warn-app.git"
description = "Garden with open intervention"
check = ["python3", "-c", "print('ok')"]

[repositories.plain]
path = "plain-repo"
remote = "https://example.invalid/plain-repo.git"
description = "Not yet initialized as a Garden"
check = ["python3", "-c", "print('ok')"]

[repositories.missing]
path = "does-not-exist"
remote = "https://example.invalid/missing.git"
description = "Missing checkout"
check = ["python3", "-c", "print('ok')"]
""",
                encoding="utf-8",
            )

            fleet = collect_fleet_status(
                catalog, ws, include_self=False, skip_network=True
            )
            self.assertEqual(fleet["total_gardens"], 4)
            self.assertTrue(fleet["has_errors"])

            by_repo = {g["repo"]: g for g in fleet["gardens"]}
            # Healthy garden has skip_network branch_protection warn -> health == warn
            self.assertEqual(by_repo["healthy"]["latest_run"]["verdict"], "SATISFIED")
            self.assertEqual(by_repo["healthy"]["doctor"]["exit_code"], 0)
            self.assertEqual(by_repo["warn_app"]["metrics_summary"]["open_interventions"], 1)
            self.assertEqual(by_repo["plain"]["health"], "unconfigured")
            self.assertEqual(by_repo["missing"]["health"], "unavailable")

            # CLI exit code is 1 when an unavailable/error garden exists
            proc = run_cli(
                "--catalog",
                str(catalog),
                "--workspace",
                str(ws),
                "status",
                "--no-self",
                "--skip-network",
                "--json",
            )
            self.assertEqual(proc.returncode, 1)
            parsed = json.loads(proc.stdout)
            self.assertEqual(parsed["total_gardens"], 4)

    def test_cli_status_includes_nursery_self_and_exits_zero_when_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            nursery = ws / "nursery"
            _create_configured_garden(nursery, approved=True)
            catalog = nursery / "garden.toml"
            catalog.write_text("[repositories]\n", encoding="utf-8")

            run_ok = run_cli("run", "verify_only", "--target", str(nursery), "--json")
            self.assertEqual(run_ok.returncode, 0, run_ok.stderr)

            proc = run_cli(
                "--catalog",
                str(catalog),
                "--workspace",
                str(ws),
                "status",
                "--skip-network",
                "--json",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            self.assertEqual(data["total_gardens"], 1)
            self.assertFalse(data["has_errors"])
            self.assertEqual(data["gardens"][0]["repo"], "nursery-self")
            self.assertEqual(data["gardens"][0]["latest_run"]["verdict"], "SATISFIED")
