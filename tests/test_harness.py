"""Tests for MVP-6 harness standardization (required_skills, required_mcp, and pstack core)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from garden.definition import DefinitionError, load_definition_bytes
from garden.doctor import run_doctor
from garden.engine import run_flow
from garden.harness import discover_configured_mcp_servers, discover_installed_skills
from tests.helpers import PSTACK_SHA, commit_all, init_target_repo, sample_spec


def _write_skill(root: Path, name: str, *, nested: bool = False) -> Path:
    skill_dir = (root / "skills" / name) if nested else (root / name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(f"---\nname: {name}\n---\n# {name}\n", encoding="utf-8")
    return skill_md


def _make_garden_repo(
    base: Path,
    *,
    spec_status: str = "approved",
    common_required_skills: list[str] | None = None,
    target_required_skills: list[str] | None = None,
    target_required_mcp: list[str] | None = None,
    with_real_agent: bool = False,
) -> Path:
    repo = init_target_repo(base / "repo")
    (repo / "docs" / "specs").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "specs" / "app.md").write_text(
        sample_spec(spec_status), encoding="utf-8"
    )
    (repo / ".garden").mkdir(exist_ok=True)
    (repo / ".garden" / ".gitignore").write_text("runs/\nworktrees/\n", encoding="utf-8")

    ch_req = json.dumps(common_required_skills or [])
    th_req_skills = json.dumps(target_required_skills or [])
    th_req_mcp = json.dumps(target_required_mcp or [])

    if with_real_agent:
        delegation = """
[delegation]
mode = "propose_pr"
allowed_paths = ["src/**"]
max_retries = 1
max_duration_sec = 120
max_cost_usd = 1.0
"""
        agents_block = """
[agents.coder]
adapter = "codex"
command = ["python3", "-c", "pass"]
timeout_sec = 30
"""
        flow_block = """
[flows.verify]
scope = "all_specs"

[[flows.verify.steps]]
id = "s1"
type = "check"
check = "c1"
on_fail = "fix_and_retry"
fix_step = "fix1"

[[flows.verify.steps]]
id = "fix1"
type = "agent"
agent = "coder"
purpose = "fix"
run_on_demand = true
"""
    else:
        delegation = """
[delegation]
mode = "observe_only"
max_retries = 0
max_duration_sec = 60
max_cost_usd = 0.0
"""
        agents_block = ""
        flow_block = """
[flows.verify]
scope = "all_specs"

[[flows.verify.steps]]
id = "s1"
type = "check"
check = "c1"
"""

    (repo / ".garden" / "garden.toml").write_text(
        f"""
schema_version = 1

[template]
name = "cli"
version = 1

[common_harness]
name = "pstack"
ref = "{PSTACK_SHA}"
required_skills = {ch_req}

[target_harness]
name = "app-harness"
version = 1
required_skills = {th_req_skills}
required_mcp = {th_req_mcp}

[spec]
paths = ["docs/specs/app.md"]
{delegation}
[checks.c1]
command = ["python3", "-c", "pass"]
covers = ["AC-001", "AC-002", "INV-001"]
{agents_block}
{flow_block}
""",
        encoding="utf-8",
    )
    commit_all(repo, "init")
    return repo


class HarnessStandardizationTests(unittest.TestCase):
    def test_definition_parses_and_validates_required_skills_and_mcp(self):
        valid = f"""
schema_version = 1
[template]
name = "web-frontend"
version = 1
[common_harness]
name = "pstack"
ref = "{PSTACK_SHA}"
required_skills = ["poteto-mode", "poteto-mode", "create-verification-skill"]
[target_harness]
name = "web-h"
version = 1
required_skills = ["verify-ui"]
required_mcp = ["playwright", "playwright"]
[spec]
paths = ["docs/specs/app.md"]
[delegation]
mode = "observe_only"
max_retries = 0
max_duration_sec = 60
max_cost_usd = 0.0
[checks.c1]
command = ["python3", "-c", "pass"]
covers = ["AC-001", "AC-002", "INV-001"]
[flows.v]
[[flows.v.steps]]
id = "s1"
type = "check"
check = "c1"
""".encode("utf-8")
        defn = load_definition_bytes(valid)
        self.assertEqual(
            defn.common_harness.required_skills,
            ("poteto-mode", "create-verification-skill"),
        )
        self.assertEqual(defn.target_harness.required_skills, ("verify-ui",))
        self.assertEqual(defn.target_harness.required_mcp, ("playwright",))

        invalid = valid.replace(
            b'required_mcp = ["playwright", "playwright"]',
            b'required_mcp = [""]',
        )
        with self.assertRaises(DefinitionError):
            load_definition_bytes(invalid)

    def test_doctor_and_preflight_enforce_declared_required_skills_and_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_garden_repo(
                base,
                common_required_skills=["create-verification-skill"],
                target_required_skills=["verify-app"],
                target_required_mcp=["playwright"],
            )
            empty_skills = base / "empty-skills"
            empty_skills.mkdir()
            empty_mcp = base / "empty.mcp.json"
            empty_mcp.write_text(
                json.dumps({"mcpServers": {"playwright": {"disabled": True}}}),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "GARDEN_SKILL_PATHS": str(empty_skills),
                    "GARDEN_MCP_CONFIG_PATHS": str(empty_mcp),
                },
            ):
                code, items = run_doctor(repo, skip_network=True)
                by_id = {i.id: i for i in items}
                self.assertEqual(code, 1)
                self.assertEqual(by_id["required_skills"].status, "error")
                self.assertIn("create-verification-skill", by_id["required_skills"].detail)
                self.assertIn("verify-app", by_id["required_skills"].detail)
                self.assertEqual(by_id["required_mcp"].status, "error")
                self.assertIn("playwright", by_id["required_mcp"].detail)

                run_res = run_flow(repo, "verify", force=True)
                self.assertEqual(run_res.verdict, "INCONCLUSIVE")
                self.assertEqual(run_res.exit_code, 3)
                self.assertEqual(
                    run_res.run_data["escalation"]["reason"],
                    "harness_requirements_missing",
                )

            # Satisfy both skills (nested pstack layout + direct skill layout) and MCP
            pstack_dir = base / "pstack"
            _write_skill(pstack_dir, "create-verification-skill", nested=True)
            _write_skill(repo / ".agents" / "skills", "verify-app", nested=False)
            active_mcp = base / "codex.toml"
            active_mcp.write_text(
                '[mcp_servers.playwright]\ncommand = "npx"\n[mcp_servers.disabled_srv]\nenabled = false\n',
                encoding="utf-8",
            )

            skill_paths = os.pathsep.join([str(pstack_dir), str(repo / ".agents" / "skills")])
            with patch.dict(
                os.environ,
                {
                    "GARDEN_SKILL_PATHS": skill_paths,
                    "GARDEN_MCP_CONFIG_PATHS": str(active_mcp),
                },
            ):
                self.assertIn("verify-app", discover_installed_skills(repo))
                mcp_found = discover_configured_mcp_servers(repo)
                self.assertIn("playwright", mcp_found)
                self.assertNotIn("disabled_srv", mcp_found)

                code2, items2 = run_doctor(repo, skip_network=True)
                by_id2 = {i.id: i for i in items2}
                self.assertEqual(code2, 0, [i.to_dict() for i in items2])
                self.assertEqual(by_id2["required_skills"].status, "ok")
                self.assertEqual(by_id2["required_mcp"].status, "ok")

                run_res2 = run_flow(repo, "verify", force=True)
                self.assertEqual(run_res2.verdict, "SATISFIED")
                self.assertEqual(run_res2.exit_code, 0)

    def test_strict_harness_and_real_agent_enforce_pstack_core_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo_obs = _make_garden_repo(base / "obs", with_real_agent=False)
            repo_agent = _make_garden_repo(base / "agt", with_real_agent=True)
            empty_skills = base / "empty-skills"
            empty_skills.mkdir()

            with patch.dict(os.environ, {"GARDEN_SKILL_PATHS": str(empty_skills)}):
                # observe_only without --strict-harness warns cleanly (CI-compatible)
                code_obs, items_obs = run_doctor(repo_obs, skip_network=True)
                by_id_obs = {i.id: i for i in items_obs}
                self.assertEqual(code_obs, 0)
                self.assertEqual(by_id_obs["required_skills"].status, "warn")

                # --strict-harness upgrades missing pstack core skills to error (exit 1)
                code_strict, items_strict = run_doctor(
                    repo_obs, skip_network=True, strict_harness=True
                )
                by_id_strict = {i.id: i for i in items_strict}
                self.assertEqual(code_strict, 1)
                self.assertEqual(by_id_strict["required_skills"].status, "error")
                self.assertIn("poteto-mode", by_id_strict["required_skills"].detail)

                # Real agent flow automatically enforces pstack core skills in doctor & preflight
                code_agt, items_agt = run_doctor(repo_agent, skip_network=True)
                self.assertEqual(code_agt, 1)
                self.assertEqual(
                    {i.id: i for i in items_agt}["required_skills"].status, "error"
                )
                run_agt = run_flow(repo_agent, "verify", force=True)
                self.assertEqual(run_agt.verdict, "INCONCLUSIVE")
                self.assertEqual(run_agt.exit_code, 3)

            # Populate all 4 pstack core skills -> strict doctor and real-agent flow pass
            pstack_dir = base / "pstack-full"
            for core in (
                "poteto-mode",
                "correct",
                "create-verification-skill",
                "maintain-verification-skill",
            ):
                _write_skill(pstack_dir, core, nested=True)

            with patch.dict(os.environ, {"GARDEN_SKILL_PATHS": str(pstack_dir)}):
                code_ok, items_ok = run_doctor(
                    repo_obs, skip_network=True, strict_harness=True
                )
                self.assertEqual(code_ok, 0, [i.to_dict() for i in items_ok])
                run_agt_ok = run_flow(repo_agent, "verify", force=True)
                self.assertEqual(run_agt_ok.verdict, "SATISFIED")
                self.assertEqual(run_agt_ok.exit_code, 0)

    def test_unapproved_spec_still_takes_precedence_over_missing_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_garden_repo(
                base,
                spec_status="draft",
                target_required_mcp=["missing-mcp"],
            )
            empty_mcp = base / "empty.mcp.json"
            empty_mcp.write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {"GARDEN_MCP_CONFIG_PATHS": str(empty_mcp)}):
                res = run_flow(repo, "verify", force=True)
            self.assertEqual(res.verdict, "NEEDS_HUMAN")
            self.assertEqual(res.exit_code, 4)
            self.assertEqual(res.run_data["escalation"]["reason"], "spec_not_approved")
