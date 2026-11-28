"""Reproducible harness setup guidance and pinned pstack synchronization (MVP-6)."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from garden.definition import (
    GARDEN_DEF_REL_PATH,
    SHA40_RE,
    GardenDefinition,
    load_definition,
    load_definition_bytes,
)
from garden.git_utils import GitError, read_file_at, repo_root, resolve_branch_commit
from garden.harness import (
    ADAPTER_MCP_HINTS,
    definition_real_adapters,
    evaluate_harness_requirements,
    load_pstack_manifest,
)
from garden.resources import nursery_root as resolve_nursery_root
from garden.templates import load_template_meta


PSTACK_SYNC_ADAPTERS: dict[str, tuple[str, str]] = {
    "antigravity": ("~/.gemini/config/plugins/pstack", "plugin"),
    "cursor": ("~/.cursor/plugins/pstack", "plugin"),
    "codex": ("~/.agents/skills", "skills_dir"),
    "claude": ("~/.claude/skills", "skills_dir"),
    "repo": (".agents/skills", "skills_dir"),
}

GUIDE_ADAPTERS: tuple[str, ...] = ("antigravity", "cursor", "claude", "codex")
MANAGED_MARKER_FILENAME = ".garden-pstack-ref"

KNOWN_MCP_SNIPPETS: dict[str, dict[str, Any]] = {
    "playwright": {
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
    },
}


def _mcp_snippet_entry(server_name: str) -> dict[str, Any]:
    if server_name in KNOWN_MCP_SNIPPETS:
        return dict(KNOWN_MCP_SNIPPETS[server_name])
    return {
        "command": f"<command-for-{server_name}>",
        "args": [],
    }


def _render_json_mcp_snippet(servers: tuple[str, ...]) -> str:
    payload = {"mcpServers": {name: _mcp_snippet_entry(name) for name in servers}}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _render_codex_toml_mcp_snippet(servers: tuple[str, ...]) -> str:
    lines: list[str] = []
    for name in servers:
        entry = _mcp_snippet_entry(name)
        lines.append(f"[mcp_servers.{json.dumps(name)}]")
        lines.append(f"command = {json.dumps(entry['command'])}")
        if entry.get("args"):
            lines.append(f"args = {json.dumps(entry['args'])}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _validate_subpath(subpath: str) -> Path:
    rel = Path(subpath)
    if rel.is_absolute() or ".." in rel.parts or not subpath.strip():
        raise ValueError(f"invalid pstack manifest subpath: {subpath!r}")
    return rel


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _normalize_without_following_final_symlink(path: Path) -> Path:
    """Resolve parent directory while preserving the final path component without following symlinks."""
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    if not expanded.name:
        return expanded
    return expanded.parent.resolve() / expanded.name


def _resolve_sync_dest(
    adapter: str, dest: Path | None, target_root: Path | None
) -> tuple[Path, str]:
    if adapter not in PSTACK_SYNC_ADAPTERS:
        raise ValueError(
            f"unsupported adapter {adapter!r}; choose from auto, {', '.join(sorted(PSTACK_SYNC_ADAPTERS))}"
        )
    default_raw, layout = PSTACK_SYNC_ADAPTERS[adapter]
    if dest is not None:
        raw_dest = dest.expanduser()
        if not raw_dest.is_absolute():
            anchor = (target_root or Path.cwd()).resolve()
            raw_dest = anchor / raw_dest
        final_dest = _normalize_without_following_final_symlink(raw_dest)
    else:
        base_raw = Path(default_raw).expanduser()
        if base_raw.is_absolute():
            final_dest = _normalize_without_following_final_symlink(base_raw)
        else:
            anchor = (target_root or Path.cwd()).resolve()
            final_dest = _normalize_without_following_final_symlink(anchor / base_raw)

    if final_dest.is_symlink():
        raise ValueError(
            f"refusing to sync pstack into symlink destination: {final_dest}"
        )
    return final_dest, layout


def _discover_skill_dirs(pstack_src: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    candidates = [pstack_src]
    nested = pstack_src / "skills"
    if nested.is_dir():
        candidates.insert(0, nested)
    for root_dir in candidates:
        for child in sorted(root_dir.iterdir()):
            if child.is_dir() and (child / "SKILL.md").is_file():
                found.setdefault(child.name, child)
    return found


def _load_target_definition_for_sync(
    target_root: Path, approval_branch: str | None
) -> GardenDefinition | None:
    try:
        root = repo_root(target_root)
    except GitError:
        root = target_root.resolve()

    if approval_branch:
        try:
            ref_name, commit_sha = resolve_branch_commit(root, approval_branch)
        except GitError as exc:
            raise ValueError(str(exc)) from exc
        raw = read_file_at(root, commit_sha, GARDEN_DEF_REL_PATH)
        if raw is not None:
            return load_definition_bytes(raw, f"{ref_name}:{GARDEN_DEF_REL_PATH}")
        raise ValueError(
            f"could not read approved {GARDEN_DEF_REL_PATH} from branch '{approval_branch}'"
        )

    def_path = root / GARDEN_DEF_REL_PATH
    if def_path.is_file():
        return load_definition(def_path)
    return None


def _resolve_auto_adapters(
    definition: GardenDefinition | None,
) -> tuple[str, ...]:
    if definition is None:
        return ("codex",)
    real_adapters = definition_real_adapters(definition)
    if not real_adapters:
        return ("codex",)
    unsupported = [a for a in real_adapters if a not in PSTACK_SYNC_ADAPTERS]
    if unsupported:
        raise ValueError(
            f"automatic pstack sync does not support adapter(s): {', '.join(unsupported)}; pass --adapter and --dest explicitly"
        )
    return real_adapters


def _fetch_verified_pstack_subpath(
    clone_dir: Path,
    fetch_source: str,
    pinned_ref: str,
    subpath: Path,
    core_skills: list[str],
) -> tuple[Path, dict[str, Path]]:
    clone_dir.mkdir(parents=True, exist_ok=True)
    init_proc = _run_git(clone_dir, "init", "-q")
    if init_proc.returncode != 0:
        raise RuntimeError(f"git init failed: {init_proc.stderr.strip()}")

    fetch_proc = _run_git(clone_dir, "fetch", "--depth", "1", fetch_source, pinned_ref)
    if fetch_proc.returncode != 0:
        fallback = _run_git(clone_dir, "fetch", fetch_source)
        if fallback.returncode != 0:
            raise RuntimeError(
                f"git fetch failed for {fetch_source}@{pinned_ref}: "
                f"{fallback.stderr.strip() or fetch_proc.stderr.strip()}"
            )
        co_proc = _run_git(clone_dir, "checkout", "--detach", pinned_ref)
    else:
        co_proc = _run_git(clone_dir, "checkout", "--detach", "FETCH_HEAD")

    if co_proc.returncode != 0:
        raise RuntimeError(
            f"git checkout {pinned_ref} failed: {co_proc.stderr.strip()}"
        )

    rev_proc = _run_git(clone_dir, "rev-parse", "HEAD")
    actual_sha = rev_proc.stdout.strip()
    if rev_proc.returncode != 0 or actual_sha != pinned_ref:
        raise RuntimeError(
            f"checked out commit {actual_sha!r} does not match pinned_ref {pinned_ref!r}"
        )

    src_root = (clone_dir / subpath).resolve()
    if not src_root.is_dir() or (
        clone_dir.resolve() not in src_root.parents
        and src_root != clone_dir.resolve()
    ):
        raise RuntimeError(
            f"subpath {subpath.as_posix()!r} not found in {fetch_source}@{pinned_ref}"
        )

    skill_dirs = _discover_skill_dirs(src_root)
    missing_core = [s for s in core_skills if s not in skill_dirs]
    if missing_core:
        raise RuntimeError(
            f"fetched pstack@{pinned_ref[:12]} is missing core skill(s): {', '.join(missing_core)}"
        )
    return src_root, skill_dirs


def _install_plugin_layout(
    src_root: Path,
    skill_dirs: dict[str, Path],
    dest_path: Path,
    pinned_ref: str,
) -> list[str]:
    if dest_path.is_symlink():
        raise ValueError(
            f"refusing to sync pstack into symlink destination: {dest_path}"
        )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".pstack-sync-", dir=str(dest_path.parent))
    )
    backup: Path | None = None
    preserved_custom: list[str] = []
    try:
        shutil.copytree(src_root, staging, dirs_exist_ok=True)
        # Preserve non-conflicting custom skills already in the existing plugin dir
        if dest_path.is_dir():
            existing_skills = _discover_skill_dirs(dest_path)
            target_skills_parent = (
                (staging / "skills") if (staging / "skills").is_dir() else staging
            )
            for name, existing_dir in sorted(existing_skills.items()):
                if name in skill_dirs:
                    continue
                shutil.copytree(existing_dir, target_skills_parent / name)
                preserved_custom.append(name)

        (staging / MANAGED_MARKER_FILENAME).write_text(
            pinned_ref + "\n", encoding="utf-8"
        )
        if dest_path.exists():
            backup = dest_path.parent / f".pstack-backup-{staging.name}"
            dest_path.replace(backup)
        staging.replace(dest_path)
    except Exception:
        if backup is not None and backup.exists() and not dest_path.exists():
            backup.replace(dest_path)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if backup is not None and backup.exists():
            if backup.is_file():
                backup.unlink(missing_ok=True)
            else:
                shutil.rmtree(backup, ignore_errors=True)
    return sorted(set(skill_dirs.keys()) | set(preserved_custom))


def _install_skills_dir_layout(
    skill_dirs: dict[str, Path],
    dest_path: Path,
    pinned_ref: str,
    core_skills: list[str],
    *,
    force: bool = False,
) -> list[str]:
    if dest_path.is_symlink():
        raise ValueError(
            f"refusing to sync pstack into symlink destination: {dest_path}"
        )
    dest_path.mkdir(parents=True, exist_ok=True)

    # Pre-flight safety checks before writing anything
    unmanaged_conflicts: list[str] = []
    for name in sorted(skill_dirs.keys()):
        existing = dest_path / name
        if existing.is_symlink():
            raise ValueError(
                f"refusing to overwrite symlink skill directory: {existing}"
            )
        if existing.exists():
            marker = existing / MANAGED_MARKER_FILENAME
            if not marker.is_file() and not force:
                unmanaged_conflicts.append(name)
    if unmanaged_conflicts:
        raise ValueError(
            f"refusing to overwrite unmanaged existing skill(s) in {dest_path}: "
            f"{', '.join(unmanaged_conflicts)} (pass --force to replace)"
        )

    staging = Path(tempfile.mkdtemp(prefix=".pstack-stage-", dir=str(dest_path)))
    backup = Path(tempfile.mkdtemp(prefix=".pstack-backup-", dir=str(dest_path)))
    moved_to_backup: list[str] = []
    installed_names: list[str] = []
    try:
        # Stage and verify ALL skills before mutating dest_path
        for name, sdir in sorted(skill_dirs.items()):
            staged_dir = staging / name
            shutil.copytree(sdir, staged_dir)
            (staged_dir / MANAGED_MARKER_FILENAME).write_text(
                pinned_ref + "\n", encoding="utf-8"
            )
        for core in core_skills:
            if not (staging / core / "SKILL.md").is_file():
                raise RuntimeError(f"staged core skill missing SKILL.md: {core}")

        # Move existing target dirs to backup first
        for name in sorted(skill_dirs.keys()):
            existing = dest_path / name
            if existing.exists():
                existing.replace(backup / name)
                moved_to_backup.append(name)

        # Atomically move all staged skills into place
        for name in sorted(skill_dirs.keys()):
            (staging / name).replace(dest_path / name)
            installed_names.append(name)
    except Exception:
        # Roll back: remove partially installed skills and restore every backup
        for name in installed_names:
            partial = dest_path / name
            if partial.exists():
                shutil.rmtree(partial, ignore_errors=True)
        for name in moved_to_backup:
            backed = backup / name
            if backed.exists() and not (dest_path / name).exists():
                backed.replace(dest_path / name)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)

    return installed_names


def sync_pstack(
    *,
    adapter: str = "antigravity",
    dest: Path | None = None,
    target_root: Path | None = None,
    approval_branch: str | None = None,
    source: str | None = None,
    apply: bool = False,
    force: bool = False,
    nursery_root: Path | None = None,
) -> dict[str, Any]:
    """Plan or execute pinned pstack synchronization from harnesses/pstack.toml.

    Dry-run by default (`apply=False`). When `apply=True`, fetches the exact
    `pinned_ref` commit SHA, verifies `git rev-parse HEAD == pinned_ref` and
    that all `[skills].core` definitions exist, then installs atomically into `dest`.
    """
    n_root = nursery_root or resolve_nursery_root()
    manifest = load_pstack_manifest(n_root)
    if manifest is None:
        raise ValueError("failed to load harnesses/pstack.toml from nursery")

    upstream = str(manifest.get("upstream") or "").strip()
    pinned_ref = str(manifest.get("pinned_ref") or "").strip()
    subpath_raw = str(manifest.get("subpath") or "pstack").strip()
    if not upstream and not source:
        raise ValueError("harnesses/pstack.toml is missing 'upstream'")
    if not SHA40_RE.match(pinned_ref):
        raise ValueError(
            f"harnesses/pstack.toml pinned_ref must be a 40-char commit SHA, got {pinned_ref!r}"
        )
    subpath = _validate_subpath(subpath_raw)

    skills_tbl = manifest.get("skills")
    if not isinstance(skills_tbl, dict) or not isinstance(skills_tbl.get("core"), list):
        raise ValueError("harnesses/pstack.toml is missing [skills].core")
    core_skills = [
        s.strip() for s in skills_tbl["core"] if isinstance(s, str) and s.strip()
    ]
    if not core_skills:
        raise ValueError("harnesses/pstack.toml defines empty [skills].core")

    target_def: GardenDefinition | None = None
    if target_root is not None and (approval_branch is not None or adapter == "auto"):
        target_def = _load_target_definition_for_sync(target_root, approval_branch)
    if target_def is not None and target_def.common_harness.ref != pinned_ref:
        raise ValueError(
            f"target approved [common_harness].ref ({target_def.common_harness.ref[:12]}) "
            f"does not match nursery pinned_ref ({pinned_ref[:12]})"
        )

    if adapter == "auto":
        if dest is not None:
            raise ValueError("--dest cannot be combined with --adapter auto")
        adapters_to_sync = _resolve_auto_adapters(target_def)
    else:
        adapters_to_sync = (adapter,)

    resolved_targets: list[tuple[str, Path, str]] = []
    for ad in adapters_to_sync:
        dpath, layout = _resolve_sync_dest(ad, dest, target_root)
        resolved_targets.append((ad, dpath, layout))

    fetch_source = source or upstream
    primary_adapter, primary_dest, primary_layout = resolved_targets[0]

    plan: dict[str, Any] = {
        "adapter": adapter,
        "resolved_adapters": [ad for ad, _, _ in resolved_targets],
        "layout": primary_layout,
        "source": fetch_source,
        "subpath": subpath.as_posix(),
        "pinned_ref": pinned_ref,
        "dest": str(primary_dest),
        "destinations": [
            {"adapter": ad, "dest": str(dp), "layout": ly}
            for ad, dp, ly in resolved_targets
        ],
        "core_skills": core_skills,
        "applied": False,
    }
    if not apply:
        return plan

    with tempfile.TemporaryDirectory() as tmp:
        src_root, skill_dirs = _fetch_verified_pstack_subpath(
            Path(tmp) / "pstack-src",
            fetch_source,
            pinned_ref,
            subpath,
            core_skills,
        )
        all_installed: list[str] = []
        for _, dest_path, layout in resolved_targets:
            if layout == "plugin":
                installed = _install_plugin_layout(
                    src_root, skill_dirs, dest_path, pinned_ref
                )
            else:
                installed = _install_skills_dir_layout(
                    skill_dirs,
                    dest_path,
                    pinned_ref,
                    core_skills,
                    force=force,
                )
            for s in installed:
                if s not in all_installed:
                    all_installed.append(s)

    return plan | {
        "applied": True,
        "installed_skills": sorted(all_installed),
        "verified_core_skills": core_skills,
    }


def _load_template_standard_harness(
    template_name: str, version: int, nursery_root: Path | None
) -> dict[str, Any]:
    try:
        _, meta = load_template_meta(template_name, version, nursery_root)
    except ValueError:
        return {}
    std = meta.get("standard_harness")
    return dict(std) if isinstance(std, dict) else {}


def build_harness_guide(
    target_root: Path,
    *,
    adapter: str = "all",
    nursery_root: Path | None = None,
    strict_harness: bool = False,
    probe_mcp: bool = False,
    trust_project_mcp: bool = False,
) -> dict[str, Any]:
    """Inspect a target repository and produce concrete setup guidance per environment."""
    if adapter != "all" and adapter not in GUIDE_ADAPTERS:
        raise ValueError(
            f"unsupported adapter {adapter!r}; choose from all, {', '.join(GUIDE_ADAPTERS)}"
        )

    n_root = nursery_root or resolve_nursery_root()
    try:
        root = repo_root(target_root)
    except GitError:
        root = target_root.resolve()
    definition = load_definition(root / GARDEN_DEF_REL_PATH)
    manifest = load_pstack_manifest(n_root)

    real_adapters = definition_real_adapters(definition)
    eval_adapters = (adapter,) if adapter != "all" else real_adapters
    enforce_core = strict_harness or bool(real_adapters)
    evaluation = evaluate_harness_requirements(
        root,
        definition,
        nursery_root=n_root,
        enforce_core=enforce_core,
        include_advisory_core=True,
        adapters=eval_adapters,
        probe_mcp=probe_mcp,
        trust_project_mcp=trust_project_mcp,
    )

    std_harness = _load_template_standard_harness(
        definition.template.name, definition.template.version, n_root
    )
    recommended_common_skills = list(std_harness.get("common_required_skills") or [])
    recommended_target_skills = list(std_harness.get("target_required_skills") or [])
    recommended_mcp = list(std_harness.get("target_required_mcp") or [])

    declared_skills = set(definition.common_harness.required_skills) | set(
        definition.target_harness.required_skills
    )
    declared_mcp = set(definition.target_harness.required_mcp)

    undeclared_recommended_skills = [
        s
        for s in (recommended_common_skills + recommended_target_skills)
        if s not in declared_skills
    ]
    undeclared_recommended_mcp = [
        m for m in recommended_mcp if m not in declared_mcp
    ]

    mcp_for_snippets = tuple(
        dict.fromkeys(
            list(evaluation.missing_mcp)
            + list(evaluation.mcp_probe_errors.keys())
            + list(evaluation.required_mcp)
            + undeclared_recommended_mcp
        )
    )

    selected_envs = GUIDE_ADAPTERS if adapter == "all" else (adapter,)
    environments: dict[str, dict[str, Any]] = {}
    for env_name in selected_envs:
        dest_raw, layout = PSTACK_SYNC_ADAPTERS[env_name]
        env_entry: dict[str, Any] = {
            "pstack_sync_command": f"garden harness sync-pstack --adapter {env_name} --apply",
            "pstack_dest": dest_raw,
            "pstack_layout": layout,
            "mcp_config_targets": ADAPTER_MCP_HINTS[env_name],
        }
        if mcp_for_snippets:
            if env_name == "codex":
                env_entry["mcp_snippet_format"] = "toml"
                env_entry["mcp_snippet"] = _render_codex_toml_mcp_snippet(
                    mcp_for_snippets
                )
            else:
                env_entry["mcp_snippet_format"] = "json"
                env_entry["mcp_snippet"] = _render_json_mcp_snippet(mcp_for_snippets)
        environments[env_name] = env_entry

    custom_missing_skills = [
        s
        for s in evaluation.missing_skills
        if s not in set(evaluation.core_skills)
        and s not in set(definition.common_harness.required_skills)
    ]

    return {
        "target": str(root),
        "template": {
            "name": definition.template.name,
            "version": definition.template.version,
            "standard_harness": {
                "common_required_skills": recommended_common_skills,
                "target_required_skills": recommended_target_skills,
                "target_required_mcp": recommended_mcp,
                "notes": std_harness.get("notes", ""),
            },
            "undeclared_recommended_skills": undeclared_recommended_skills,
            "undeclared_recommended_mcp": undeclared_recommended_mcp,
        },
        "pstack": {
            "upstream": manifest.get("upstream") if isinstance(manifest, dict) else None,
            "pinned_ref": manifest.get("pinned_ref") if isinstance(manifest, dict) else None,
            "core_skills": list(evaluation.core_skills),
            "manifest_error": evaluation.manifest_error,
        },
        "status": {
            "ok": not evaluation.has_blocking_missing,
            "core_enforced": evaluation.core_enforced,
            "required_skills": list(evaluation.required_skills),
            "missing_skills": list(evaluation.missing_skills),
            "missing_core_skills": list(evaluation.missing_core_skills),
            "found_skills": dict(evaluation.found_skills),
            "required_mcp": list(evaluation.required_mcp),
            "missing_mcp": list(evaluation.missing_mcp),
            "found_mcp": dict(evaluation.found_mcp),
            "mcp_probe_errors": dict(evaluation.mcp_probe_errors),
            "custom_skill_scaffold_paths": [
                f".agents/skills/{name}/SKILL.md" for name in custom_missing_skills
            ],
        },
        "environments": environments,
        "ci_hint": "Pass `sync_pstack: true` to garden-reusable.yml (using a nursery_ref that includes sync_pstack) when running real-agent flows in GitHub Actions.",
    }


def format_harness_guide_markdown(guide: dict[str, Any]) -> str:
    status = guide["status"]
    tpl = guide["template"]
    pstack = guide["pstack"]
    lines = [
        f"# Garden Harness Setup Guide ({tpl['name']}@v{tpl['version']})",
        "",
        f"- Target: `{guide['target']}`",
        f"- Status: {'OK' if status['ok'] else 'ACTION REQUIRED'}",
        f"- Pinned pstack: `{pstack.get('pinned_ref') or 'unavailable'}`",
    ]
    if pstack.get("manifest_error"):
        lines.append(f"- Manifest error: {pstack['manifest_error']}")
    if status["missing_skills"]:
        lines.append(f"- Missing required skills: {', '.join(status['missing_skills'])}")
    elif status["missing_core_skills"]:
        lines.append(
            f"- Missing pstack core skills (advisory): {', '.join(status['missing_core_skills'])}"
        )
    if status["missing_mcp"]:
        lines.append(f"- Missing required MCP servers: {', '.join(status['missing_mcp'])}")
    if status["mcp_probe_errors"]:
        probe_items = ", ".join(
            f"{k} ({v})" for k, v in sorted(status["mcp_probe_errors"].items())
        )
        lines.append(f"- MCP probe errors: {probe_items}")
    if status["custom_skill_scaffold_paths"]:
        lines.append(
            "- Target skill paths to create: "
            + ", ".join(f"`{p}`" for p in status["custom_skill_scaffold_paths"])
        )

    std = tpl["standard_harness"]
    if tpl["undeclared_recommended_skills"] or tpl["undeclared_recommended_mcp"]:
        lines.extend(["", "## Template Standard Harness Recommendations"])
        if std.get("notes"):
            lines.append(f"- {std['notes']}")
        if tpl["undeclared_recommended_skills"]:
            lines.append(
                "- Recommended `required_skills`: "
                + ", ".join(f"`{s}`" for s in tpl["undeclared_recommended_skills"])
            )
        if tpl["undeclared_recommended_mcp"]:
            lines.append(
                "- Recommended `required_mcp`: "
                + ", ".join(f"`{m}`" for m in tpl["undeclared_recommended_mcp"])
            )

    lines.extend(["", "## Environment Setup"])
    for env_name, env_cfg in guide["environments"].items():
        lines.append(f"### {env_name}")
        lines.append(
            f"- Sync pstack (`{env_cfg['pstack_dest']}`): `{env_cfg['pstack_sync_command']}`"
        )
        lines.append(f"- MCP config location(s): {env_cfg['mcp_config_targets']}")
        if "mcp_snippet" in env_cfg:
            fmt = env_cfg.get("mcp_snippet_format", "json")
            lines.append(f"```{fmt}")
            lines.append(env_cfg["mcp_snippet"])
            lines.append("```")
        lines.append("")

    lines.append(f"- CI: {guide['ci_hint']}")
    return "\n".join(lines).rstrip()
