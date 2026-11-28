"""Harness requirement resolution and verification (required skills and MCP servers)."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tomllib
from typing import Any

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

# Per-adapter MCP configuration sources ordered from lowest to highest precedence.
# Format: (path_template, kind) where kind in {"toml", "json", "claude_json", "schema_dir"}.
ADAPTER_MCP_PROFILES: dict[str, tuple[tuple[str, str], ...]] = {
    "codex": (
        ("~/.codex/config.toml", "toml"),
        (".codex/config.toml", "toml"),
    ),
    "claude": (
        ("~/.claude.json", "claude_json_global"),
        (".mcp.json", "json"),
        ("~/.claude.json", "claude_json_project"),
    ),
    "antigravity": (
        ("~/.gemini/antigravity/mcp", "schema_dir"),
        ("~/.gemini/config/mcp_config.json", "json"),
        ("~/.gemini/antigravity/mcp_config.json", "json"),
        (".mcp.json", "json"),
    ),
    "cursor": (
        ("~/.cursor/mcp.json", "json"),
        (".cursor/mcp.json", "json"),
    ),
}

ADAPTER_MCP_HINTS: dict[str, str] = {
    "codex": ".codex/config.toml or ~/.codex/config.toml",
    "claude": ".mcp.json or ~/.claude.json",
    "antigravity": ".mcp.json, ~/.gemini/antigravity/mcp_config.json, or ~/.gemini/config/mcp_config.json",
    "cursor": ".cursor/mcp.json or ~/.cursor/mcp.json",
}


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

    @property
    def has_blocking_missing(self) -> bool:
        return bool(
            self.missing_skills
            or self.missing_mcp
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


def _skill_search_roots(
    target_root: Path, manifest: dict[str, Any] | None
) -> list[Path]:
    env_override = os.environ.get("GARDEN_SKILL_PATHS")
    if env_override is not None:
        raw_entries = [p.strip() for p in env_override.split(os.pathsep) if p.strip()]
        return [_expand_path(p, target_root) for p in raw_entries]

    raw_paths: list[str] = []
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
    target_root: Path, manifest: dict[str, Any] | None = None
) -> dict[str, str]:
    """Scan configured skill directories and return {skill_name: SKILL.md path}."""
    found: dict[str, str] = {}
    for base in _skill_search_roots(target_root, manifest):
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
    state: dict[str, tuple[dict[str, Any] | None, str]],
    mapping: Any,
    source: str,
) -> None:
    if not isinstance(mapping, dict):
        return
    for raw_name, cfg in mapping.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        name = raw_name.strip()
        if not isinstance(cfg, dict):
            state[name] = (None, source)
            continue
        prev_entry = state.get(name)
        if prev_entry is not None and isinstance(prev_entry[0], dict):
            merged = {**prev_entry[0], **cfg}
        else:
            merged = dict(cfg)
        state[name] = (merged, source)


def _finalize_mcp_state(
    state: dict[str, tuple[dict[str, Any] | None, str]],
) -> dict[str, str]:
    active: dict[str, str] = {}
    for name, (cfg, source) in state.items():
        if _is_disabled_mcp_server_config(cfg):
            continue
        if _is_valid_mcp_server_config(cfg):
            active[name] = source
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
    state: dict[str, tuple[dict[str, Any] | None, str]],
    path: Path,
    kind: str,
    target_root: Path,
) -> None:
    if kind == "schema_dir":
        if not path.is_dir():
            return
        try:
            for child in sorted(path.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    state.setdefault(
                        child.name, ({"command": str(child)}, str(child))
                    )
        except OSError:
            pass
        return

    if not path.is_file():
        return

    if kind == "toml":
        data = _read_toml_dict(path)
        if data is not None:
            _apply_mcp_mapping(state, data.get("mcp_servers"), str(path))
        return

    data = _read_json_dict(path)
    if data is None:
        return

    if kind == "json":
        _apply_mcp_mapping(state, data.get("mcpServers"), str(path))
        for proj_val in _matching_claude_projects(data, target_root):
            _apply_mcp_mapping(state, proj_val.get("mcpServers"), str(path))
    elif kind == "claude_json_global":
        _apply_mcp_mapping(state, data.get("mcpServers"), str(path))
    elif kind == "claude_json_project":
        for proj_val in _matching_claude_projects(data, target_root):
            disabled_list = proj_val.get("disabledMcpjsonServers")
            if isinstance(disabled_list, list):
                for raw_name in disabled_list:
                    if isinstance(raw_name, str) and raw_name.strip():
                        state[raw_name.strip()] = ({"disabled": True}, str(path))
            _apply_mcp_mapping(state, proj_val.get("mcpServers"), str(path))


def _discover_mcp_for_profile(
    target_root: Path, profile_sources: tuple[tuple[str, str], ...]
) -> dict[str, str]:
    state: dict[str, tuple[dict[str, Any] | None, str]] = {}
    for raw_path, kind in profile_sources:
        _apply_mcp_source(state, _expand_path(raw_path, target_root), kind, target_root)
    return _finalize_mcp_state(state)


def discover_configured_mcp_servers(
    target_root: Path,
    *,
    adapters: tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Scan MCP configuration files and return {server_name: source_path}.

    When `adapters` includes adapter(s) with dedicated profiles (codex, claude,
    antigravity, cursor), each selected adapter's config precedence is evaluated
    and a server is returned only if configured and enabled for all selected
    adapters. When no profile-specific adapter is selected, configured servers
    are discovered across all known adapter profiles.
    """
    env_override = os.environ.get("GARDEN_MCP_CONFIG_PATHS")
    if env_override is not None:
        state: dict[str, tuple[dict[str, Any] | None, str]] = {}
        raw_paths = [p.strip() for p in env_override.split(os.pathsep) if p.strip()]
        for raw in raw_paths:
            p = _expand_path(raw, target_root)
            if p.is_file():
                kind = "toml" if p.suffix.lower() == ".toml" else "json"
                _apply_mcp_source(state, p, kind, target_root)
            elif p.is_dir():
                _apply_mcp_source(state, p, "schema_dir", target_root)
        return _finalize_mcp_state(state)

    scoped_adapters = _dedupe(
        [a for a in (adapters or ()) if a in ADAPTER_MCP_PROFILES]
    )
    if scoped_adapters:
        per_adapter = [
            _discover_mcp_for_profile(target_root, ADAPTER_MCP_PROFILES[adapter])
            for adapter in scoped_adapters
        ]
        common_names = set(per_adapter[0].keys())
        for mapping in per_adapter[1:]:
            common_names &= set(mapping.keys())
        return {
            name: ", ".join(_dedupe([mapping[name] for mapping in per_adapter]))
            for name in sorted(common_names)
        }

    found: dict[str, str] = {}
    for profile_sources in ADAPTER_MCP_PROFILES.values():
        for name, source in _discover_mcp_for_profile(
            target_root, profile_sources
        ).items():
            found.setdefault(name, source)
    return found


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
) -> HarnessCheckResult:
    """Verify declared required_skills, required_mcp, and (when enforced) pstack core skills."""
    explicit_skills = _dedupe(
        list(definition.common_harness.required_skills)
        + list(definition.target_harness.required_skills)
    )
    required_mcp = _dedupe(list(definition.target_harness.required_mcp))

    if not enforce_core and not include_advisory_core and not explicit_skills and not required_mcp:
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
            manifest_error = (
                "failed to load pstack manifest (harnesses/pstack.toml)"
            )
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
        discover_installed_skills(target_root, manifest) if skills_to_probe else {}
    )
    configured_mcp = (
        discover_configured_mcp_servers(target_root, adapters=adapters)
        if required_mcp
        else {}
    )

    missing_skills = tuple(s for s in effective_required_skills if s not in installed_skills)
    found_skills = {
        s: installed_skills[s]
        for s in skills_to_probe
        if s in installed_skills
    }
    missing_core_skills = tuple(s for s in core_skills if s not in installed_skills)

    missing_mcp = tuple(m for m in required_mcp if m not in configured_mcp)
    found_mcp = {m: configured_mcp[m] for m in required_mcp if m in configured_mcp}

    hints: list[str] = []
    if manifest_error and enforce_core:
        hints.append(
            "restore a valid harnesses/pstack.toml with [skills].core in the nursery"
        )
    if missing_skills or missing_core_skills:
        upstream = manifest.get("upstream") if isinstance(manifest, dict) else None
        pinned_ref = manifest.get("pinned_ref") if isinstance(manifest, dict) else None
        if upstream and pinned_ref:
            hints.append(
                f"install pstack from {upstream} ref {str(pinned_ref)[:12]} into ~/.gemini/config/plugins/pstack or place custom SKILL.md under .agents/skills/<name>/ or ~/.agents/skills/<name>/"
            )
        else:
            hints.append(
                "place required skill definitions under .agents/skills/<name>/SKILL.md or ~/.agents/skills/<name>/SKILL.md"
            )
    if missing_mcp:
        scoped = _dedupe([a for a in (adapters or ()) if a in ADAPTER_MCP_HINTS])
        if scoped:
            targets_desc = "; ".join(
                f"{adapter}: {ADAPTER_MCP_HINTS[adapter]}" for adapter in scoped
            )
            hints.append(f"configure missing MCP server(s) for {targets_desc}")
        else:
            hints.append(
                "configure missing MCP server(s) in .mcp.json, .codex/config.toml, ~/.codex/config.toml, ~/.gemini/antigravity/mcp_config.json, ~/.cursor/mcp.json, or ~/.claude.json"
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
    )
