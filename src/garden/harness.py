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
    "~/.gemini/config/skills",
    "~/.cursor/skills",
    "~/.claude/skills",
)

DEFAULT_MCP_CONFIG_FILES: tuple[str, ...] = (
    ".mcp.json",
    ".cursor/mcp.json",
    "~/.gemini/antigravity/mcp_config.json",
    "~/.gemini/config/mcp_config.json",
    "~/.cursor/mcp.json",
    "~/.claude.json",
    "~/.codex/config.toml",
)

DEFAULT_MCP_SCHEMA_DIRS: tuple[str, ...] = (
    "~/.gemini/antigravity/mcp",
)


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

    @property
    def has_blocking_missing(self) -> bool:
        return bool(self.missing_skills or self.missing_mcp)


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


def _extract_mcp_servers_from_json(
    path: Path, target_root: Path
) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}

    found: dict[str, str] = {}

    def _collect(mapping: Any) -> None:
        if not isinstance(mapping, dict):
            return
        for name, cfg in mapping.items():
            if not isinstance(name, str) or not name.strip():
                continue
            if isinstance(cfg, dict) and cfg.get("disabled") is True:
                continue
            found.setdefault(name.strip(), str(path))

    _collect(data.get("mcpServers"))
    projects = data.get("projects")
    if isinstance(projects, dict):
        target_key = str(target_root.resolve())
        for proj_key, proj_val in projects.items():
            if not isinstance(proj_val, dict):
                continue
            if proj_key == target_key or Path(proj_key).expanduser().resolve() == target_root.resolve():
                _collect(proj_val.get("mcpServers"))
    return found


def _extract_mcp_servers_from_toml(path: Path) -> dict[str, str]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    found: dict[str, str] = {}
    for name, cfg in servers.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(cfg, dict) and cfg.get("enabled") is False:
            continue
        found.setdefault(name.strip(), str(path))
    return found


def discover_configured_mcp_servers(target_root: Path) -> dict[str, str]:
    """Scan MCP configuration files and return {server_name: source_path}."""
    found: dict[str, str] = {}
    env_override = os.environ.get("GARDEN_MCP_CONFIG_PATHS")
    if env_override is not None:
        raw_paths = [p.strip() for p in env_override.split(os.pathsep) if p.strip()]
        for raw in raw_paths:
            p = _expand_path(raw, target_root)
            if p.is_file():
                extracted = (
                    _extract_mcp_servers_from_toml(p)
                    if p.suffix.lower() == ".toml"
                    else _extract_mcp_servers_from_json(p, target_root)
                )
                for k, v in extracted.items():
                    found.setdefault(k, v)
            elif p.is_dir():
                try:
                    for child in sorted(p.iterdir()):
                        if child.is_dir() and not child.name.startswith("."):
                            found.setdefault(child.name, str(child))
                except OSError:
                    pass
        return found

    for raw in DEFAULT_MCP_CONFIG_FILES:
        p = _expand_path(raw, target_root)
        if not p.is_file():
            continue
        extracted = (
            _extract_mcp_servers_from_toml(p)
            if p.suffix.lower() == ".toml"
            else _extract_mcp_servers_from_json(p, target_root)
        )
        for k, v in extracted.items():
            found.setdefault(k, v)

    for raw_dir in DEFAULT_MCP_SCHEMA_DIRS:
        d = _expand_path(raw_dir, target_root)
        if not d.is_dir():
            continue
        try:
            for child in sorted(d.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    found.setdefault(child.name, str(child))
        except OSError:
            pass
    return found


def flow_uses_real_agent(definition: GardenDefinition, flow: FlowConfig) -> bool:
    for step in flow.steps:
        if step.type != "agent" or not step.agent:
            continue
        agent_cfg = definition.agents.get(step.agent)
        if agent_cfg is not None and agent_cfg.adapter != "mock":
            return True
    return False


def definition_uses_real_agent(definition: GardenDefinition) -> bool:
    return any(
        flow_uses_real_agent(definition, flow) for flow in definition.flows.values()
    )


def evaluate_harness_requirements(
    target_root: Path,
    definition: GardenDefinition,
    *,
    nursery_root: Path | None = None,
    enforce_core: bool = False,
    include_advisory_core: bool = False,
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
        )

    manifest = (
        load_pstack_manifest(nursery_root)
        if (enforce_core or include_advisory_core or explicit_skills)
        else None
    )
    core_skills: tuple[str, ...] = ()
    if isinstance(manifest, dict):
        skills_tbl = manifest.get("skills")
        if isinstance(skills_tbl, dict) and isinstance(skills_tbl.get("core"), list):
            core_skills = _dedupe([str(s).strip() for s in skills_tbl["core"] if str(s).strip()])

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
        discover_configured_mcp_servers(target_root) if required_mcp else {}
    )

    missing_skills = tuple(s for s in effective_required_skills if s not in installed_skills)
    found_skills = {
        s: installed_skills[s]
        for s in _dedupe(list(core_skills) + list(explicit_skills))
        if s in installed_skills
    }
    missing_core_skills = tuple(s for s in core_skills if s not in installed_skills)

    missing_mcp = tuple(m for m in required_mcp if m not in configured_mcp)
    found_mcp = {m: configured_mcp[m] for m in required_mcp if m in configured_mcp}

    hints: list[str] = []
    if missing_skills or missing_core_skills:
        upstream = manifest.get("upstream") if isinstance(manifest, dict) else None
        pinned_ref = manifest.get("pinned_ref") if isinstance(manifest, dict) else None
        if upstream and pinned_ref:
            hints.append(
                f"install pstack from {upstream} ref {str(pinned_ref)[:12]} into ~/.gemini/config/plugins/pstack or place custom SKILL.md under .agents/skills/<name>/"
            )
        else:
            hints.append("place required skill definitions under .agents/skills/<name>/SKILL.md")
    if missing_mcp:
        hints.append(
            "configure missing MCP server(s) in .mcp.json, ~/.gemini/antigravity/mcp_config.json, ~/.cursor/mcp.json, ~/.claude.json, or ~/.codex/config.toml"
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
    )
