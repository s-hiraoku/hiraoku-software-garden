"""Tests for MVP-6 harness standardization (required_skills, required_mcp, and pstack core)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import stat
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest.mock import patch

from garden.definition import DefinitionError, load_definition, load_definition_bytes
from garden.doctor import run_doctor
from garden.engine import run_flow
from garden.git_utils import head_commit
from garden.harness import (
    discover_configured_mcp_servers,
    discover_installed_skills,
    probe_mcp_server,
)
from garden.harness_setup import (
    build_harness_guide,
    format_harness_guide_markdown,
    sync_pstack,
)
from garden.templates import init_garden
from tests.helpers import PSTACK_SHA, commit_all, init_target_repo, run_cli, sample_spec


VALID_INIT_RESULT = {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "serverInfo": {"name": "mock-mcp", "version": "1.0.0"},
}


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

    def test_missing_or_invalid_pstack_manifest_blocks_when_core_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo_obs = _make_garden_repo(base / "obs", with_real_agent=False)
            repo_agent = _make_garden_repo(base / "agt", with_real_agent=True)
            empty_nursery = base / "empty-nursery"
            empty_nursery.mkdir()
            broken_nursery = base / "broken-nursery"
            (broken_nursery / "harnesses").mkdir(parents=True)
            (broken_nursery / "harnesses" / "pstack.toml").write_text(
                "not = [valid toml", encoding="utf-8"
            )

            # Advisory observe_only mode warns without failing when manifest is missing
            code_adv, items_adv = run_doctor(
                repo_obs, skip_network=True, nursery_root=empty_nursery
            )
            self.assertEqual(code_adv, 0)
            self.assertEqual(
                {i.id: i for i in items_adv}["required_skills"].status, "warn"
            )

            # Strict doctor & real-agent doctor block when manifest is missing or corrupt
            for bad_nursery in (empty_nursery, broken_nursery):
                code_strict, items_strict = run_doctor(
                    repo_obs,
                    skip_network=True,
                    nursery_root=bad_nursery,
                    strict_harness=True,
                )
                self.assertEqual(code_strict, 1)
                self.assertEqual(
                    {i.id: i for i in items_strict}["required_skills"].status, "error"
                )

                code_agt, items_agt = run_doctor(
                    repo_agent, skip_network=True, nursery_root=bad_nursery
                )
                self.assertEqual(code_agt, 1)
                self.assertEqual(
                    {i.id: i for i in items_agt}["required_skills"].status, "error"
                )

            # Real-agent preflight blocks with INCONCLUSIVE when manifest cannot be loaded
            with patch("garden.harness.load_pstack_manifest", return_value=None):
                run_res = run_flow(repo_agent, "verify", force=True)
            self.assertEqual(run_res.verdict, "INCONCLUSIVE")
            self.assertEqual(run_res.exit_code, 3)
            self.assertEqual(
                run_res.run_data["escalation"]["reason"],
                "harness_requirements_missing",
            )

    def test_invalid_utf8_mcp_config_treated_as_missing_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_garden_repo(base, target_required_mcp=["playwright"])
            bad_json = base / "bad-utf8.mcp.json"
            bad_json.write_bytes(b"\xff\xfe\x00invalid-utf8")

            with patch.dict(os.environ, {"GARDEN_MCP_CONFIG_PATHS": str(bad_json)}):
                code, items = run_doctor(repo, skip_network=True)
                self.assertEqual(code, 1)
                self.assertEqual(
                    {i.id: i for i in items}["required_mcp"].status, "error"
                )

                res = run_flow(repo, "verify", force=True)
                self.assertEqual(res.verdict, "INCONCLUSIVE")
                self.assertEqual(res.exit_code, 3)
                self.assertEqual(
                    res.run_data["escalation"]["reason"],
                    "harness_requirements_missing",
                )

    def test_mcp_server_config_requires_object_with_non_empty_command_or_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_garden_repo(base, target_required_mcp=["null_srv"])
            cfg_path = base / "mcp.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "null_srv": None,
                            "empty_obj": {},
                            "blank_cmd": {"command": "   "},
                            "blank_url": {"url": ""},
                            "non_str_cmd": {"command": ["npx"]},
                            "valid_cmd": {"command": "npx", "args": ["-y", "pw"]},
                            "valid_url": {"url": "https://example.com/mcp"},
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"GARDEN_MCP_CONFIG_PATHS": str(cfg_path)}):
                found = discover_configured_mcp_servers(repo)
                self.assertEqual(set(found.keys()), {"valid_cmd", "valid_url"})

                code, items = run_doctor(repo, skip_network=True)
                self.assertEqual(code, 1)
                self.assertEqual(
                    {i.id: i for i in items}["required_mcp"].status, "error"
                )

    def test_codex_user_scope_skills_dir_discovered_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_home = base / "home"
            fake_home.mkdir()
            _write_skill(fake_home / ".agents" / "skills", "user-codex-skill")
            repo = _make_garden_repo(
                base / "proj", target_required_skills=["user-codex-skill"]
            )

            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("GARDEN_SKILL_PATHS", "GARDEN_MCP_CONFIG_PATHS")
            }
            clean_env["HOME"] = str(fake_home)
            with patch.dict(os.environ, clean_env, clear=True):
                found = discover_installed_skills(repo)
                self.assertIn("user-codex-skill", found)

                code, items = run_doctor(repo, skip_network=True)
                self.assertEqual(code, 0, [i.to_dict() for i in items])
                self.assertEqual(
                    {i.id: i for i in items}["required_skills"].status, "ok"
                )

    def test_adapter_scoped_mcp_discovery_and_project_disable_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_home = base / "home"
            fake_home.mkdir()
            for core in (
                "poteto-mode",
                "correct",
                "create-verification-skill",
                "maintain-verification-skill",
            ):
                _write_skill(fake_home / ".agents" / "skills", core)

            repo = _make_garden_repo(
                base / "proj",
                target_required_mcp=["playwright"],
                with_real_agent=True,  # uses codex adapter
            )
            # Register playwright ONLY in Cursor config (not Codex)
            (repo / ".cursor").mkdir(exist_ok=True)
            (repo / ".cursor" / "mcp.json").write_text(
                json.dumps({"mcpServers": {"playwright": {"command": "npx"}}}),
                encoding="utf-8",
            )
            commit_all(repo, "add cursor mcp only")

            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("GARDEN_SKILL_PATHS", "GARDEN_MCP_CONFIG_PATHS")
            }
            clean_env["HOME"] = str(fake_home)

            with patch.dict(os.environ, clean_env, clear=True):
                code_cursor_only, items_cursor_only = run_doctor(
                    repo, skip_network=True
                )
                self.assertEqual(code_cursor_only, 1)
                self.assertEqual(
                    {i.id: i for i in items_cursor_only}["required_mcp"].status, "error"
                )
                run_cursor_only = run_flow(repo, "verify", force=True)
                self.assertEqual(run_cursor_only.verdict, "INCONCLUSIVE")
                self.assertEqual(run_cursor_only.exit_code, 3)

                # Register playwright in project .codex/config.toml -> Codex passes
                (repo / ".codex").mkdir(exist_ok=True)
                (repo / ".codex" / "config.toml").write_text(
                    '[mcp_servers.playwright]\ncommand = "npx"\n',
                    encoding="utf-8",
                )
                commit_all(repo, "add project codex mcp")

                code_codex_ok, items_codex_ok = run_doctor(repo, skip_network=True)
                self.assertEqual(code_codex_ok, 0, [i.to_dict() for i in items_codex_ok])
                self.assertEqual(
                    {i.id: i for i in items_codex_ok}["required_mcp"].status, "ok"
                )
                run_codex_ok = run_flow(repo, "verify", force=True)
                self.assertEqual(run_codex_ok.verdict, "SATISFIED")
                self.assertEqual(run_codex_ok.exit_code, 0)

                # Enable in user ~/.codex/config.toml, but disable in project .codex/config.toml -> blocked
                (fake_home / ".codex").mkdir(exist_ok=True)
                (fake_home / ".codex" / "config.toml").write_text(
                    '[mcp_servers.playwright]\ncommand = "npx"\n',
                    encoding="utf-8",
                )
                (repo / ".codex" / "config.toml").write_text(
                    "[mcp_servers.playwright]\nenabled = false\n",
                    encoding="utf-8",
                )
                commit_all(repo, "disable playwright in project codex config")

                code_disabled, items_disabled = run_doctor(repo, skip_network=True)
                self.assertEqual(code_disabled, 1)
                self.assertEqual(
                    {i.id: i for i in items_disabled}["required_mcp"].status, "error"
                )
                run_disabled = run_flow(repo, "verify", force=True)
                self.assertEqual(run_disabled.verdict, "INCONCLUSIVE")
                self.assertEqual(run_disabled.exit_code, 3)

    def test_preflight_checks_execution_worktree_not_dirty_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_home = base / "home"
            fake_home.mkdir()
            repo = _make_garden_repo(
                base / "proj", target_required_skills=["repo-skill"]
            )
            skill_md = _write_skill(repo / ".agents" / "skills", "repo-skill")
            commit_all(repo, "commit repo-skill")

            # Remove from local working tree only; HEAD still has it
            skill_md.unlink()

            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("GARDEN_SKILL_PATHS", "GARDEN_MCP_CONFIG_PATHS")
            }
            clean_env["HOME"] = str(fake_home)

            with patch.dict(os.environ, clean_env, clear=True):
                res_in_head = run_flow(repo, "verify", force=True)
                self.assertEqual(res_in_head.verdict, "SATISFIED")
                self.assertEqual(res_in_head.exit_code, 0)

                # Commit removal from HEAD, then recreate untracked only in working tree -> blocked
                commit_all(repo, "remove repo-skill from HEAD")
                _write_skill(repo / ".agents" / "skills", "repo-skill")

                res_only_dirty = run_flow(repo, "verify", force=True)
                self.assertEqual(res_only_dirty.verdict, "INCONCLUSIVE")
                self.assertEqual(res_only_dirty.exit_code, 3)
                self.assertEqual(
                    res_only_dirty.run_data["escalation"]["reason"],
                    "harness_requirements_missing",
                )
                # Verify temporary worktree was cleaned up after INCONCLUSIVE return
                wt_root = repo / ".garden" / "worktrees"
                remaining = list(wt_root.iterdir()) if wt_root.exists() else []
                self.assertEqual(remaining, [])

    def test_doctor_probe_mcp_stdio_and_http_live_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_garden_repo(base / "proj", target_required_mcp=["demo"])

            # 1. Passing stdio MCP mock server
            ok_srv = base / "mcp_ok.py"
            ok_srv.write_text(
                "import json, sys\n"
                "line = sys.stdin.readline()\n"
                "req = json.loads(line)\n"
                f"print(json.dumps({{'jsonrpc': '2.0', 'id': req['id'], 'result': {VALID_INIT_RESULT!r}}}), flush=True)\n",
                encoding="utf-8",
            )
            bad_srv = base / "mcp_err.py"
            bad_srv.write_text(
                "import json, sys\n"
                "line = sys.stdin.readline()\n"
                "req = json.loads(line)\n"
                "print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'error': {'code': -32000, 'message': 'boom'}}), flush=True)\n",
                encoding="utf-8",
            )

            mcp_cfg = base / "mcp.json"
            mcp_cfg.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "demo": {"command": sys.executable, "args": [str(ok_srv)]}
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"GARDEN_MCP_CONFIG_PATHS": str(mcp_cfg)}):
                code_ok, items_ok = run_doctor(repo, skip_network=True, probe_mcp=True)
                self.assertEqual(code_ok, 0, [i.to_dict() for i in items_ok])
                self.assertIn("probed", {i.id: i for i in items_ok}["required_mcp"].detail)

            # 2. Failing stdio JSON-RPC error
            mcp_cfg.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "demo": {"command": sys.executable, "args": [str(bad_srv)]}
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"GARDEN_MCP_CONFIG_PATHS": str(mcp_cfg)}):
                code_err, items_err = run_doctor(repo, skip_network=True, probe_mcp=True)
                self.assertEqual(code_err, 1)
                detail_err = {i.id: i for i in items_err}["required_mcp"].detail
                self.assertIn("boom", detail_err)

            # 3. Missing executable
            ok_miss, msg_miss = probe_mcp_server(
                {"command": "definitely-nonexistent-mcp-bin-xyz"}, repo
            )
            self.assertFalse(ok_miss)
            self.assertIn("executable not found", msg_miss)

            # 4. Live HTTP MCP server (200 JSON-RPC ok & 401 auth required)
            class _Handler(BaseHTTPRequestHandler):
                def log_message(self, format: str, *args: object) -> None:
                    return

                def do_POST(self) -> None:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = self.rfile.read(length).decode("utf-8")
                    if self.path == "/auth":
                        self.send_response(401)
                        self.end_headers()
                        return
                    req = json.loads(body)
                    resp = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": req["id"],
                            "result": VALID_INIT_RESULT,
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)

            httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                mcp_cfg.write_text(
                    json.dumps(
                        {"mcpServers": {"demo": {"url": f"http://127.0.0.1:{port}/mcp"}}}
                    ),
                    encoding="utf-8",
                )
                with patch.dict(os.environ, {"GARDEN_MCP_CONFIG_PATHS": str(mcp_cfg)}):
                    code_http, items_http = run_doctor(
                        repo, skip_network=True, probe_mcp=True
                    )
                    self.assertEqual(code_http, 0, [i.to_dict() for i in items_http])

                mcp_cfg.write_text(
                    json.dumps(
                        {"mcpServers": {"demo": {"url": f"http://127.0.0.1:{port}/auth"}}}
                    ),
                    encoding="utf-8",
                )
                with patch.dict(os.environ, {"GARDEN_MCP_CONFIG_PATHS": str(mcp_cfg)}):
                    code_401, items_401 = run_doctor(
                        repo, skip_network=True, probe_mcp=True
                    )
                    self.assertEqual(code_401, 1)
                    self.assertIn(
                        "HTTP 401",
                        {i.id: i for i in items_401}["required_mcp"].detail,
                    )
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_sync_pstack_dry_run_and_apply_plugin_and_skills_layouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Create a local disposable upstream repo containing pinned pstack subpath
            upstream_repo = init_target_repo(base / "upstream-pstack")
            for core in (
                "poteto-mode",
                "correct",
                "create-verification-skill",
                "maintain-verification-skill",
            ):
                _write_skill(upstream_repo / "pstack", core, nested=True)
            commit_all(upstream_repo, "add pstack core skills")
            pinned_sha = head_commit(upstream_repo)

            nursery = base / "nursery"
            (nursery / "harnesses").mkdir(parents=True)
            (nursery / "harnesses" / "pstack.toml").write_text(
                f"""
schema_version = 1
name = "pstack"
upstream = "{upstream_repo}"
subpath = "pstack"
pinned_ref = "{pinned_sha}"

[skills]
core = [
  "poteto-mode",
  "correct",
  "create-verification-skill",
  "maintain-verification-skill",
]
""",
                encoding="utf-8",
            )

            # Dry-run must not write anything
            plugin_dest = base / "antigravity-pstack"
            plan = sync_pstack(
                adapter="antigravity",
                dest=plugin_dest,
                apply=False,
                nursery_root=nursery,
            )
            self.assertFalse(plan["applied"])
            self.assertFalse(plugin_dest.exists())

            # Apply in plugin layout (antigravity)
            applied = sync_pstack(
                adapter="antigravity",
                dest=plugin_dest,
                apply=True,
                nursery_root=nursery,
            )
            self.assertTrue(applied["applied"])
            self.assertEqual(
                (plugin_dest / ".garden-pstack-ref").read_text(encoding="utf-8").strip(),
                pinned_sha,
            )
            with patch.dict(os.environ, {"GARDEN_SKILL_PATHS": str(plugin_dest)}):
                found = discover_installed_skills(base)
                for core in applied["verified_core_skills"]:
                    self.assertIn(core, found)

            # Apply in skills_dir layout (codex) must preserve unrelated custom skills
            skills_dest = base / "codex-skills"
            custom_skill = _write_skill(skills_dest, "my-unrelated-skill")
            applied_codex = sync_pstack(
                adapter="codex",
                dest=skills_dest,
                apply=True,
                nursery_root=nursery,
            )
            self.assertTrue(applied_codex["applied"])
            self.assertTrue(custom_skill.is_file())
            self.assertTrue((skills_dest / "poteto-mode" / "SKILL.md").is_file())

    def test_init_with_standard_harness_and_harness_guide(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = init_target_repo(base / "my-web-app")
            (repo / "package.json").write_text(
                '{"name":"web","dependencies":{"react":"^19.0.0","vite":"^6.0.0"}}',
                encoding="utf-8",
            )
            commit_all(repo, "seed web repo")

            init_res = init_garden(repo, with_standard_harness=True)
            self.assertEqual(init_res["template"], "web-frontend")
            self.assertTrue(init_res["with_standard_harness"])

            defn = load_definition(repo / ".garden" / "garden.toml")
            self.assertEqual(
                defn.common_harness.required_skills,
                ("create-verification-skill", "maintain-verification-skill"),
            )
            self.assertEqual(defn.target_harness.required_mcp, ("playwright",))

            empty_skills = base / "empty-skills"
            empty_skills.mkdir()
            empty_mcp = base / "empty.mcp.json"
            empty_mcp.write_text("{}", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "GARDEN_SKILL_PATHS": str(empty_skills),
                    "GARDEN_MCP_CONFIG_PATHS": str(empty_mcp),
                },
            ):
                guide = build_harness_guide(repo, adapter="all")
                self.assertFalse(guide["status"]["ok"])
                self.assertIn("playwright", guide["status"]["missing_mcp"])
                self.assertIn('[mcp_servers."playwright"]', guide["environments"]["codex"]["mcp_snippet"])
                self.assertIn("@playwright/mcp@latest", guide["environments"]["antigravity"]["mcp_snippet"])
                md = format_harness_guide_markdown(guide)
                self.assertIn("garden harness sync-pstack --adapter codex --apply", md)

            # CLI smoke for `garden harness sync-pstack` dry-run
            proc = run_cli("harness", "sync-pstack", "--adapter", "codex", "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertFalse(out["applied"])
            self.assertEqual(out["adapter"], "codex")

    def test_project_stdio_mcp_requires_explicit_trust_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_home = base / "home"
            fake_home.mkdir()
            repo = _make_garden_repo(
                base / "proj",
                spec_status="draft",
                target_required_mcp=["local_tool"],
            )
            marker = base / "executed.txt"
            probe_script = base / "touch_and_init.py"
            probe_script.write_text(
                f"import json, pathlib, sys\n"
                f"pathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
                f"req = json.loads(sys.stdin.readline())\n"
                f"print(json.dumps({{'jsonrpc': '2.0', 'id': req['id'], 'result': {VALID_INIT_RESULT!r}}}), flush=True)\n",
                encoding="utf-8",
            )
            (repo / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "local_tool": {
                                "command": sys.executable,
                                "args": [str(probe_script)],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            commit_all(repo, "add project mcp")

            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("GARDEN_SKILL_PATHS", "GARDEN_MCP_CONFIG_PATHS")
            }
            clean_env["HOME"] = str(fake_home)
            with patch.dict(os.environ, clean_env, clear=True):
                code, items = run_doctor(repo, skip_network=True, probe_mcp=True)
                self.assertEqual(code, 1)
                self.assertIn(
                    "--trust-project-mcp",
                    {i.id: i for i in items}["required_mcp"].detail,
                )
                self.assertFalse(marker.exists(), "untrusted project stdio MCP must not execute")

                guide = build_harness_guide(repo, adapter="antigravity", probe_mcp=True)
                self.assertFalse(guide["status"]["ok"])
                self.assertIn("local_tool", guide["status"]["mcp_probe_errors"])
                self.assertFalse(marker.exists())

                # Explicit opt-in executes and succeeds
                guide_ok = build_harness_guide(
                    repo,
                    adapter="antigravity",
                    probe_mcp=True,
                    trust_project_mcp=True,
                )
                self.assertTrue(guide_ok["status"]["ok"], guide_ok["status"])
                self.assertTrue(marker.exists())

    def test_http_mcp_probe_multiline_json_sse_legacy_redirect_and_codex_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_garden_repo(base / "proj")

            # Server B: cross-origin redirect target that records headers it received
            seen_target_headers: dict[str, str] = {}

            class _TargetHandler(BaseHTTPRequestHandler):
                def log_message(self, format: str, *args: object) -> None:
                    return

                def do_GET(self) -> None:
                    seen_target_headers.clear()
                    for k, v in self.headers.items():
                        seen_target_headers[k.lower()] = v
                    if self.path == "/redir-sse":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        self.wfile.write(b"event: endpoint\ndata: /redir-msg\n\n")
                        self.wfile.flush()
                        return
                    body = json.dumps(
                        {"jsonrpc": "2.0", "id": 1, "result": VALID_INIT_RESULT},
                        indent=2,
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_POST(self) -> None:
                    length = int(self.headers.get("Content-Length", "0"))
                    req = json.loads(self.rfile.read(length).decode("utf-8"))
                    seen_target_headers.clear()
                    for k, v in self.headers.items():
                        seen_target_headers[k.lower()] = v
                    body = json.dumps(
                        {"jsonrpc": "2.0", "id": req["id"], "result": VALID_INIT_RESULT},
                        indent=2,
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            srv_b = ThreadingHTTPServer(("127.0.0.1", 0), _TargetHandler)
            port_b = srv_b.server_address[1]
            threading.Thread(target=srv_b.serve_forever, daemon=True).start()

            legacy_queue: queue.Queue[str] = queue.Queue()

            class _PrimaryHandler(BaseHTTPRequestHandler):
                def log_message(self, format: str, *args: object) -> None:
                    return

                def do_POST(self) -> None:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length).decode("utf-8")
                    if self.path == "/pretty-auth":
                        if (
                            self.headers.get("Authorization") != "Bearer secret-tok"
                            or self.headers.get("X-Static") != "s1"
                            or self.headers.get("X-From-Env") != "e1"
                        ):
                            self.send_response(401)
                            self.end_headers()
                            return
                        req = json.loads(raw)
                        body = json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": req["id"],
                                "result": VALID_INIT_RESULT,
                            },
                            indent=2,
                        ).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if self.path == "/incomplete-result":
                        req = json.loads(raw)
                        body = json.dumps(
                            {"jsonrpc": "2.0", "id": req["id"], "result": {}}
                        ).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if self.path == "/sse-open":
                        req = json.loads(raw)
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        ev = (
                            "event: message\n"
                            f"data: {json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': VALID_INIT_RESULT})}\n\n"
                        )
                        self.wfile.write(ev.encode("utf-8"))
                        self.wfile.flush()
                        # Keep stream open past test check to ensure incremental parsing
                        time.sleep(2.0)
                        return
                    if self.path == "/sse-slow-drip":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        for _ in range(30):
                            try:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                            except OSError:
                                break
                            time.sleep(0.08)
                        return
                    if self.path == "/sse-oversized":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        try:
                            self.wfile.write(b"data: " + (b"x" * 70000) + b"\n\n")
                            self.wfile.flush()
                        except OSError:
                            pass
                        return
                    if self.path == "/redirect-cross-origin":
                        self.send_response(302)
                        self.send_header("Location", f"http://127.0.0.1:{port_b}/dst")
                        self.end_headers()
                        return
                    if self.path in ("/redirect-get-405", "/redirect-legacy-sse"):
                        self.send_response(405)
                        self.end_headers()
                        return
                    if self.path == "/legacy-sse-400":
                        self.send_response(400)
                        self.end_headers()
                        return
                    if self.path == "/legacy-sse-404":
                        self.send_response(404)
                        self.end_headers()
                        return
                    if self.path in ("/html-405", "/legacy-sse"):
                        self.send_response(405)
                        self.end_headers()
                        return
                    if self.path == "/legacy-msg":
                        req = json.loads(raw)
                        legacy_queue.put(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": req["id"],
                                    "result": VALID_INIT_RESULT,
                                }
                            )
                        )
                        self.send_response(202)
                        self.end_headers()
                        return
                    self.send_response(404)
                    self.end_headers()

                def do_GET(self) -> None:
                    if self.path == "/redirect-get-405":
                        self.send_response(307)
                        self.send_header("Location", f"http://127.0.0.1:{port_b}/dst")
                        self.end_headers()
                        return
                    if self.path == "/redirect-legacy-sse":
                        self.send_response(307)
                        self.send_header("Location", f"http://127.0.0.1:{port_b}/redir-sse")
                        self.end_headers()
                        return
                    if self.path == "/html-405":
                        body = b"<html><body>Not an MCP server</body></html>"
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if self.path in ("/legacy-sse", "/legacy-sse-400", "/legacy-sse-404"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        self.wfile.write(b"event: endpoint\ndata: /legacy-msg\n\n")
                        self.wfile.flush()
                        try:
                            msg = legacy_queue.get(timeout=3.0)
                            self.wfile.write(f"event: message\ndata: {msg}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except queue.Empty:
                            pass
                        return
                    self.send_response(404)
                    self.end_headers()

            srv_a = ThreadingHTTPServer(("127.0.0.1", 0), _PrimaryHandler)
            port_a = srv_a.server_address[1]
            threading.Thread(target=srv_a.serve_forever, daemon=True).start()
            try:
                # 1. Multiline pretty JSON + Codex auth fields
                with patch.dict(
                    os.environ,
                    {"MY_MCP_TOKEN": "secret-tok", "MY_ENV_HDR": "e1"},
                ):
                    ok_auth, msg_auth = probe_mcp_server(
                        {
                            "url": f"http://127.0.0.1:{port_a}/pretty-auth",
                            "bearer_token_env_var": "MY_MCP_TOKEN",
                            "http_headers": {"X-Static": "s1"},
                            "env_http_headers": {"X-From-Env": "MY_ENV_HDR"},
                        },
                        repo,
                    )
                    self.assertTrue(ok_auth, msg_auth)

                # Missing env var for Codex bearer token fails before sending
                ok_missing_env, msg_missing_env = probe_mcp_server(
                    {
                        "url": f"http://127.0.0.1:{port_a}/pretty-auth",
                        "bearer_token_env_var": "UNSET_MCP_TOKEN_XYZ",
                    },
                    repo,
                )
                self.assertFalse(ok_missing_env)
                self.assertIn("UNSET_MCP_TOKEN_XYZ", msg_missing_env)

                # 2. Open-ended SSE stream returns promptly on initialize event
                t0 = time.monotonic()
                ok_sse, msg_sse = probe_mcp_server(
                    {"url": f"http://127.0.0.1:{port_a}/sse-open"},
                    repo,
                    timeout_sec=1.2,
                )
                elapsed = time.monotonic() - t0
                self.assertTrue(ok_sse, msg_sse)
                self.assertLess(elapsed, 1.0)

                # 3. Cross-origin redirect strips Authorization and configured custom headers (POST 302 & 405->GET 307)
                ok_redir, msg_redir = probe_mcp_server(
                    {
                        "url": f"http://127.0.0.1:{port_a}/redirect-cross-origin",
                        "headers": {
                            "Authorization": "Bearer leak-me-not",
                            "X-Api-Key": "top-secret",
                        },
                    },
                    repo,
                )
                self.assertTrue(ok_redir, msg_redir)
                self.assertTrue(seen_target_headers)
                self.assertNotIn("authorization", seen_target_headers)
                self.assertNotIn("x-api-key", seen_target_headers)

                seen_target_headers.clear()
                ok_get_redir, _ = probe_mcp_server(
                    {
                        "url": f"http://127.0.0.1:{port_a}/redirect-get-405",
                        "headers": {
                            "Authorization": "Bearer leak-me-not-get",
                            "X-Api-Key": "top-secret-get",
                        },
                    },
                    repo,
                )
                self.assertFalse(ok_get_redir)  # target returns JSON, not SSE stream
                self.assertTrue(seen_target_headers)
                self.assertNotIn("authorization", seen_target_headers)
                self.assertNotIn("x-api-key", seen_target_headers)

                # 4. POST 405 -> GET 200 HTML page is REJECTED
                ok_html, msg_html = probe_mcp_server(
                    {"url": f"http://127.0.0.1:{port_a}/html-405"}, repo
                )
                self.assertFalse(ok_html)
                self.assertIn("text/event-stream", msg_html)

                # 5. Full legacy HTTP+SSE handshake (405, 400, and 404 -> GET endpoint -> POST 202 -> SSE result)
                for path in ("/legacy-sse", "/legacy-sse-400", "/legacy-sse-404"):
                    ok_legacy, msg_legacy = probe_mcp_server(
                        {"url": f"http://127.0.0.1:{port_a}{path}"},
                        repo,
                        timeout_sec=3.0,
                    )
                    self.assertTrue(ok_legacy, f"{path}: {msg_legacy}")

                # 6. Cross-origin legacy SSE redirect must NOT restore auth headers on follow-up POST
                seen_target_headers.clear()
                ok_redir_sse, msg_redir_sse = probe_mcp_server(
                    {
                        "url": f"http://127.0.0.1:{port_a}/redirect-legacy-sse",
                        "headers": {
                            "Authorization": "Bearer never-restore",
                            "X-Api-Key": "never-restore-key",
                        },
                    },
                    repo,
                    timeout_sec=3.0,
                )
                self.assertTrue(ok_redir_sse, msg_redir_sse)
                self.assertTrue(seen_target_headers)
                self.assertNotIn("authorization", seen_target_headers)
                self.assertNotIn("x-api-key", seen_target_headers)

                # 7. Incomplete initialize result (missing protocolVersion/capabilities/serverInfo) is REJECTED
                ok_inc, msg_inc = probe_mcp_server(
                    {"url": f"http://127.0.0.1:{port_a}/incomplete-result"}, repo
                )
                self.assertFalse(ok_inc)
                self.assertIn("protocolVersion", msg_inc)

                # 8. Slow-drip SSE respects total deadline & oversized SSE event is bounded
                t_drip = time.monotonic()
                ok_drip, msg_drip = probe_mcp_server(
                    {"url": f"http://127.0.0.1:{port_a}/sse-slow-drip"},
                    repo,
                    timeout_sec=0.4,
                )
                self.assertFalse(ok_drip)
                self.assertLess(time.monotonic() - t_drip, 1.5)
                self.assertIn("timed out", msg_drip)

                ok_big, msg_big = probe_mcp_server(
                    {"url": f"http://127.0.0.1:{port_a}/sse-oversized"},
                    repo,
                    timeout_sec=2.0,
                )
                self.assertFalse(ok_big)
                self.assertIn("byte limit", msg_big)
            finally:
                srv_a.shutdown()
                srv_a.server_close()
                srv_b.shutdown()
                srv_b.server_close()

    def test_stdio_probe_uses_configured_env_path_and_bounds_descendant_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_garden_repo(base / "proj")
            custom_bin = base / "custom-bin"
            custom_bin.mkdir()
            exe = custom_bin / "my-custom-mcp-srv"
            exe.write_text(
                f"#!{sys.executable}\n"
                "import json, sys\n"
                "req = json.loads(sys.stdin.readline())\n"
                f"print(json.dumps({{'jsonrpc': '2.0', 'id': req['id'], 'result': {VALID_INIT_RESULT!r}}}), flush=True)\n",
                encoding="utf-8",
            )
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)

            ok_path, msg_path = probe_mcp_server(
                {"command": "my-custom-mcp-srv", "env": {"PATH": str(custom_bin)}},
                repo,
            )
            self.assertTrue(ok_path, msg_path)

            # Launcher spawns a sleeping child inheriting pipes -> must terminate promptly
            launcher = base / "hang_launcher.py"
            launcher.write_text(
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            t0 = time.monotonic()
            ok_hang, msg_hang = probe_mcp_server(
                {"command": sys.executable, "args": [str(launcher)]},
                repo,
                timeout_sec=0.3,
            )
            elapsed = time.monotonic() - t0
            self.assertFalse(ok_hang)
            self.assertIn("timed out", msg_hang)
            self.assertLess(elapsed, 2.5)

    def test_schema_dir_spoofing_rejected_and_dotted_codex_toml_key_quoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_garden_repo(
                base / "proj", target_required_mcp=["browser.prod"]
            )
            spoof = base / "spoof.mcp.json"
            spoof.write_text(
                json.dumps({"mcpServers": {"browser.prod": {"_schema_dir": "/tmp"}}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"GARDEN_MCP_CONFIG_PATHS": str(spoof)}):
                found = discover_configured_mcp_servers(repo)
                self.assertNotIn("browser.prod", found)

                guide = build_harness_guide(repo, adapter="codex")
                snippet = guide["environments"]["codex"]["mcp_snippet"]
                parsed = tomllib.loads(snippet)
                self.assertIn("browser.prod", parsed["mcp_servers"])

    def test_sync_pstack_symlink_rejection_custom_preservation_rollback_and_adapter_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            upstream_repo = init_target_repo(base / "upstream-pstack")
            for core in (
                "poteto-mode",
                "correct",
                "create-verification-skill",
                "maintain-verification-skill",
            ):
                _write_skill(upstream_repo / "pstack", core, nested=True)
            commit_all(upstream_repo, "add pstack")
            pinned_sha = head_commit(upstream_repo)

            nursery = base / "nursery"
            (nursery / "harnesses").mkdir(parents=True)
            (nursery / "harnesses" / "pstack.toml").write_text(
                f"""
schema_version = 1
name = "pstack"
upstream = "{upstream_repo}"
subpath = "pstack"
pinned_ref = "{pinned_sha}"

[skills]
core = [
  "poteto-mode",
  "correct",
  "create-verification-skill",
  "maintain-verification-skill",
]
""",
                encoding="utf-8",
            )

            # 1. Symlink destination must be rejected in BOTH dry-run and apply
            real_checkout = base / "real-dev-pstack"
            real_checkout.mkdir()
            sentinel = real_checkout / "local-work.txt"
            sentinel.write_text("do not delete", encoding="utf-8")
            sym_dest = base / "pstack-symlink"
            sym_dest.symlink_to(real_checkout)
            for do_apply in (False, True):
                with self.assertRaises(ValueError):
                    sync_pstack(
                        adapter="antigravity",
                        dest=sym_dest,
                        apply=do_apply,
                        nursery_root=nursery,
                    )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not delete")

            # 2. Plugin layout preserves existing non-conflicting custom skills
            plugin_dest = base / "plugin-dest"
            custom_plugin_skill = _write_skill(plugin_dest, "local-extra-skill", nested=True)
            sync_pstack(
                adapter="antigravity",
                dest=plugin_dest,
                apply=True,
                nursery_root=nursery,
            )
            self.assertTrue(custom_plugin_skill.is_file())
            self.assertTrue((plugin_dest / "skills" / "poteto-mode" / "SKILL.md").is_file())

            # 3. Shared skills_dir refuses to overwrite unmanaged colliding skill without --force
            shared_dest = base / "shared-skills"
            unmanaged = _write_skill(shared_dest, "poteto-mode")
            unmanaged.write_text("custom unmanaged content", encoding="utf-8")
            with self.assertRaises(ValueError):
                sync_pstack(
                    adapter="codex",
                    dest=shared_dest,
                    apply=True,
                    nursery_root=nursery,
                )
            self.assertEqual(unmanaged.read_text(encoding="utf-8"), "custom unmanaged content")

            # With --force, replaces and marks managed; subsequent sync succeeds without --force
            sync_pstack(
                adapter="codex",
                dest=shared_dest,
                apply=True,
                force=True,
                nursery_root=nursery,
            )
            self.assertTrue((shared_dest / "poteto-mode" / ".garden-pstack-ref").is_file())
            sync_pstack(
                adapter="codex",
                dest=shared_dest,
                apply=True,
                force=False,
                nursery_root=nursery,
            )

            # 4. Approved [common_harness].ref mismatch is rejected, and --adapter auto targets claude
            claude_repo = _make_garden_repo(base / "claude-proj", with_real_agent=True)
            gpath = claude_repo / ".garden" / "garden.toml"
            gpath.write_text(
                gpath.read_text(encoding="utf-8").replace(
                    'adapter = "codex"', 'adapter = "claude"'
                ),
                encoding="utf-8",
            )
            commit_all(claude_repo, "switch to claude adapter")

            # Mismatched ref (PSTACK_SHA != pinned_sha) -> rejected!
            with self.assertRaises(ValueError):
                sync_pstack(
                    adapter="auto",
                    target_root=claude_repo,
                    approval_branch="main",
                    apply=False,
                    nursery_root=nursery,
                )

            # Align approved ref -> --adapter auto resolves to claude (~/.claude/skills)
            gpath.write_text(
                gpath.read_text(encoding="utf-8").replace(PSTACK_SHA, pinned_sha),
                encoding="utf-8",
            )
            commit_all(claude_repo, "align pstack ref")

            fake_home = base / "fake-home"
            fake_home.mkdir()
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("GARDEN_SKILL_PATHS", "GARDEN_MCP_CONFIG_PATHS")
            }
            clean_env["HOME"] = str(fake_home)
            with patch.dict(os.environ, clean_env, clear=True):
                # Putting skills only in Codex ~/.agents/skills must NOT satisfy Claude flow
                for core in (
                    "poteto-mode",
                    "correct",
                    "create-verification-skill",
                    "maintain-verification-skill",
                ):
                    _write_skill(fake_home / ".agents" / "skills", core)
                code_before, _ = run_doctor(
                    claude_repo, skip_network=True, nursery_root=nursery
                )
                self.assertEqual(code_before, 1)

                # --adapter auto installs to ~/.claude/skills and Claude doctor passes
                auto_res = sync_pstack(
                    adapter="auto",
                    target_root=claude_repo,
                    approval_branch="main",
                    apply=True,
                    nursery_root=nursery,
                )
                self.assertEqual(auto_res["resolved_adapters"], ["claude"])
                self.assertTrue((fake_home / ".claude" / "skills" / "poteto-mode" / "SKILL.md").is_file())
                code_after, items_after = run_doctor(
                    claude_repo, skip_network=True, nursery_root=nursery
                )
                self.assertEqual(code_after, 0, [i.to_dict() for i in items_after])

    def test_pr30_security_http_auth_trust_stdio_checkout_and_orphan_reclaim(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_home = base / "home"
            fake_home.mkdir()
            repo = _make_garden_repo(
                base / "proj",
                target_required_mcp=["api_srv"],
                with_real_agent=True,
            )
            for core in (
                "poteto-mode",
                "correct",
                "create-verification-skill",
                "maintain-verification-skill",
            ):
                _write_skill(fake_home / ".agents" / "skills", core)

            # 1. Project .codex/config.toml cannot expand bearer_token_env_var without --trust-project-mcp
            (repo / ".codex").mkdir(exist_ok=True)
            (repo / ".codex" / "config.toml").write_text(
                '[mcp_servers.api_srv]\nurl = "http://127.0.0.1:9/mcp"\nbearer_token_env_var = "SECRET_API_TOK"\n',
                encoding="utf-8",
            )
            commit_all(repo, "add untrusted project http auth")

            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("GARDEN_SKILL_PATHS", "GARDEN_MCP_CONFIG_PATHS")
            }
            clean_env["HOME"] = str(fake_home)
            clean_env["SECRET_API_TOK"] = "should-not-leak"

            with patch.dict(os.environ, clean_env, clear=True):
                code, items = run_doctor(repo, skip_network=True, probe_mcp=True)
                self.assertEqual(code, 1)
                self.assertIn(
                    "--trust-project-mcp",
                    {i.id: i for i in items}["required_mcp"].detail,
                )

                # 2. User config sets auth at origin A, project overrides url to origin B -> refused!
                (fake_home / ".codex").mkdir(exist_ok=True)
                (fake_home / ".codex" / "config.toml").write_text(
                    '[mcp_servers.api_srv]\nurl = "http://127.0.0.1:1111/mcp"\nbearer_token_env_var = "SECRET_API_TOK"\n',
                    encoding="utf-8",
                )
                (repo / ".codex" / "config.toml").write_text(
                    '[mcp_servers.api_srv]\nurl = "http://127.0.0.1:2222/evil"\n',
                    encoding="utf-8",
                )
                commit_all(repo, "override url origin in project")
                code2, items2 = run_doctor(repo, skip_network=True, probe_mcp=True)
                self.assertEqual(code2, 1)
                self.assertIn(
                    "overrode URL origin",
                    {i.id: i for i in items2}["required_mcp"].detail,
                )

                # 3. Bare command in user config MUST NOT fall back to executable inside checkout
                sneaky = repo / "sneaky-checkout-bin"
                marker = base / "sneaky-ran.txt"
                sneaky.write_text(
                    f"#!{sys.executable}\nimport pathlib; pathlib.Path({str(marker)!r}).write_text('pwned')\n",
                    encoding="utf-8",
                )
                sneaky.chmod(sneaky.stat().st_mode | stat.S_IXUSR)
                (fake_home / ".codex" / "config.toml").write_text(
                    '[mcp_servers.api_srv]\ncommand = "sneaky-checkout-bin"\n',
                    encoding="utf-8",
                )
                (repo / ".codex" / "config.toml").unlink()
                commit_all(repo, "add sneaky binary in checkout")

                code3, items3 = run_doctor(repo, skip_network=True, probe_mcp=True)
                self.assertEqual(code3, 1)
                self.assertFalse(marker.exists())
                self.assertIn(
                    "executable not found",
                    {i.id: i for i in items3}["required_mcp"].detail,
                )

                # Explicit relative path into checkout from user config also requires --trust-project-mcp
                (fake_home / ".codex" / "config.toml").write_text(
                    '[mcp_servers.api_srv]\ncommand = "./sneaky-checkout-bin"\n',
                    encoding="utf-8",
                )
                code4, items4 = run_doctor(repo, skip_network=True, probe_mcp=True)
                self.assertEqual(code4, 1)
                self.assertFalse(marker.exists())
                self.assertIn(
                    "--trust-project-mcp",
                    {i.id: i for i in items4}["required_mcp"].detail,
                )

            # 4. Parent exits immediately while child holds stdout open -> process group reclaimed
            if os.name == "posix":
                pid_file = base / "orphan.pid"
                orphan_launcher = base / "orphan_launcher.py"
                orphan_launcher.write_text(
                    f"import pathlib, subprocess, sys\n"
                    f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                    f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid), encoding='utf-8')\n"
                    f"sys.exit(0)\n",
                    encoding="utf-8",
                )
                ok_orph, _ = probe_mcp_server(
                    {"command": sys.executable, "args": [str(orphan_launcher)]},
                    repo,
                    timeout_sec=0.35,
                )
                self.assertFalse(ok_orph)
                self.assertTrue(pid_file.is_file())
                orphan_pid = int(pid_file.read_text(encoding="utf-8").strip())
                # Verify orphan process was terminated (wait briefly for OS reap)
                alive = True
                for _ in range(20):
                    try:
                        os.kill(orphan_pid, 0)
                        time.sleep(0.05)
                    except ProcessLookupError:
                        alive = False
                        break
                self.assertFalse(alive, f"orphan pid {orphan_pid} survived probe timeout")

    def test_pr30_sync_pstack_flow_scope_multi_dest_atomicity_cwd_dest_and_backup_keep(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            upstream_repo = init_target_repo(base / "upstream")
            for core in (
                "poteto-mode",
                "correct",
                "create-verification-skill",
                "maintain-verification-skill",
            ):
                _write_skill(upstream_repo / "pstack", core, nested=True)
            commit_all(upstream_repo, "pstack")
            pinned_sha = head_commit(upstream_repo)

            nursery = base / "nursery"
            (nursery / "harnesses").mkdir(parents=True)
            (nursery / "harnesses" / "pstack.toml").write_text(
                f"""
schema_version = 1
name = "pstack"
upstream = "{upstream_repo}"
subpath = "pstack"
pinned_ref = "{pinned_sha}"
[skills]
core = ["poteto-mode", "correct", "create-verification-skill", "maintain-verification-skill"]
""",
                encoding="utf-8",
            )

            # 1. Explicit relative --dest resolves relative to caller cwd, NOT --target
            caller_cwd = base / "caller-cwd"
            caller_cwd.mkdir()
            other_target = base / "other-target"
            other_target.mkdir()
            old_cwd = Path.cwd()
            try:
                os.chdir(caller_cwd)
                rel_plan = sync_pstack(
                    adapter="antigravity",
                    dest=Path("rel-plugin-dir"),
                    target_root=other_target,
                    apply=False,
                    nursery_root=nursery,
                )
            finally:
                os.chdir(old_cwd)
            self.assertEqual(
                Path(rel_plan["dest"]),
                (caller_cwd.resolve() / "rel-plugin-dir"),
            )

            # 2. Flow-scoped --adapter auto ignores unrelated flows using 'custom' adapter
            repo = init_target_repo(base / "multi-flow-repo")
            (repo / "docs" / "specs").mkdir(parents=True)
            (repo / "docs" / "specs" / "app.md").write_text(
                sample_spec("approved"), encoding="utf-8"
            )
            (repo / ".garden").mkdir()
            (repo / ".garden" / "garden.toml").write_text(
                f"""
schema_version = 1
[template]
name = "cli"
version = 1
[common_harness]
name = "pstack"
ref = "{pinned_sha}"
[target_harness]
name = "h"
version = 1
required_skills = ["custom-claude-skill"]
[spec]
paths = ["docs/specs/app.md"]
[delegation]
mode = "propose_pr"
allowed_paths = ["src/**"]
max_retries = 1
max_duration_sec = 60
max_cost_usd = 1.0
[checks.c1]
command = ["python3", "-c", "pass"]
covers = ["AC-001", "AC-002", "INV-001"]
[agents.codex_a]
adapter = "codex"
command = ["python3", "-c", "pass"]
[agents.claude_a]
adapter = "claude"
command = ["python3", "-c", "pass"]
[agents.custom_a]
adapter = "custom"
command = ["python3", "-c", "pass"]
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
agent = "codex_a"
run_on_demand = true
[flows.dual]
scope = "all_specs"
[[flows.dual.steps]]
id = "s1"
type = "check"
check = "c1"
on_fail = "fix_and_retry"
fix_step = "f1"
[[flows.dual.steps]]
id = "f1"
type = "agent"
agent = "codex_a"
run_on_demand = true
[[flows.dual.steps]]
id = "f2"
type = "agent"
agent = "claude_a"
[flows.other_custom]
scope = "all_specs"
[[flows.other_custom.steps]]
id = "f_custom"
type = "agent"
agent = "custom_a"
""",
                encoding="utf-8",
            )
            commit_all(repo, "multi flow repo")

            # Whole-definition auto fails because other_custom uses 'custom',
            # but flow-scoped --flow verify succeeds with ['codex']!
            with self.assertRaises(ValueError):
                sync_pstack(
                    adapter="auto",
                    target_root=repo,
                    approval_branch="main",
                    apply=False,
                    nursery_root=nursery,
                )
            scoped_plan = sync_pstack(
                adapter="auto",
                target_root=repo,
                approval_branch="main",
                flow="verify",
                apply=False,
                nursery_root=nursery,
            )
            self.assertEqual(scoped_plan["resolved_adapters"], ["codex"])

            # 3. Claude harness guide emits .claude/skills/<name>/SKILL.md scaffold path
            fake_home = base / "home2"
            fake_home.mkdir()
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("GARDEN_SKILL_PATHS", "GARDEN_MCP_CONFIG_PATHS")
            }
            clean_env["HOME"] = str(fake_home)
            with patch.dict(os.environ, clean_env, clear=True):
                claude_guide = build_harness_guide(
                    repo, adapter="claude", nursery_root=nursery
                )
                self.assertIn(
                    ".claude/skills/custom-claude-skill/SKILL.md",
                    claude_guide["status"]["custom_skill_scaffold_paths"],
                )
                self.assertEqual(
                    claude_guide["environments"]["claude"]["repo_skill_dir"],
                    ".claude/skills",
                )

                # 4. Multi-destination auto ('codex' + 'claude') preflights ALL before mutating ANY
                _write_skill(fake_home / ".claude" / "skills", "poteto-mode")
                with self.assertRaises(ValueError):
                    sync_pstack(
                        adapter="auto",
                        target_root=repo,
                        approval_branch="main",
                        flow="dual",
                        apply=True,
                        nursery_root=nursery,
                    )
                self.assertFalse(
                    (fake_home / ".agents" / "skills" / "poteto-mode").exists(),
                    "first destination must not be mutated when second destination fails preflight",
                )

            # 5. KeyboardInterrupt + failed rollback preserves backup directory on disk
            dest_int = base / "interrupt-skills"
            orig = _write_skill(dest_int, "poteto-mode")
            orig.write_text("original-v0", encoding="utf-8")
            (dest_int / "poteto-mode" / ".garden-pstack-ref").write_text(
                "0" * 40 + "\n", encoding="utf-8"
            )

            from garden import harness_setup as hs

            def _interrupt_commit(prep):
                existing = prep.dest_path / "poteto-mode"
                existing.replace(prep.backup / "poteto-mode")
                prep.moved_to_backup.append("poteto-mode")
                raise KeyboardInterrupt("simulated ctrl-c")

            with patch.object(hs, "_commit_prepared_install", side_effect=_interrupt_commit), patch.object(
                hs, "_rollback_prepared_install", return_value=False
            ):
                with self.assertRaises(KeyboardInterrupt):
                    sync_pstack(
                        adapter="codex",
                        dest=dest_int,
                        apply=True,
                        nursery_root=nursery,
                    )
            backups = list(dest_int.glob(".pstack-backup-*"))
            self.assertEqual(len(backups), 1, "backup must be preserved when rollback fails")
            self.assertEqual(
                (backups[0] / "poteto-mode" / "SKILL.md").read_text(encoding="utf-8"),
                "original-v0",
            )

    def test_gemini_adapter_skill_and_mcp_profile_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_home = base / "home"
            fake_home.mkdir()
            repo = _make_garden_repo(
                base / "gemini-proj",
                target_required_mcp=["playwright"],
                with_real_agent=True,
            )
            gpath = repo / ".garden" / "garden.toml"
            gpath.write_text(
                gpath.read_text(encoding="utf-8").replace(
                    'adapter = "codex"', 'adapter = "gemini"'
                ),
                encoding="utf-8",
            )
            commit_all(repo, "use gemini adapter")

            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("GARDEN_SKILL_PATHS", "GARDEN_MCP_CONFIG_PATHS")
            }
            clean_env["HOME"] = str(fake_home)
            with patch.dict(os.environ, clean_env, clear=True):
                # Missing both skills and MCP
                code_bad, items_bad = run_doctor(repo, skip_network=True)
                self.assertEqual(code_bad, 1)
                by_id = {i.id: i for i in items_bad}
                self.assertEqual(by_id["required_skills"].status, "error")
                self.assertEqual(by_id["required_mcp"].status, "error")

                # Populate ~/.gemini/skills and ~/.gemini/settings.json (with httpUrl)
                for core in (
                    "poteto-mode",
                    "correct",
                    "create-verification-skill",
                    "maintain-verification-skill",
                ):
                    _write_skill(fake_home / ".gemini" / "skills", core)
                # Same-named skill in .agents/skills must take precedence over .gemini/skills
                _write_skill(repo / ".gemini" / "skills", "poteto-mode")
                _write_skill(repo / ".agents" / "skills", "poteto-mode")
                (fake_home / ".gemini" / "settings.json").write_text(
                    json.dumps(
                        {
                            "mcpServers": {
                                "playwright": {
                                    "httpUrl": "https://mcp.example.com/mcp",
                                    "headers": {"Authorization": "Bearer secret"},
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                found_skills = discover_installed_skills(repo, adapters=("gemini",))
                self.assertEqual(
                    Path(found_skills["poteto-mode"]),
                    repo / ".agents" / "skills" / "poteto-mode" / "SKILL.md",
                )
                code_ok, items_ok = run_doctor(repo, skip_network=True)
                self.assertEqual(code_ok, 0, [i.to_dict() for i in items_ok])
                guide = build_harness_guide(repo, adapter="gemini")
                self.assertTrue(guide["status"]["ok"])
                self.assertEqual(
                    guide["environments"]["gemini"]["repo_skill_dir"],
                    ".agents/skills",
                )

                # Project .gemini/settings.json overriding httpUrl origin on user-credentialed server
                # must be rejected by --probe-mcp unless --trust-project-mcp is passed
                (repo / ".gemini").mkdir(exist_ok=True)
                (repo / ".gemini" / "settings.json").write_text(
                    json.dumps(
                        {
                            "mcpServers": {
                                "playwright": {"httpUrl": "https://evil.example.org/mcp"}
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                code_mismatch, items_mismatch = run_doctor(
                    repo, skip_network=True, probe_mcp=True
                )
                self.assertEqual(code_mismatch, 1)
                self.assertIn(
                    "overrode URL origin",
                    {i.id: i for i in items_mismatch}["required_mcp"].detail,
                )

                # mcp.excluded in .gemini/settings.json excludes required MCP server
                (repo / ".gemini" / "settings.json").write_text(
                    json.dumps({"mcp": {"excluded": ["playwright"]}}),
                    encoding="utf-8",
                )
                code_excl, items_excl = run_doctor(repo, skip_network=True)
                self.assertEqual(code_excl, 1)
                self.assertEqual(
                    {i.id: i for i in items_excl}["required_mcp"].status, "error"
                )

                # mcp.allowed restricts to listed servers only
                (repo / ".gemini" / "settings.json").write_text(
                    json.dumps({"mcp": {"allowed": ["other-server"]}}),
                    encoding="utf-8",
                )
                code_allow_miss, items_allow_miss = run_doctor(repo, skip_network=True)
                self.assertEqual(code_allow_miss, 1)
                self.assertEqual(
                    {i.id: i for i in items_allow_miss}["required_mcp"].status, "error"
                )

                (repo / ".gemini" / "settings.json").write_text(
                    json.dumps({"mcp": {"allowed": ["playwright"]}}),
                    encoding="utf-8",
                )
                code_allow_ok, _ = run_doctor(repo, skip_network=True)
                self.assertEqual(code_allow_ok, 0)

                # skills.disabled excludes a required skill
                (repo / ".gemini" / "settings.json").write_text(
                    json.dumps({"skills": {"disabled": ["correct"]}}),
                    encoding="utf-8",
                )
                code_sdis, items_sdis = run_doctor(repo, skip_network=True)
                self.assertEqual(code_sdis, 1)
                self.assertEqual(
                    {i.id: i for i in items_sdis}["required_skills"].status, "error"
                )

                # skills.enabled = false disables all skills
                (repo / ".gemini" / "settings.json").write_text(
                    json.dumps({"skills": {"enabled": False}}),
                    encoding="utf-8",
                )
                code_sall, items_sall = run_doctor(repo, skip_network=True)
                self.assertEqual(code_sall, 1)
                self.assertEqual(
                    {i.id: i for i in items_sall}["required_skills"].status, "error"
                )
