"""Tests for MVP-6 harness standardization (required_skills, required_mcp, and pstack core)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
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
                "print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': {'protocolVersion': '2024-11-05'}}), flush=True)\n",
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
                            "result": {"protocolVersion": "2024-11-05"},
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
                self.assertIn("[mcp_servers.playwright]", guide["environments"]["codex"]["mcp_snippet"])
                self.assertIn("@playwright/mcp@latest", guide["environments"]["antigravity"]["mcp_snippet"])
                md = format_harness_guide_markdown(guide)
                self.assertIn("garden harness sync-pstack --adapter codex --apply", md)

            # CLI smoke for `garden harness sync-pstack` dry-run
            proc = run_cli("harness", "sync-pstack", "--adapter", "codex", "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertFalse(out["applied"])
            self.assertEqual(out["adapter"], "codex")
