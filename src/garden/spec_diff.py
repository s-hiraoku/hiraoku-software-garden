"""Criterion-level specification diff and check/flow impact analysis (Design §2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from garden.definition import GARDEN_DEF_REL_PATH, load_definition, load_definition_bytes
from garden.git_utils import (
    GitError,
    git_run,
    read_file_at,
    repo_root,
    resolve_branch_commit,
)
from garden.spec import SpecDocument, parse_spec_markdown


def _resolve_commit_ref(root: Path, ref: str) -> str:
    """Resolve a branch name or explicit Git commit/ref to a full commit SHA."""
    if not ref or ref.startswith("-"):
        raise GitError(f"invalid git ref: {ref!r}")
    try:
        _, branch_sha = resolve_branch_commit(root, ref)
        return branch_sha
    except GitError:
        pass
    proc = git_run(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    raise GitError(f"unable to resolve git ref '{ref}'")


def _criteria_map(doc: SpecDocument | None) -> dict[str, str]:
    if doc is None:
        return {}
    return dict(doc.acceptance_criteria) | dict(doc.invariants)


def analyze_spec_diff(
    target: Path,
    *,
    base_ref: str = "main",
    head_ref: str | None = None,
) -> dict[str, Any]:
    """Compare approved base spec(s) against head/working-tree spec(s) and map impacted checks/flows."""
    root = repo_root(target)
    base_commit = _resolve_commit_ref(root, base_ref)
    head_commit_sha = _resolve_commit_ref(root, head_ref) if head_ref else None

    if head_commit_sha is not None:
        def_bytes = read_file_at(root, head_commit_sha, GARDEN_DEF_REL_PATH)
        if def_bytes is None:
            raise ValueError(f"{GARDEN_DEF_REL_PATH} not found at {head_ref}")
        definition = load_definition_bytes(def_bytes, source=f"{head_ref}:{GARDEN_DEF_REL_PATH}")
    else:
        definition = load_definition(root / GARDEN_DEF_REL_PATH)

    spec_diffs: list[dict[str, Any]] = []
    all_added: set[str] = set()
    all_removed: set[str] = set()
    all_modified: set[str] = set()
    all_unchanged: set[str] = set()
    any_change = False

    for spath in definition.spec.paths:
        base_bytes = read_file_at(root, base_commit, spath)
        base_doc = (
            parse_spec_markdown(base_bytes.decode("utf-8"), f"{base_ref}:{spath}")
            if base_bytes is not None
            else None
        )

        if head_commit_sha is not None:
            head_bytes = read_file_at(root, head_commit_sha, spath)
            if head_bytes is None:
                raise ValueError(f"spec '{spath}' not found at {head_ref}")
            head_doc = parse_spec_markdown(
                head_bytes.decode("utf-8"), f"{head_ref}:{spath}"
            )
        else:
            full_path = root / spath
            if not full_path.is_file():
                raise ValueError(f"spec '{spath}' not found in working tree")
            head_doc = parse_spec_markdown(full_path.read_text(encoding="utf-8"), spath)

        base_map = _criteria_map(base_doc)
        head_map = _criteria_map(head_doc)

        added = [
            {"id": cid, "text": head_map[cid]}
            for cid in sorted(head_map.keys() - base_map.keys())
        ]
        removed = [
            {"id": cid, "text": base_map[cid]}
            for cid in sorted(base_map.keys() - head_map.keys())
        ]
        modified = [
            {"id": cid, "before": base_map[cid], "after": head_map[cid]}
            for cid in sorted(base_map.keys() & head_map.keys())
            if base_map[cid] != head_map[cid]
        ]
        unchanged = sorted(
            cid
            for cid in (base_map.keys() & head_map.keys())
            if base_map[cid] == head_map[cid]
        )

        all_added.update(item["id"] for item in added)
        all_removed.update(item["id"] for item in removed)
        all_modified.update(item["id"] for item in modified)
        all_unchanged.update(unchanged)

        version_changed = (base_doc.version if base_doc else None) != head_doc.version
        status_changed = (base_doc.status if base_doc else None) != head_doc.status
        spec_changed = bool(
            base_doc is None or added or removed or modified or version_changed or status_changed
        )
        if spec_changed:
            any_change = True

        spec_diffs.append(
            {
                "path": spath,
                "spec_id": head_doc.spec_id,
                "is_new_spec": base_doc is None,
                "base_version": base_doc.version if base_doc else None,
                "head_version": head_doc.version,
                "base_status": base_doc.status if base_doc else None,
                "head_status": head_doc.status,
                "changed": spec_changed,
                "added": added,
                "removed": removed,
                "modified": modified,
                "unchanged": unchanged,
            }
        )

    active_covered: set[str] = set()
    impacted_checks: list[dict[str, Any]] = []
    orphaned_checks: list[dict[str, Any]] = []
    impacted_check_names: set[str] = set()

    for cname, chk in sorted(definition.checks.items()):
        covers = set(chk.covers)
        if chk.status == "active":
            active_covered.update(covers)
        mod_hit = sorted(covers & all_modified)
        rem_hit = sorted(covers & all_removed)
        add_hit = sorted(covers & all_added)
        if rem_hit:
            orphaned_checks.append(
                {
                    "check": cname,
                    "status": chk.status,
                    "removed_criteria": rem_hit,
                }
            )
        if mod_hit or rem_hit or add_hit:
            impacted_check_names.add(cname)
            impacted_checks.append(
                {
                    "check": cname,
                    "status": chk.status,
                    "modified_criteria": mod_hit,
                    "removed_criteria": rem_hit,
                    "added_criteria": add_hit,
                }
            )

    uncovered_added_criteria = sorted(all_added - active_covered)

    impacted_flows: list[dict[str, Any]] = []
    for fname, flow in sorted(definition.flows.items()):
        flow_checks: set[str] = set(flow.delegation.require_checks)
        for step in flow.steps:
            if step.check:
                flow_checks.add(step.check)
            flow_checks.update(step.require_checks)
        direct_hits = sorted(flow_checks & impacted_check_names)
        scope_hit = flow.scope == "all_specs" and bool(all_added or all_removed or all_modified)
        if direct_hits or scope_hit:
            reasons: list[str] = []
            if direct_hits:
                reasons.append("references_impacted_checks")
            if scope_hit:
                reasons.append("all_specs_scope")
            impacted_flows.append(
                {
                    "flow": fname,
                    "scope": flow.scope,
                    "impacted_checks": direct_hits,
                    "reasons": reasons,
                }
            )

    return {
        "schema_version": 1,
        "target": str(root),
        "base_ref": base_ref,
        "base_commit": base_commit,
        "head_ref": head_ref or "working-tree",
        "head_commit": head_commit_sha,
        "has_spec_changes": any_change,
        "summary": {
            "added_count": len(all_added),
            "removed_count": len(all_removed),
            "modified_count": len(all_modified),
            "unchanged_count": len(all_unchanged),
        },
        "specs": spec_diffs,
        "impact": {
            "uncovered_added_criteria": uncovered_added_criteria,
            "orphaned_checks": orphaned_checks,
            "impacted_checks": impacted_checks,
            "impacted_flows": impacted_flows,
            "requires_garden_update": bool(uncovered_added_criteria or orphaned_checks),
        },
    }
