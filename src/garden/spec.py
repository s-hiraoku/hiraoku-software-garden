"""Specification Markdown parser and approval-branch trust anchor verification."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from garden.definition import (
    GARDEN_DEF_REL_PATH,
    DefinitionError,
    GardenDefinition,
    load_definition_bytes,
)
from garden.git_utils import (
    GitError,
    blob_sha_at,
    read_file_at,
    resolve_branch_commit,
)


class SpecError(ValueError):
    """Raised when a specification file violates the required format."""


SPEC_STATUSES = ("draft", "approved", "superseded")
AC_RE = re.compile(r"^-\s+\*\*(AC-\d{3,})\*\*:\s+(.+)$")
INV_RE = re.compile(r"^-\s+\*\*(INV-\d{3,})\*\*:\s+(.+)$")


@dataclass(frozen=True)
class SpecDocument:
    path: str
    spec_id: str
    version: int
    status: str
    title: str
    owner: str | None
    acceptance_criteria: dict[str, str]
    invariants: dict[str, str]

    @property
    def all_criteria_ids(self) -> tuple[str, ...]:
        return tuple(self.acceptance_criteria.keys()) + tuple(self.invariants.keys())


@dataclass(frozen=True)
class ApprovedSpecRecord:
    spec_id: str
    path: str
    version: int
    status: str
    approval_branch: str
    approval_commit: str
    blob_sha: str
    modified_in_head: bool
    document: SpecDocument


@dataclass(frozen=True)
class TrustAnchorResult:
    active_definition: GardenDefinition
    approval_branch: str
    approval_commit: str | None
    approved_definition_blob_sha: str | None
    head_definition_blob_sha: str | None
    evaluation_criteria_changed: bool
    evaluation_change_reasons: tuple[str, ...]
    specs_approved: bool
    spec_failure_reason: str | None
    specs: tuple[ApprovedSpecRecord, ...]
    force_observe_only: bool


def _parse_simple_yaml_frontmatter(lines: list[str], source: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_no, raw_line in enumerate(lines, start=2):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise SpecError(f"{source}:{line_no}: invalid frontmatter line: {raw_line}")
        key, raw_val = stripped.split(":", 1)
        key = key.strip()
        val = raw_val.strip()
        if not key:
            raise SpecError(f"{source}:{line_no}: empty frontmatter key")
        if len(val) >= 2 and (
            (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")
        ):
            val = val[1:-1]
        result[key] = val
    return result


def parse_spec_markdown(text: str, path: str) -> SpecDocument:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SpecError(f"{path}: missing YAML frontmatter opening '---'")
    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise SpecError(f"{path}: missing YAML frontmatter closing '---'")

    fm = _parse_simple_yaml_frontmatter(lines[1:end_idx], path)
    spec_id = fm.get("spec_id", "").strip()
    if not spec_id:
        raise SpecError(f"{path}: frontmatter missing required 'spec_id'")

    version_str = fm.get("version", "").strip()
    if not version_str.isdigit() or int(version_str) < 1:
        raise SpecError(f"{path}: frontmatter 'version' must be a positive integer")
    version = int(version_str)

    status = fm.get("status", "").strip()
    if status not in SPEC_STATUSES:
        raise SpecError(
            f"{path}: frontmatter 'status' must be one of {', '.join(SPEC_STATUSES)}"
        )

    title = fm.get("title", "").strip()
    if not title:
        raise SpecError(f"{path}: frontmatter missing required 'title'")

    owner = fm.get("owner", "").strip() or None

    acceptance_criteria: dict[str, str] = {}
    invariants: dict[str, str] = {}
    for line in lines[end_idx + 1 :]:
        ac_match = AC_RE.match(line.strip())
        if ac_match:
            cid, desc = ac_match.group(1), ac_match.group(2).strip()
            if cid in acceptance_criteria:
                raise SpecError(f"{path}: duplicate acceptance criterion ID '{cid}'")
            acceptance_criteria[cid] = desc
            continue
        inv_match = INV_RE.match(line.strip())
        if inv_match:
            iid, desc = inv_match.group(1), inv_match.group(2).strip()
            if iid in invariants:
                raise SpecError(f"{path}: duplicate invariant ID '{iid}'")
            invariants[iid] = desc

    return SpecDocument(
        path=path,
        spec_id=spec_id,
        version=version,
        status=status,
        title=title,
        owner=owner,
        acceptance_criteria=acceptance_criteria,
        invariants=invariants,
    )


def validate_covers(
    definition: GardenDefinition, specs: tuple[SpecDocument, ...] | list[SpecDocument]
) -> None:
    known_ac: set[str] = set()
    known_inv: set[str] = set()
    for doc in specs:
        known_ac.update(doc.acceptance_criteria.keys())
        known_inv.update(doc.invariants.keys())
    known_all = known_ac | known_inv

    for check in definition.checks.values():
        for cid in check.covers:
            if cid not in known_all:
                raise SpecError(
                    f"checks.{check.name}.covers: criterion '{cid}' not found in referenced specs"
                )
        if check.kind == "formal_check":
            for iid in check.invariants:
                if iid not in known_inv:
                    raise SpecError(
                        f"checks.{check.name}.invariants: invariant '{iid}' not found in referenced specs"
                    )


def verify_trust_anchor(
    repo_root: Path,
    definition_rel_path: str = GARDEN_DEF_REL_PATH,
    *,
    approval_branch: str = "main",
    prefer_remote: bool = False,
) -> TrustAnchorResult:
    """Resolve and validate approved garden definition and specs against a trusted approval_branch.

    Never reads `approval_branch` from the untrusted HEAD definition so a PR cannot
    redirect the Trust Anchor to itself (e.g. `approval_branch = "HEAD"`).
    """
    worktree_def_path = repo_root / definition_rel_path
    if not worktree_def_path.is_file():
        raise DefinitionError(f"{worktree_def_path}: garden definition not found")

    worktree_def_bytes = worktree_def_path.read_bytes()
    head_def = load_definition_bytes(worktree_def_bytes, str(worktree_def_path))
    head_def_blob_sha = blob_sha_at(repo_root, "HEAD", definition_rel_path)

    try:
        _, approval_commit = resolve_branch_commit(
            repo_root, approval_branch, prefer_remote=prefer_remote
        )
    except GitError as error:
        return TrustAnchorResult(
            active_definition=head_def,
            approval_branch=approval_branch,
            approval_commit=None,
            approved_definition_blob_sha=None,
            head_definition_blob_sha=head_def_blob_sha,
            evaluation_criteria_changed=True,
            evaluation_change_reasons=(str(error),),
            specs_approved=False,
            spec_failure_reason=f"approval_branch_unavailable: {error}",
            specs=(),
            force_observe_only=True,
        )

    approved_def_bytes = read_file_at(repo_root, approval_commit, definition_rel_path)
    approved_def_blob_sha = blob_sha_at(repo_root, approval_commit, definition_rel_path)

    change_reasons: list[str] = []
    if approved_def_bytes is None:
        active_definition = head_def
        change_reasons.append("initial_garden_definition_not_on_approval_branch")
    else:
        active_definition = load_definition_bytes(
            approved_def_bytes, f"{approval_branch}:{definition_rel_path}"
        )
        if approved_def_blob_sha != head_def_blob_sha or worktree_def_bytes != approved_def_bytes:
            change_reasons.append("garden_definition_differs_from_approval_branch")

    approved_specs: list[ApprovedSpecRecord] = []
    specs_approved = True
    spec_failure_reason: str | None = None
    force_observe_only = False

    for spec_path in active_definition.spec.paths:
        approved_bytes = read_file_at(repo_root, approval_commit, spec_path)
        approved_sha = blob_sha_at(repo_root, approval_commit, spec_path)
        if approved_bytes is None or approved_sha is None:
            specs_approved = False
            spec_failure_reason = f"spec_not_approved: '{spec_path}' not found on {approval_branch}"
            break
        try:
            doc = parse_spec_markdown(
                approved_bytes.decode("utf-8"), f"{approval_branch}:{spec_path}"
            )
        except (UnicodeDecodeError, SpecError) as error:
            specs_approved = False
            spec_failure_reason = f"spec_invalid: {error}"
            break

        if doc.status != "approved":
            specs_approved = False
            spec_failure_reason = (
                f"spec_not_approved: '{spec_path}' has status='{doc.status}' on {approval_branch}"
            )
            break

        worktree_spec_file = repo_root / spec_path
        worktree_spec_bytes = (
            worktree_spec_file.read_bytes() if worktree_spec_file.is_file() else None
        )
        head_spec_sha = blob_sha_at(repo_root, "HEAD", spec_path)
        modified = (head_spec_sha != approved_sha) or (worktree_spec_bytes != approved_bytes)
        if modified:
            change_reasons.append(f"spec_modified:{spec_path}")
            force_observe_only = True

        approved_specs.append(
            ApprovedSpecRecord(
                spec_id=doc.spec_id,
                path=spec_path,
                version=doc.version,
                status=doc.status,
                approval_branch=approval_branch,
                approval_commit=approval_commit,
                blob_sha=approved_sha,
                modified_in_head=modified,
                document=doc,
            )
        )

    if specs_approved:
        try:
            validate_covers(active_definition, [rec.document for rec in approved_specs])
        except SpecError as error:
            raise DefinitionError(str(error)) from error

    return TrustAnchorResult(
        active_definition=active_definition,
        approval_branch=approval_branch,
        approval_commit=approval_commit,
        approved_definition_blob_sha=approved_def_blob_sha,
        head_definition_blob_sha=head_def_blob_sha,
        evaluation_criteria_changed=bool(change_reasons),
        evaluation_change_reasons=tuple(change_reasons),
        specs_approved=specs_approved,
        spec_failure_reason=spec_failure_reason,
        specs=tuple(approved_specs),
        force_observe_only=force_observe_only,
    )
