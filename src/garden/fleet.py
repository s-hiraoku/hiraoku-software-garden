"""Multi-Garden Nursery fleet status dashboard across garden.toml and Nursery self."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from garden.catalog import read_catalog
from garden.definition import GARDEN_DEF_REL_PATH, DefinitionError, load_definition
from garden.doctor import run_doctor
from garden.git_utils import GitError, head_commit, is_dirty, repo_root
from garden.metrics import collect_garden_metrics
from garden.templates import available_template_versions, default_nursery_root


def inspect_garden_status(
    repo_id: str,
    path: Path,
    *,
    approval_branch: str = "main",
    skip_network: bool = True,
    nursery_root: Path | None = None,
) -> dict[str, Any]:
    """Inspect health, doctor status, template version, and metrics for one Garden repository."""
    n_root = nursery_root or default_nursery_root()
    resolved = path.resolve()
    base: dict[str, Any] = {
        "repo": repo_id,
        "path": str(resolved),
        "garden_configured": False,
        "health": "unavailable",
    }
    if not resolved.is_dir():
        return base | {"detail": "directory does not exist"}

    try:
        root = repo_root(resolved)
    except GitError as exc:
        return base | {"detail": f"not a usable git repository: {exc}"}

    try:
        commit: str | None = head_commit(root)
    except GitError:
        commit = None
    try:
        dirty = is_dirty(root)
    except GitError:
        dirty = False

    def_path = root / GARDEN_DEF_REL_PATH
    if not def_path.is_file():
        return base | {
            "commit": commit,
            "dirty": dirty,
            "health": "unconfigured",
            "detail": f"{GARDEN_DEF_REL_PATH} not found",
        }

    try:
        definition = load_definition(def_path)
    except (DefinitionError, OSError) as exc:
        return base | {
            "commit": commit,
            "dirty": dirty,
            "garden_configured": True,
            "health": "error",
            "detail": f"invalid garden definition: {exc}",
        }

    available_vers = available_template_versions(definition.template.name, n_root)
    latest_ver = available_vers[-1] if available_vers else definition.template.version
    upgrade_available = latest_ver > definition.template.version

    doc_code, doc_items = run_doctor(
        root,
        skip_network=skip_network,
        approval_branch=approval_branch,
    )
    doc_errors = [i.id for i in doc_items if i.status == "error"]
    doc_warnings = [i.id for i in doc_items if i.status == "warn"]

    metrics = collect_garden_metrics(root)
    latest_run = metrics["recent_runs"][0] if metrics["recent_runs"] else None
    latest_verdict = latest_run.get("verdict") if latest_run else None
    open_interventions = int(metrics["interventions"]["open_count"])

    if doc_code != 0 or latest_verdict in ("UNSATISFIED", "NEEDS_HUMAN", "ABORTED"):
        health = "error"
    elif (
        doc_warnings
        or upgrade_available
        or open_interventions > 0
        or latest_verdict == "INCONCLUSIVE"
    ):
        health = "warn"
    else:
        health = "ok"

    return {
        "repo": repo_id,
        "path": str(root),
        "commit": commit,
        "dirty": dirty,
        "garden_configured": True,
        "health": health,
        "template": {
            "name": definition.template.name,
            "version": definition.template.version,
            "latest_version": latest_ver,
            "upgrade_available": upgrade_available,
        },
        "delegation_mode": definition.delegation.mode,
        "doctor": {
            "exit_code": doc_code,
            "errors": doc_errors,
            "warnings": doc_warnings,
        },
        "latest_run": latest_run,
        "metrics_summary": {
            "total_runs": metrics["total_runs_recorded"],
            "satisfied_rate": metrics["rates"]["satisfied"],
            "recurring_failure_criteria": metrics["recurring_failure_criteria"],
            "open_interventions": open_interventions,
            "verified_interventions": int(metrics["interventions"]["verified_count"]),
        },
    }


def collect_fleet_status(
    catalog_path: Path,
    workspace: Path,
    *,
    include_self: bool = True,
    approval_branch: str = "main",
    skip_network: bool = True,
    nursery_root: Path | None = None,
) -> dict[str, Any]:
    """Collect status across the Nursery itself (optional) and all catalog-registered Gardens."""
    repositories = read_catalog(catalog_path)
    gardens: list[dict[str, Any]] = []

    nursery_checkout = catalog_path.resolve().parent
    if include_self and (nursery_checkout / GARDEN_DEF_REL_PATH).is_file():
        gardens.append(
            inspect_garden_status(
                "nursery-self",
                nursery_checkout,
                approval_branch=approval_branch,
                skip_network=skip_network,
                nursery_root=nursery_root,
            )
        )

    for repo_id, entry in repositories.items():
        repo_path = (workspace / entry["path"]).resolve()
        gardens.append(
            inspect_garden_status(
                repo_id,
                repo_path,
                approval_branch=approval_branch,
                skip_network=skip_network,
                nursery_root=nursery_root,
            )
        )

    health_counts = Counter(g["health"] for g in gardens)
    has_failure = any(g["health"] in ("error", "unavailable") for g in gardens)
    return {
        "schema_version": 1,
        "catalog": str(catalog_path.resolve()),
        "total_gardens": len(gardens),
        "health_counts": dict(health_counts),
        "has_errors": has_failure,
        "gardens": gardens,
    }
