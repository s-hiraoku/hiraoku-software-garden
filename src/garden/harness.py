"""Harness requirement resolution, verification, and live MCP probing (MVP-6)."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import tomllib
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from garden.definition import FlowConfig, GardenDefinition
from garden.resources import nursery_root as resolve_nursery_root


DEFAULT_EXTRA_SKILL_DIRS: tuple[str, ...] = (
    ".agents/skills",
    ".cursor/skills",
    ".claude/skills",
    "~/.agents/skills",
    "~/.gemini/config/skills",
    "~/.cursor/skills",
    "~/.claude/skills",
)

# Per-adapter skill search roots (project-local + adapter user scope).
ADAPTER_SKILL_DIRS: dict[str, tuple[str, ...]] = {
    "codex": (
        ".agents/skills",
        "~/.agents/skills",
    ),
    "claude": (
        ".claude/skills",
        "~/.claude/skills",
    ),
    "antigravity": (
        ".agents/skills",
        "~/.gemini/config/plugins/pstack",
        "~/.gemini/config/skills",
    ),
    "cursor": (
        ".cursor/skills",
        ".agents/skills",
        "~/.cursor/plugins/pstack",
        "~/.cursor/skills",
    ),
}

ADAPTER_REPO_SKILL_DIR: dict[str, str] = {
    "codex": ".agents/skills",
    "claude": ".claude/skills",
    "antigravity": ".agents/skills",
    "cursor": ".cursor/skills",
}

# Per-adapter MCP configuration sources ordered from lowest to highest precedence.
# Format: (path_template, kind, trusted_origin)
#   kind in {"toml", "json", "claude_json_global", "claude_json_project", "schema_dir"}
#   trusted_origin is True for user-scope configs and False for project-local configs.
ADAPTER_MCP_PROFILES: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "codex": (
        ("~/.codex/config.toml", "toml", True),
        (".codex/config.toml", "toml", False),
    ),
    "claude": (
        ("~/.claude.json", "claude_json_global", True),
        (".mcp.json", "json", False),
        ("~/.claude.json", "claude_json_project", True),
    ),
    "antigravity": (
        ("~/.gemini/antigravity/mcp", "schema_dir", True),
        ("~/.gemini/config/mcp_config.json", "json", True),
        ("~/.gemini/antigravity/mcp_config.json", "json", True),
        (".mcp.json", "json", False),
    ),
    "cursor": (
        ("~/.cursor/mcp.json", "json", True),
        (".cursor/mcp.json", "json", False),
    ),
}

ADAPTER_MCP_HINTS: dict[str, str] = {
    "codex": ".codex/config.toml or ~/.codex/config.toml",
    "claude": ".mcp.json or ~/.claude.json",
    "antigravity": ".mcp.json, ~/.gemini/antigravity/mcp_config.json, or ~/.gemini/config/mcp_config.json",
    "cursor": ".cursor/mcp.json or ~/.cursor/mcp.json",
}

_AUTH_CONFIG_KEYS: tuple[str, ...] = (
    "headers",
    "http_headers",
    "env_http_headers",
    "bearer_token_env_var",
)
_HTTP_MAX_BODY_BYTES = 262144
_SSE_MAX_EVENT_BYTES = 65536
_SSE_MAX_TOTAL_BYTES = 262144


@dataclass(frozen=True)
class _McpEntry:
    source: str
    cfg: dict[str, Any] | None
    schema_dir: str | None = None
    trusted_origin: bool = False
    auth_trusted: bool = True
    auth_origin: tuple[str, str, int] | None = None
    inherited_auth_origin_mismatch: bool = False


@dataclass(frozen=True)
class HarnessCheckResult:
    required_skills: tuple[str, ...]
    missing_skills: tuple[str, ...]
    found_skills: dict[str, str]
    core_skills: tuple[str, ...]
    missing_core_skills: tuple[str, ...]
    core_enforced: bool
    required_mcp: tuple[str, ...]
    missing_mcp: tuple[str, ...]
    found_mcp: dict[str, str]
    remedy_hints: tuple[str, ...]
    manifest_error: str | None = None
    mcp_probe_errors: dict[str, str] = field(default_factory=dict)

    @property
    def has_blocking_missing(self) -> bool:
        return bool(
            self.missing_skills
            or self.missing_mcp
            or self.mcp_probe_errors
            or (self.core_enforced and self.manifest_error)
        )


def load_pstack_manifest(nursery_root: Path | None = None) -> dict[str, Any] | None:
    n_root = nursery_root or resolve_nursery_root()
    manifest = n_root / "harnesses" / "pstack.toml"
    if not manifest.is_file():
        return None
    try:
        with manifest.open("rb") as fh:
            data = tomllib.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _dedupe(items: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _expand_path(raw: str, target_root: Path) -> Path:
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (target_root / p)


def _is_within(path: Path, root: Path) -> bool:
    try:
        r_path = path.resolve()
        r_root = root.resolve()
        return r_path == r_root or r_root in r_path.parents
    except OSError:
        return False


def _origin_tuple(url: str) -> tuple[str, str, int]:
    parsed = urllib_parse.urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if parsed.port is not None:
        port = parsed.port
    else:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


def _cfg_url(cfg: dict[str, Any] | None) -> str | None:
    if not isinstance(cfg, dict):
        return None
    for key in ("url", "serverUrl"):
        val = cfg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _cfg_has_auth(cfg: dict[str, Any] | None) -> bool:
    if not isinstance(cfg, dict):
        return False
    for key in _AUTH_CONFIG_KEYS:
        val = cfg.get(key)
        if isinstance(val, dict) and val:
            return True
        if isinstance(val, str) and val.strip():
            return True
    return False


def _scan_skill_roots(roots: list[Path]) -> dict[str, str]:
    found: dict[str, str] = {}
    for base in roots:
        if not base.is_dir():
            continue
        candidate_dirs = [base]
        nested_skills = base / "skills"
        if nested_skills.is_dir():
            candidate_dirs.append(nested_skills)
        for skill_root in candidate_dirs:
            try:
                children = sorted(skill_root.iterdir())
            except OSError:
                continue
            for child in children:
                if not child.is_dir():
                    continue
                skill_md = child / "SKILL.md"
                if skill_md.is_file() and child.name not in found:
                    found[child.name] = str(skill_md)
    return found


def _skill_search_roots(
    target_root: Path,
    manifest: dict[str, Any] | None,
    *,
    raw_dirs: tuple[str, ...] | None = None,
) -> list[Path]:
    env_override = os.environ.get("GARDEN_SKILL_PATHS")
    if env_override is not None:
        raw_entries = [p.strip() for p in env_override.split(os.pathsep) if p.strip()]
        return [_expand_path(p, target_root) for p in raw_entries]

    raw_paths: list[str] = []
    if raw_dirs is not None:
        raw_paths.extend(raw_dirs)
    else:
        if isinstance(manifest, dict):
            res = manifest.get("resolution")
            if isinstance(res, dict) and isinstance(res.get("search_paths"), list):
                raw_paths.extend(str(p) for p in res["search_paths"] if str(p).strip())
        raw_paths.extend(DEFAULT_EXTRA_SKILL_DIRS)

    roots: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_paths:
        resolved = _expand_path(raw, target_root)
        if resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)
    return roots


def discover_installed_skills(
    target_root: Path,
    manifest: dict[str, Any] | None = None,
    *,
    adapters: tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Scan configured skill directories and return {skill_name: SKILL.md path}.

    When `adapters` includes profile-specific real adapters (codex, claude,
    antigravity, cursor) and `GARDEN_SKILL_PATHS` is not set, a skill is
    returned only if it is discoverable by all selected adapters.
    """
    if os.environ.get("GARDEN_SKILL_PATHS") is not None:
        return _scan_skill_roots(_skill_search_roots(target_root, manifest))

    scoped_adapters = _dedupe(
        [a for a in (adapters or ()) if a in ADAPTER_SKILL_DIRS]
    )
    if scoped_adapters:
        per_adapter = [
            _scan_skill_roots(
                _skill_search_roots(
                    target_root, manifest, raw_dirs=ADAPTER_SKILL_DIRS[adapter]
                )
            )
            for adapter in scoped_adapters
        ]
        common_names = set(per_adapter[0].keys())
        for mapping in per_adapter[1:]:
            common_names &= set(mapping.keys())
        return {
            name: ", ".join(_dedupe([mapping[name] for mapping in per_adapter]))
            for name in sorted(common_names)
        }

    return _scan_skill_roots(_skill_search_roots(target_root, manifest))


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_valid_mcp_server_config(cfg: Any) -> bool:
    if not isinstance(cfg, dict):
        return False
    return any(_is_non_empty_str(cfg.get(key)) for key in ("command", "url", "serverUrl"))


def _is_disabled_mcp_server_config(cfg: Any) -> bool:
    return isinstance(cfg, dict) and (
        cfg.get("disabled") is True or cfg.get("enabled") is False
    )


def _apply_mcp_mapping(
    state: dict[str, _McpEntry],
    mapping: Any,
    source: str,
    *,
    trusted_origin: bool,
) -> None:
    if not isinstance(mapping, dict):
        return
    for raw_name, cfg in mapping.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        name = raw_name.strip()
        if not isinstance(cfg, dict):
            state[name] = _McpEntry(
                source=source,
                cfg=None,
                schema_dir=None,
                trusted_origin=trusted_origin,
                auth_trusted=trusted_origin,
                auth_origin=None,
                inherited_auth_origin_mismatch=False,
            )
            continue

        incoming_has_auth = _cfg_has_auth(cfg)
        incoming_url = _cfg_url(cfg)
        prev = state.get(name)
        if prev is not None and isinstance(prev.cfg, dict):
            merged = {**prev.cfg, **cfg}
            merged_trusted = prev.trusted_origin and trusted_origin
            prev_schema = (
                prev.schema_dir if not _is_valid_mcp_server_config(merged) else None
            )
            prev_has_auth = _cfg_has_auth(prev.cfg)

            if incoming_has_auth:
                merged_auth_trusted = (
                    (prev.auth_trusted and trusted_origin)
                    if prev_has_auth
                    else trusted_origin
                )
            else:
                merged_auth_trusted = prev.auth_trusted if prev_has_auth else True

            merged_url = _cfg_url(merged)
            merged_origin = _origin_tuple(merged_url) if merged_url else None

            if incoming_has_auth and trusted_origin and merged_origin is not None:
                auth_origin = merged_origin
                origin_mismatch = False
            elif prev_has_auth and prev.auth_origin is not None:
                auth_origin = prev.auth_origin
                origin_mismatch = (
                    incoming_url is not None
                    and merged_origin is not None
                    and merged_origin != prev.auth_origin
                ) or prev.inherited_auth_origin_mismatch
            else:
                auth_origin = (
                    merged_origin if (incoming_has_auth and trusted_origin) else None
                )
                origin_mismatch = prev.inherited_auth_origin_mismatch
        else:
            merged = dict(cfg)
            merged_trusted = trusted_origin
            prev_schema = None
            merged_auth_trusted = trusted_origin if incoming_has_auth else True
            merged_url = _cfg_url(merged)
            auth_origin = (
                _origin_tuple(merged_url)
                if (incoming_has_auth and trusted_origin and merged_url)
                else None
            )
            origin_mismatch = False

        state[name] = _McpEntry(
            source=source,
            cfg=merged,
            schema_dir=prev_schema,
            trusted_origin=merged_trusted,
            auth_trusted=merged_auth_trusted,
            auth_origin=auth_origin,
            inherited_auth_origin_mismatch=origin_mismatch,
        )


def _finalize_mcp_entries(state: dict[str, _McpEntry]) -> dict[str, _McpEntry]:
    active: dict[str, _McpEntry] = {}
    for name, entry in state.items():
        if _is_disabled_mcp_server_config(entry.cfg):
            continue
        if _is_valid_mcp_server_config(entry.cfg) or entry.schema_dir is not None:
            active[name] = entry
    return active


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_toml_dict(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _matching_claude_projects(
    data: dict[str, Any], target_root: Path
) -> list[dict[str, Any]]:
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return []
    try:
        resolved_target = target_root.resolve()
    except OSError:
        resolved_target = target_root
    target_key = str(resolved_target)
    matched: list[dict[str, Any]] = []
    for proj_key, proj_val in projects.items():
        if not isinstance(proj_key, str) or not isinstance(proj_val, dict):
            continue
        if proj_key == target_key:
            matched.append(proj_val)
            continue
        try:
            if Path(proj_key).expanduser().resolve() == resolved_target:
                matched.append(proj_val)
        except (OSError, ValueError):
            continue
    return matched


def _apply_mcp_source(
    state: dict[str, _McpEntry],
    path: Path,
    kind: str,
    target_root: Path,
    *,
    trusted_origin: bool,
) -> None:
    if kind == "schema_dir":
        if not path.is_dir():
            return
        try:
            for child in sorted(path.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    state.setdefault(
                        child.name,
                        _McpEntry(
                            source=str(child),
                            cfg=None,
                            schema_dir=str(child),
                            trusted_origin=trusted_origin,
                        ),
                    )
        except OSError:
            pass
        return

    if not path.is_file():
        return

    if kind == "toml":
        data = _read_toml_dict(path)
        if data is not None:
            _apply_mcp_mapping(
                state,
                data.get("mcp_servers"),
                str(path),
                trusted_origin=trusted_origin,
            )
        return

    data = _read_json_dict(path)
    if data is None:
        return

    if kind == "json":
        _apply_mcp_mapping(
            state, data.get("mcpServers"), str(path), trusted_origin=trusted_origin
        )
        for proj_val in _matching_claude_projects(data, target_root):
            _apply_mcp_mapping(
                state,
                proj_val.get("mcpServers"),
                str(path),
                trusted_origin=trusted_origin,
            )
    elif kind == "claude_json_global":
        _apply_mcp_mapping(
            state, data.get("mcpServers"), str(path), trusted_origin=trusted_origin
        )
    elif kind == "claude_json_project":
        for proj_val in _matching_claude_projects(data, target_root):
            disabled_list = proj_val.get("disabledMcpjsonServers")
            if isinstance(disabled_list, list):
                for raw_name in disabled_list:
                    if isinstance(raw_name, str) and raw_name.strip():
                        state[raw_name.strip()] = _McpEntry(
                            source=str(path),
                            cfg={"disabled": True},
                            schema_dir=None,
                            trusted_origin=trusted_origin,
                        )
            _apply_mcp_mapping(
                state,
                proj_val.get("mcpServers"),
                str(path),
                trusted_origin=trusted_origin,
            )


def _discover_mcp_entries_for_profile(
    target_root: Path, profile_sources: tuple[tuple[str, str, bool], ...]
) -> dict[str, _McpEntry]:
    state: dict[str, _McpEntry] = {}
    for raw_path, kind, trusted in profile_sources:
        _apply_mcp_source(
            state,
            _expand_path(raw_path, target_root),
            kind,
            target_root,
            trusted_origin=trusted,
        )
    return _finalize_mcp_entries(state)


def _discover_mcp_entries(
    target_root: Path,
    *,
    adapters: tuple[str, ...] | None = None,
) -> dict[str, tuple[str, list[_McpEntry]]]:
    env_override = os.environ.get("GARDEN_MCP_CONFIG_PATHS")
    if env_override is not None:
        state: dict[str, _McpEntry] = {}
        raw_paths = [p.strip() for p in env_override.split(os.pathsep) if p.strip()]
        for raw in raw_paths:
            p = _expand_path(raw, target_root)
            trusted = not _is_within(p, target_root)
            if p.is_file():
                kind = "toml" if p.suffix.lower() == ".toml" else "json"
                _apply_mcp_source(state, p, kind, target_root, trusted_origin=trusted)
            elif p.is_dir():
                _apply_mcp_source(
                    state, p, "schema_dir", target_root, trusted_origin=trusted
                )
        return {
            name: (entry.source, [entry])
            for name, entry in _finalize_mcp_entries(state).items()
        }

    scoped_adapters = _dedupe(
        [a for a in (adapters or ()) if a in ADAPTER_MCP_PROFILES]
    )
    if scoped_adapters:
        per_adapter = [
            _discover_mcp_entries_for_profile(target_root, ADAPTER_MCP_PROFILES[adapter])
            for adapter in scoped_adapters
        ]
        common_names = set(per_adapter[0].keys())
        for mapping in per_adapter[1:]:
            common_names &= set(mapping.keys())
        out: dict[str, tuple[str, list[_McpEntry]]] = {}
        for name in sorted(common_names):
            sources = _dedupe([mapping[name].source for mapping in per_adapter])
            entries = [mapping[name] for mapping in per_adapter]
            out[name] = (", ".join(sources), entries)
        return out

    found: dict[str, tuple[str, list[_McpEntry]]] = {}
    for profile_sources in ADAPTER_MCP_PROFILES.values():
        for name, entry in _discover_mcp_entries_for_profile(
            target_root, profile_sources
        ).items():
            if name not in found:
                found[name] = (entry.source, [entry])
    return found


def discover_configured_mcp_servers(
    target_root: Path,
    *,
    adapters: tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Scan MCP configuration files and return {server_name: source_path}."""
    return {
        name: source
        for name, (source, _) in _discover_mcp_entries(
            target_root, adapters=adapters
        ).items()
    }


_MCP_INITIALIZE_PAYLOAD = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "garden-doctor", "version": "0.1.0"},
        },
    }
)


def _check_jsonrpc_object(obj: Any) -> tuple[bool, str | None]:
    if not isinstance(obj, dict):
        return False, None
    if obj.get("id") != 1:
        return False, None
    if obj.get("jsonrpc") != "2.0":
        return False, "invalid JSON-RPC response (expected jsonrpc='2.0')"
    if "error" in obj and obj["error"] is not None:
        err = obj["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return False, f"initialize error: {msg}"
    if "result" in obj:
        res = obj["result"]
        if not isinstance(res, dict):
            return False, "invalid MCP initialize result (expected object)"
        if not _is_non_empty_str(res.get("protocolVersion")):
            return False, "invalid MCP initialize result (missing protocolVersion)"
        if not isinstance(res.get("capabilities"), dict):
            return False, "invalid MCP initialize result (missing capabilities object)"
        server_info = res.get("serverInfo")
        if not isinstance(server_info, dict) or not _is_non_empty_str(
            server_info.get("name")
        ):
            return False, "invalid MCP initialize result (missing serverInfo.name)"
        return True, None
    return False, None


def _extract_jsonrpc_init_outcome(text: str) -> tuple[bool, str | None]:
    """Parse either a complete JSON document or newline-delimited JSON / SSE lines."""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            ok, err_msg = _check_jsonrpc_object(json.loads(stripped))
            if ok or err_msg is not None:
                return ok, err_msg
        except json.JSONDecodeError:
            pass

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ok, err_msg = _check_jsonrpc_object(obj)
        if ok or err_msg is not None:
            return ok, err_msg
    return False, None


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        # Always reclaim the entire session/process group even if the parent
        # launcher already exited while descendants still hold stdio pipes.
        pgid = proc.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
        wait_deadline = time.monotonic() + 0.5
        while time.monotonic() < wait_deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                break
            except OSError:
                break
            time.sleep(0.02)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    elif proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
    if proc.poll() is None:
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


def _is_bare_command(cmd: str) -> bool:
    if cmd.startswith("~"):
        return False
    if os.sep in cmd:
        return False
    if os.altsep and os.altsep in cmd:
        return False
    return True


def _probe_stdio_mcp(
    cfg: dict[str, Any],
    target_root: Path,
    timeout_sec: float,
    *,
    trust_project_mcp: bool = False,
) -> tuple[bool, str]:
    env = os.environ.copy()
    raw_env = cfg.get("env")
    if isinstance(raw_env, dict):
        for k, v in raw_env.items():
            if isinstance(k, str) and isinstance(v, str):
                env[k] = v

    cmd = str(cfg["command"]).strip()
    resolved_path: Path | None = None
    if _is_bare_command(cmd):
        found_bin = shutil.which(cmd, path=env.get("PATH"))
        if found_bin is not None:
            resolved_path = Path(found_bin).resolve()
    else:
        candidate = _expand_path(cmd, target_root)
        if candidate.is_file():
            resolved_path = candidate.resolve()

    if resolved_path is None:
        return False, f"executable not found: {cmd}"

    if _is_within(resolved_path, target_root) and not trust_project_mcp:
        return (
            False,
            f"stdio MCP executable {resolved_path} is inside target checkout; review it and pass --trust-project-mcp to execute",
        )

    raw_args = [] if cfg.get("args") is None else cfg.get("args")
    if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
        return False, "invalid args (expected list of strings)"

    popen_kwargs: dict[str, Any] = {
        "cwd": str(target_root),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "env": env,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen([str(resolved_path), *raw_args], **popen_kwargs)
    except OSError as exc:
        return False, f"failed to launch {cmd}: {exc}"

    try:
        stdout, stderr = proc.communicate(
            input=_MCP_INITIALIZE_PAYLOAD + "\n", timeout=timeout_sec
        )
    except subprocess.TimeoutExpired as exc:
        captured_out = exc.output
        if isinstance(captured_out, bytes):
            captured_text = captured_out.decode("utf-8", errors="replace")
        else:
            captured_text = captured_out or ""
        _terminate_process_tree(proc)
        drained_out = ""
        try:
            drained_out, _ = proc.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        combined = captured_text + (drained_out or "")
        ok, err_msg = _extract_jsonrpc_init_outcome(combined)
        if ok:
            return True, "stdio initialize ok"
        return (
            False,
            err_msg
            or f"timed out after {timeout_sec:.1f}s waiting for initialize response",
        )

    ok, err_msg = _extract_jsonrpc_init_outcome(stdout or "")
    if ok:
        return True, "stdio initialize ok"
    if err_msg:
        return False, err_msg
    detail = (stderr or "").strip().splitlines()
    hint = detail[0][:120] if detail else f"exit code {proc.returncode}"
    return False, f"no valid MCP initialize response ({hint})"


class _UnsafeRedirectError(urllib_error.URLError):
    """Raised when an HTTP redirect violates scheme or downgrade safety."""


class _SafeMcpRedirectHandler(urllib_request.HTTPRedirectHandler):
    def __init__(self, sensitive_headers: set[str]) -> None:
        super().__init__()
        self._sensitive_headers = {h.lower() for h in sensitive_headers}

    def redirect_request(
        self,
        req: urllib_request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib_request.Request | None:
        old_origin = _origin_tuple(req.full_url)
        new_parsed = urllib_parse.urlparse(newurl)
        if new_parsed.scheme.lower() not in ("http", "https") or not new_parsed.netloc:
            raise _UnsafeRedirectError(f"refused redirect to unsafe URL {newurl!r}")
        new_origin = _origin_tuple(newurl)
        if old_origin[0] == "https" and new_origin[0] != "https":
            raise _UnsafeRedirectError(
                f"refused HTTPS-to-HTTP redirect downgrade to {newurl!r}"
            )
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        if new_origin != old_origin:
            for hdr_dict in (redirected.headers, redirected.unredirected_hdrs):
                for key in list(hdr_dict.keys()):
                    if key.lower() in self._sensitive_headers:
                        hdr_dict.pop(key, None)
        return redirected


def _build_http_headers(
    cfg: dict[str, Any],
) -> tuple[dict[str, str], set[str], str | None]:
    headers: dict[str, str] = {}
    sensitive = {"authorization", "proxy-authorization", "cookie"}

    for key_name in ("headers", "http_headers"):
        raw = cfg.get(key_name)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(k, str) and isinstance(v, str) and k.strip():
                    headers[k.strip()] = v
                    sensitive.add(k.strip().lower())

    env_headers = cfg.get("env_http_headers")
    if isinstance(env_headers, dict):
        for h_name, env_var in env_headers.items():
            if not isinstance(h_name, str) or not isinstance(env_var, str):
                continue
            val = os.environ.get(env_var)
            if not val:
                return (
                    {},
                    sensitive,
                    f"missing environment variable {env_var!r} for HTTP header {h_name!r}",
                )
            headers[h_name.strip()] = val
            sensitive.add(h_name.strip().lower())

    bearer_env = cfg.get("bearer_token_env_var")
    if isinstance(bearer_env, str) and bearer_env.strip():
        token = os.environ.get(bearer_env.strip())
        if not token:
            return (
                {},
                sensitive,
                f"missing environment variable {bearer_env.strip()!r} for bearer_token_env_var",
            )
        headers["Authorization"] = f"Bearer {token}"
        sensitive.add("authorization")

    return headers, sensitive, None


def _set_resp_socket_timeout(resp: Any, remaining: float) -> None:
    try:
        fp = getattr(resp, "fp", None)
        raw = getattr(fp, "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(max(0.05, remaining))
    except Exception:
        pass


def _read_resp_chunk(resp: Any, size: int, remaining: float) -> bytes:
    _set_resp_socket_timeout(resp, remaining)
    read1 = getattr(resp, "read1", None)
    if callable(read1):
        return bytes(read1(size))
    return bytes(resp.read(size))


def _read_http_body_bounded(
    resp: Any, deadline: float, *, max_bytes: int = _HTTP_MAX_BODY_BYTES
) -> tuple[str | None, str | None]:
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "timed out reading HTTP response body"
        to_read = min(4096, max_bytes - total + 1)
        try:
            chunk = _read_resp_chunk(resp, to_read, remaining)
        except (TimeoutError, OSError) as exc:
            return None, f"HTTP read failed: {exc}"
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            return None, f"HTTP response exceeded {max_bytes} byte limit"
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace"), None


def _read_sse_events(
    resp: Any,
    deadline: float,
    *,
    expect_event: str | None = None,
    max_event_bytes: int = _SSE_MAX_EVENT_BYTES,
    max_total_bytes: int = _SSE_MAX_TOTAL_BYTES,
    carry_buf: bytearray | None = None,
) -> tuple[str | None, tuple[bool, str | None] | None, str | None]:
    """Incrementally read SSE events within overall `deadline` and size caps.

    Returns (endpoint_data, init_outcome, error_reason).
    """
    buf = carry_buf if carry_buf is not None else bytearray()
    current_event: str | None = None
    data_lines: list[str] = []
    event_bytes = 0
    total_bytes = 0

    def _dispatch() -> tuple[str | None, tuple[bool, str | None] | None]:
        nonlocal current_event, data_lines, event_bytes
        if not data_lines and current_event is None:
            return None, None
        ev_type = (current_event or "message").strip()
        payload = "\n".join(data_lines).strip()
        current_event = None
        data_lines = []
        event_bytes = 0
        if expect_event is not None and ev_type == expect_event and payload:
            return payload, None
        if payload:
            ok, err_msg = _extract_jsonrpc_init_outcome(payload)
            if ok or err_msg is not None:
                return None, (ok, err_msg)
        return None, None

    def _process_line(
        line: str,
    ) -> tuple[str | None, tuple[bool, str | None] | None, str | None]:
        nonlocal current_event, event_bytes
        if line == "":
            ep, init_res = _dispatch()
            return ep, init_res, None
        if line.startswith(":"):
            return None, None, None
        event_bytes += len(line.encode("utf-8"))
        if event_bytes > max_event_bytes:
            return None, None, f"SSE event exceeded {max_event_bytes} byte limit"
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            val = line[5:]
            if val.startswith(" "):
                val = val[1:]
            data_lines.append(val)
        return None, None, None

    while True:
        while True:
            nl_idx = buf.find(b"\n")
            if nl_idx == -1:
                break
            raw_line = bytes(buf[:nl_idx])
            del buf[: nl_idx + 1]
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
            ep, init_res, cap_err = _process_line(line)
            if cap_err is not None:
                return None, None, cap_err
            if ep is not None or init_res is not None:
                return ep, init_res, None

        if len(buf) > max_event_bytes:
            return None, None, f"SSE line exceeded {max_event_bytes} byte limit"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, None, "timed out reading SSE stream"
        try:
            chunk = _read_resp_chunk(resp, 4096, remaining)
        except (TimeoutError, OSError) as exc:
            return None, None, f"SSE read failed: {exc}"
        if not chunk:
            if buf:
                tail = bytes(buf).decode("utf-8", errors="replace").rstrip("\r")
                buf.clear()
                ep, init_res, cap_err = _process_line(tail)
                if cap_err is not None:
                    return None, None, cap_err
                if ep is not None or init_res is not None:
                    return ep, init_res, None
            ep, init_res = _dispatch()
            if ep is not None or init_res is not None:
                return ep, init_res, None
            return None, None, "SSE stream closed before expected event arrived"
        total_bytes += len(chunk)
        if total_bytes > max_total_bytes:
            return None, None, f"SSE stream exceeded {max_total_bytes} byte limit"
        buf.extend(chunk)


def _probe_legacy_sse_mcp(
    url: str,
    opener: urllib_request.OpenerDirector,
    base_headers: dict[str, str],
    sensitive_headers: set[str],
    deadline: float,
) -> tuple[bool, str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False, "timed out before legacy SSE handshake"

    initial_origin = _origin_tuple(url)
    get_headers = {
        k: v for k, v in base_headers.items() if k.lower() != "content-type"
    }
    get_headers["Accept"] = "text/event-stream"
    get_req = urllib_request.Request(url, headers=get_headers, method="GET")
    try:
        resp = opener.open(get_req, timeout=max(0.1, remaining))
    except urllib_error.HTTPError as exc:
        code = exc.code
        exc.close()
        if code in (401, 403):
            return False, f"authentication required (HTTP {code})"
        return False, f"HTTP error {code}"
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        return False, f"unreachable: {exc}"

    try:
        status = getattr(resp, "status", 200)
        if not (200 <= status < 300):
            return False, f"unexpected HTTP status {status}"
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/event-stream" not in ctype:
            return (
                False,
                f"GET fallback returned {ctype or 'unknown content-type'} instead of text/event-stream",
            )
        final_sse_url = resp.geturl() if hasattr(resp, "geturl") else url
        sse_carry = bytearray()
        endpoint_raw, _, sse_err = _read_sse_events(
            resp, deadline, expect_event="endpoint", carry_buf=sse_carry
        )
        if endpoint_raw is None:
            return False, sse_err or "legacy SSE stream did not emit an endpoint event"

        msg_url = urllib_parse.urljoin(final_sse_url, endpoint_raw)
        if _origin_tuple(msg_url) != _origin_tuple(final_sse_url):
            return (
                False,
                f"refused cross-origin legacy SSE message endpoint {msg_url!r}",
            )

        post_rem = deadline - time.monotonic()
        if post_rem <= 0:
            return False, "timed out before legacy SSE initialize POST"
        # Never restore auth/configured sensitive headers if GET redirected away from initial_origin.
        if _origin_tuple(msg_url) == initial_origin:
            post_headers = dict(base_headers)
        else:
            post_headers = {
                k: v
                for k, v in base_headers.items()
                if k.lower() not in sensitive_headers
            }
        post_headers["Content-Type"] = "application/json"
        post_headers["Accept"] = "application/json, text/event-stream"
        msg_req = urllib_request.Request(
            msg_url,
            data=_MCP_INITIALIZE_PAYLOAD.encode("utf-8"),
            headers=post_headers,
            method="POST",
        )
        try:
            with opener.open(msg_req, timeout=max(0.1, post_rem)) as msg_resp:
                msg_status = getattr(msg_resp, "status", 200)
                msg_ctype = (msg_resp.headers.get("Content-Type") or "").lower()
                if msg_status == 200 and "application/json" in msg_ctype:
                    body, body_err = _read_http_body_bounded(msg_resp, deadline)
                    if body_err is not None:
                        return False, body_err
                    ok, err_msg = _extract_jsonrpc_init_outcome(body or "")
                    if ok:
                        return True, "legacy sse initialize ok"
                    if err_msg:
                        return False, err_msg
                elif not (200 <= msg_status < 300):
                    return False, f"legacy SSE message endpoint returned HTTP {msg_status}"
        except urllib_error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (401, 403):
                return False, f"authentication required (HTTP {code})"
            return False, f"legacy SSE message endpoint HTTP error {code}"
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            return False, f"legacy SSE message endpoint unreachable: {exc}"

        _, init_res, wait_err = _read_sse_events(
            resp, deadline, carry_buf=sse_carry
        )
        if init_res is not None:
            ok, err_msg = init_res
            if ok:
                return True, "legacy sse initialize ok"
            return False, err_msg or "legacy SSE initialize failed"
        return False, wait_err or "no MCP initialize response received on legacy SSE stream"
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _probe_http_mcp(cfg: dict[str, Any], timeout_sec: float) -> tuple[bool, str]:
    url = str(cfg.get("url") or cfg.get("serverUrl") or "").strip()
    parsed = urllib_parse.urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return False, f"unsupported MCP URL scheme or host: {url!r}"

    auth_headers, sensitive_headers, auth_err = _build_http_headers(cfg)
    if auth_err is not None:
        return False, auth_err

    opener = urllib_request.build_opener(_SafeMcpRedirectHandler(sensitive_headers))
    deadline = time.monotonic() + timeout_sec

    post_headers = dict(auth_headers)
    post_headers["Content-Type"] = "application/json"
    post_headers["Accept"] = "application/json, text/event-stream"

    req = urllib_request.Request(
        url,
        data=_MCP_INITIALIZE_PAYLOAD.encode("utf-8"),
        headers=post_headers,
        method="POST",
    )
    try:
        with opener.open(req, timeout=max(0.1, deadline - time.monotonic())) as resp:
            status = getattr(resp, "status", 200)
            if not (200 <= status < 300):
                return False, f"unexpected HTTP status {status}"
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in ctype:
                _, init_res, sse_err = _read_sse_events(resp, deadline)
                if init_res is not None:
                    ok, err_msg = init_res
                    if ok:
                        return True, f"http sse initialize ok (HTTP {status})"
                    return False, err_msg or "http sse initialize failed"
                return False, sse_err or "SSE stream did not return MCP initialize result"
            body, body_err = _read_http_body_bounded(resp, deadline)
            if body_err is not None:
                return False, body_err
            ok, err_msg = _extract_jsonrpc_init_outcome(body or "")
            if ok:
                return True, f"http initialize ok (HTTP {status})"
            if err_msg:
                return False, err_msg
            return False, f"HTTP {status} did not return a valid MCP initialize result"
    except urllib_error.HTTPError as exc:
        code = exc.code
        exc.close()
        if code in (401, 403):
            return False, f"authentication required (HTTP {code})"
        if code in (400, 404, 405):
            return _probe_legacy_sse_mcp(
                url, opener, auth_headers, sensitive_headers, deadline
            )
        return False, f"HTTP error {code}"
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        return False, f"unreachable: {exc}"


def _probe_mcp_entry(
    entry: _McpEntry,
    target_root: Path,
    *,
    timeout_sec: float = 5.0,
    trust_project_mcp: bool = False,
) -> tuple[bool, str]:
    if entry.cfg is None:
        if entry.schema_dir is not None:
            return (
                False,
                "schema directory only; configure explicit command or url in mcp_config.json to probe",
            )
        return False, "no command or url configured"
    if _is_non_empty_str(entry.cfg.get("command")):
        if not entry.trusted_origin and not trust_project_mcp:
            return (
                False,
                f"untrusted project-scoped stdio MCP command in {entry.source}; review it and pass --trust-project-mcp to execute",
            )
        return _probe_stdio_mcp(
            entry.cfg,
            target_root,
            timeout_sec,
            trust_project_mcp=trust_project_mcp,
        )
    if _is_non_empty_str(entry.cfg.get("url")) or _is_non_empty_str(
        entry.cfg.get("serverUrl")
    ):
        has_auth = _cfg_has_auth(entry.cfg)
        if has_auth and not trust_project_mcp:
            if entry.inherited_auth_origin_mismatch:
                return (
                    False,
                    f"project-scoped MCP config ({entry.source}) overrode URL origin for a server inheriting user credentials; review it and pass --trust-project-mcp to probe",
                )
            if not entry.auth_trusted or not entry.trusted_origin:
                return (
                    False,
                    f"untrusted project-scoped HTTP MCP auth/header config in {entry.source}; review it and pass --trust-project-mcp to probe",
                )
        return _probe_http_mcp(entry.cfg, timeout_sec)
    return False, "no command or url configured"


def probe_mcp_server(
    cfg: dict[str, Any], target_root: Path, *, timeout_sec: float = 5.0
) -> tuple[bool, str]:
    """Perform a live connectivity/handshake probe for one configured MCP server."""
    return _probe_mcp_entry(
        _McpEntry(
            source="<inline>",
            cfg=cfg,
            trusted_origin=True,
            auth_trusted=True,
        ),
        target_root,
        timeout_sec=timeout_sec,
        trust_project_mcp=True,
    )


def flow_real_adapters(definition: GardenDefinition, flow: FlowConfig) -> tuple[str, ...]:
    adapters: list[str] = []
    for step in flow.steps:
        if step.type != "agent" or not step.agent:
            continue
        agent_cfg = definition.agents.get(step.agent)
        if agent_cfg is not None and agent_cfg.adapter != "mock":
            adapters.append(agent_cfg.adapter)
    return _dedupe(adapters)


def definition_real_adapters(definition: GardenDefinition) -> tuple[str, ...]:
    adapters: list[str] = []
    for flow in definition.flows.values():
        adapters.extend(flow_real_adapters(definition, flow))
    return _dedupe(adapters)


def flow_uses_real_agent(definition: GardenDefinition, flow: FlowConfig) -> bool:
    return bool(flow_real_adapters(definition, flow))


def definition_uses_real_agent(definition: GardenDefinition) -> bool:
    return bool(definition_real_adapters(definition))


def evaluate_harness_requirements(
    target_root: Path,
    definition: GardenDefinition,
    *,
    nursery_root: Path | None = None,
    enforce_core: bool = False,
    include_advisory_core: bool = False,
    adapters: tuple[str, ...] | None = None,
    probe_mcp: bool = False,
    probe_timeout_sec: float = 5.0,
    trust_project_mcp: bool = False,
) -> HarnessCheckResult:
    """Verify declared required_skills, required_mcp, and (when enforced) pstack core skills."""
    explicit_skills = _dedupe(
        list(definition.common_harness.required_skills)
        + list(definition.target_harness.required_skills)
    )
    required_mcp = _dedupe(list(definition.target_harness.required_mcp))

    if (
        not enforce_core
        and not include_advisory_core
        and not explicit_skills
        and not required_mcp
    ):
        return HarnessCheckResult(
            required_skills=(),
            missing_skills=(),
            found_skills={},
            core_skills=(),
            missing_core_skills=(),
            core_enforced=False,
            required_mcp=(),
            missing_mcp=(),
            found_mcp={},
            remedy_hints=(),
            manifest_error=None,
            mcp_probe_errors={},
        )

    manifest = (
        load_pstack_manifest(nursery_root)
        if (enforce_core or include_advisory_core or explicit_skills)
        else None
    )
    core_skills: tuple[str, ...] = ()
    manifest_error: str | None = None
    if enforce_core or include_advisory_core:
        if manifest is None:
            manifest_error = "failed to load pstack manifest (harnesses/pstack.toml)"
        else:
            skills_tbl = manifest.get("skills")
            if not isinstance(skills_tbl, dict) or not isinstance(
                skills_tbl.get("core"), list
            ):
                manifest_error = (
                    "pstack manifest (harnesses/pstack.toml) is missing [skills].core"
                )
            else:
                core_skills = _dedupe(
                    [
                        s.strip()
                        for s in skills_tbl["core"]
                        if isinstance(s, str) and s.strip()
                    ]
                )
                if not core_skills:
                    manifest_error = (
                        "pstack manifest (harnesses/pstack.toml) defines empty [skills].core"
                    )

    effective_required_skills = (
        _dedupe(list(core_skills) + list(explicit_skills))
        if enforce_core
        else explicit_skills
    )

    skills_to_probe = _dedupe(list(core_skills) + list(explicit_skills))
    installed_skills = (
        discover_installed_skills(target_root, manifest, adapters=adapters)
        if skills_to_probe
        else {}
    )
    configured_mcp_entries = (
        _discover_mcp_entries(target_root, adapters=adapters) if required_mcp else {}
    )

    missing_skills = tuple(
        s for s in effective_required_skills if s not in installed_skills
    )
    found_skills = {
        s: installed_skills[s] for s in skills_to_probe if s in installed_skills
    }
    missing_core_skills = tuple(s for s in core_skills if s not in installed_skills)

    missing_mcp = tuple(m for m in required_mcp if m not in configured_mcp_entries)
    found_mcp = {
        m: configured_mcp_entries[m][0]
        for m in required_mcp
        if m in configured_mcp_entries
    }

    mcp_probe_errors: dict[str, str] = {}
    if probe_mcp:
        for m in required_mcp:
            entry_pair = configured_mcp_entries.get(m)
            if entry_pair is None:
                continue
            _, entries = entry_pair
            for entry in entries:
                ok, msg = _probe_mcp_entry(
                    entry,
                    target_root,
                    timeout_sec=probe_timeout_sec,
                    trust_project_mcp=trust_project_mcp,
                )
                if not ok:
                    mcp_probe_errors[m] = msg
                    break

    hints: list[str] = []
    if manifest_error and enforce_core:
        hints.append(
            "restore a valid harnesses/pstack.toml with [skills].core in the nursery"
        )
    if missing_skills or missing_core_skills:
        scoped_skill_dirs = _dedupe(
            [
                ADAPTER_REPO_SKILL_DIR[a]
                for a in (adapters or ())
                if a in ADAPTER_REPO_SKILL_DIR
            ]
        ) or (".agents/skills",)
        repo_skill_hint = " or ".join(f"{d}/<name>/SKILL.md" for d in scoped_skill_dirs)
        upstream = manifest.get("upstream") if isinstance(manifest, dict) else None
        pinned_ref = manifest.get("pinned_ref") if isinstance(manifest, dict) else None
        if upstream and pinned_ref:
            hints.append(
                f"run `garden harness sync-pstack --apply` (from {upstream} ref {str(pinned_ref)[:12]}) or place custom skill definitions under {repo_skill_hint}"
            )
        else:
            hints.append(f"place required skill definitions under {repo_skill_hint}")
    if missing_mcp or mcp_probe_errors:
        scoped = _dedupe([a for a in (adapters or ()) if a in ADAPTER_MCP_HINTS])
        if scoped:
            targets_desc = "; ".join(
                f"{adapter}: {ADAPTER_MCP_HINTS[adapter]}" for adapter in scoped
            )
            hints.append(f"configure/verify MCP server(s) for {targets_desc}")
        else:
            hints.append(
                "configure/verify MCP server(s) in .mcp.json, .codex/config.toml, ~/.codex/config.toml, ~/.gemini/antigravity/mcp_config.json, ~/.cursor/mcp.json, or ~/.claude.json"
            )

    return HarnessCheckResult(
        required_skills=effective_required_skills,
        missing_skills=missing_skills,
        found_skills=found_skills,
        core_skills=core_skills,
        missing_core_skills=missing_core_skills,
        core_enforced=enforce_core,
        required_mcp=required_mcp,
        missing_mcp=missing_mcp,
        found_mcp=found_mcp,
        remedy_hints=tuple(hints),
        manifest_error=manifest_error,
        mcp_probe_errors=mcp_probe_errors,
    )
