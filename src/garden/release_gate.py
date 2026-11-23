"""Template release gate & release-evidence.json generator (Detailed Design §7.3)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from garden.definition import GARDEN_DEF_REL_PATH, load_definition
from garden.doctor import run_doctor
from garden.engine import run_flow
from garden.templates import (
    available_template_versions,
    default_nursery_root,
    init_garden,
    load_template_meta,
    plan_upgrade,
    apply_upgrade,
)


def _git(repo: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")


def _init_disposable_repo(repo_dir: Path) -> Path:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.name", "Garden Release Gate")
    _git(repo_dir, "config", "user.email", "release-gate@example.invalid")
    _git(repo_dir, "remote", "add", "origin", f"https://example.invalid/{repo_dir.name}.git")
    (repo_dir / "README.md").write_text(f"# {repo_dir.name}\n", encoding="utf-8")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "initial")
    return repo_dir


def _list_template_types(nursery_root: Path) -> list[str]:
    tdir = nursery_root / "templates"
    if not tdir.is_dir():
        return []
    return sorted(
        child.name
        for child in tdir.iterdir()
        if child.is_dir() and available_template_versions(child.name, nursery_root)
    )


def _replace_first_check_command_with_failure(garden_toml: Path) -> None:
    text = garden_toml.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"(?m)^command\s*=\s*\[[^\n]*\]$",
        'command = ["python3", "-c", "import sys; sys.exit(1)"]',
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"could not replace first check command in {garden_toml}")
    garden_toml.write_text(updated, encoding="utf-8")


def _cover_all_initial_criteria(garden_toml: Path, spec_path: Path) -> None:
    # Ensure the initial spec is approved and all its criteria are covered by the first check
    spec_text = spec_path.read_text(encoding="utf-8").replace(
        "status: draft", "status: approved"
    )
    spec_path.write_text(spec_text, encoding="utf-8")

    ids = re.findall(r"\*\*((?:AC|INV)-\d{3,})\*\*", spec_text)
    if not ids:
        return
    unique_ids = list(dict.fromkeys(ids))
    covers_toml = ", ".join(json.dumps(cid) for cid in unique_ids)
    gtext = garden_toml.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"(?m)^covers\s*=\s*\[[^\n]*\]$",
        f"covers = [{covers_toml}]",
        gtext,
        count=1,
    )
    if count == 1:
        garden_toml.write_text(updated, encoding="utf-8")


def verify_template_release(
    template_type: str,
    version: int | None = None,
    *,
    nursery_root: Path | None = None,
) -> dict[str, Any]:
    """Run the two-part template release gate (fresh init pass/fail + upgrade preservation)."""
    n_root = nursery_root or default_nursery_root()
    versions = available_template_versions(template_type, n_root)
    if not versions:
        raise ValueError(f"no versions found for template {template_type!r}")
    target_ver = version if version is not None else versions[-1]
    if target_ver not in versions:
        raise ValueError(
            f"template {template_type!r} version v{target_ver} not found (available: {versions})"
        )

    errors: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Gate 1: Fresh init -> doctor -> passing run (SATISFIED) -> failing run (UNSATISFIED)
        fresh_repo = _init_disposable_repo(tmp_path / f"fresh-{template_type}-v{target_ver}")
        init_res = init_garden(
            fresh_repo,
            template_type=template_type,
            version=target_ver,
            nursery_root=n_root,
        )
        spec_files = sorted((fresh_repo / "docs" / "specs").glob("*.md"))
        if not spec_files:
            raise RuntimeError("init_garden did not generate an initial spec")
        garden_toml = fresh_repo / GARDEN_DEF_REL_PATH
        _cover_all_initial_criteria(garden_toml, spec_files[0])
        _git(fresh_repo, "add", "-A")
        _git(fresh_repo, "commit", "-m", "approve initial spec")

        doctor_code, _ = run_doctor(fresh_repo, skip_network=True, approval_branch="main")
        if doctor_code != 0:
            errors.append(f"doctor returned non-zero exit code {doctor_code}")

        pass_run = run_flow(
            fresh_repo,
            "verify_only",
            approval_branch="main",
            force=True,
        )
        if pass_run.verdict != "SATISFIED" or pass_run.exit_code != 0:
            errors.append(
                f"fresh passing fixture expected SATISFIED (0), got {pass_run.verdict} ({pass_run.exit_code})"
            )

        _replace_first_check_command_with_failure(garden_toml)
        _git(fresh_repo, "add", "-A")
        _git(fresh_repo, "commit", "-m", "inject failing smoke check")
        fail_run = run_flow(
            fresh_repo,
            "verify_only",
            approval_branch="main",
            force=True,
        )
        if fail_run.verdict != "UNSATISFIED" or fail_run.exit_code != 1:
            errors.append(
                f"fresh failing fixture expected UNSATISFIED (1), got {fail_run.verdict} ({fail_run.exit_code})"
            )

        # Gate 2: Upgrade from previous version (or synthetic v(N+1) when testing v1)
        prev_versions = [v for v in versions if v < target_ver]
        if prev_versions:
            upgrade_mode = "real_previous_version"
            from_ver = prev_versions[-1]
            to_ver = target_ver
            upgrade_nursery = n_root
        else:
            upgrade_mode = "synthetic_next_version"
            from_ver = target_ver
            to_ver = target_ver + 1
            upgrade_nursery = tmp_path / "synthetic-nursery"
            shutil.copytree(n_root / "templates", upgrade_nursery / "templates")
            src_vdir, _ = load_template_meta(template_type, from_ver, upgrade_nursery)
            dst_vdir = upgrade_nursery / "templates" / template_type / f"v{to_ver}"
            shutil.copytree(src_vdir, dst_vdir)
            meta_file = dst_vdir / "template.toml"
            meta_text = meta_file.read_text(encoding="utf-8")
            meta_text = re.sub(
                r"(?m)^version\s*=\s*\d+$", f"version = {to_ver}", meta_text, count=1
            )
            meta_text += (
                "\n[defaults.checks.release_gate_candidate]\n"
                'kind = "check"\n'
                'command = ["python3", "-c", "print(\'candidate ok\')"]\n'
                "timeout_sec = 60\n"
                'covers = ["AC-001"]\n'
            )
            meta_file.write_text(meta_text, encoding="utf-8")

        upg_repo = _init_disposable_repo(tmp_path / f"upgrade-{template_type}")
        init_garden(
            upg_repo,
            template_type=template_type,
            version=from_ver,
            nursery_root=upgrade_nursery,
        )
        upg_spec = sorted((upg_repo / "docs" / "specs").glob("*.md"))[0]
        upg_toml = upg_repo / GARDEN_DEF_REL_PATH
        _cover_all_initial_criteria(upg_toml, upg_spec)
        # Add an operator custom check to verify preservation
        upg_toml.write_text(
            upg_toml.read_text(encoding="utf-8").rstrip()
            + "\n\n[checks.custom_operator_check]\n"
            + 'kind = "check"\n'
            + 'command = ["python3", "-c", "print(\'custom ok\')"]\n'
            + "timeout_sec = 60\n"
            + 'covers = ["AC-001"]\n'
            + 'status = "active"\n',
            encoding="utf-8",
        )
        _git(upg_repo, "add", "-A")
        _git(upg_repo, "commit", "-m", "init with custom operator check")

        plan = plan_upgrade(upg_repo, to_version=to_ver, nursery_root=upgrade_nursery)
        if "custom_operator_check" not in plan.preserved_custom_checks:
            errors.append("upgrade plan failed to preserve custom_operator_check")

        apply_upgrade(upg_repo, to_version=to_ver, nursery_root=upgrade_nursery)
        upgraded_def = load_definition(upg_toml)
        if upgraded_def.template.version != to_ver:
            errors.append(
                f"upgraded template version expected {to_ver}, got {upgraded_def.template.version}"
            )
        if "custom_operator_check" not in upgraded_def.checks:
            errors.append("custom_operator_check missing after apply_upgrade")
        elif upgraded_def.checks["custom_operator_check"].status != "active":
            errors.append("custom_operator_check status changed after apply_upgrade")

        for proposed in plan.proposed_new_checks:
            chk = upgraded_def.checks.get(proposed.name)
            if chk is None or chk.status != "proposed":
                errors.append(
                    f"newly added template check {proposed.name!r} was not added with status='proposed'"
                )

    return {
        "template": template_type,
        "version": target_ver,
        "init_files": init_res["created_files"],
        "fresh_init": {
            "doctor_exit_code": doctor_code,
            "pass_verdict": pass_run.verdict,
            "pass_exit_code": pass_run.exit_code,
            "fail_verdict": fail_run.verdict,
            "fail_exit_code": fail_run.exit_code,
        },
        "upgrade": {
            "mode": upgrade_mode,
            "from_version": from_ver,
            "to_version": to_ver,
            "preserved_custom_checks": list(plan.preserved_custom_checks),
            "proposed_new_checks": [p.name for p in plan.proposed_new_checks],
        },
        "passed": not errors,
        "errors": errors,
    }


def run_template_release_gate(
    *,
    template_type: str | None = None,
    version: int | None = None,
    all_templates: bool = False,
    output_path: Path | None = None,
    nursery_root: Path | None = None,
) -> dict[str, Any]:
    """Run template release gates and optionally write release-evidence.json."""
    n_root = nursery_root or default_nursery_root()
    if all_templates:
        targets = [(t, None) for t in _list_template_types(n_root)]
    elif template_type:
        targets = [(template_type, version)]
    else:
        raise ValueError("specify either template_type or all_templates=True")

    if not targets:
        raise ValueError("no templates found to verify")

    results = [
        verify_template_release(t, v, nursery_root=n_root) for t, v in targets
    ]
    commit_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=n_root,
        capture_output=True,
        text=True,
        check=False,
    )
    nursery_commit = commit_proc.stdout.strip() if commit_proc.returncode == 0 else None

    evidence: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nursery_commit": nursery_commit,
        "overall_passed": all(r["passed"] for r in results),
        "results": results,
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return evidence
