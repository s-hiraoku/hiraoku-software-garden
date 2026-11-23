"""Environment, definition, trust-anchor, pstack, and branch-protection diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
from typing import Any

from garden.definition import GARDEN_DEF_REL_PATH, DefinitionError, load_definition
from garden.git_utils import GitError, list_remotes, repo_root
from garden.spec import verify_trust_anchor


GITHUB_REMOTE_RE = re.compile(
    r"(?:https?://github\.com/|git@github\.com:)([^/]+)/([^/.]+?)(?:\.git)?$"
)


@dataclass(frozen=True)
class DiagnosticItem:
    id: str
    status: str  # ok | warn | error
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "status": self.status, "detail": self.detail}


def _nursery_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_pstack_manifest(nursery_root: Path) -> dict[str, Any] | None:
    manifest = nursery_root / "harnesses" / "pstack.toml"
    if not manifest.is_file():
        return None
    try:
        with manifest.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _check_branch_protection(
    remotes: dict[str, str], branch: str, *, skip_network: bool = False
) -> DiagnosticItem:
    if skip_network:
        return DiagnosticItem(
            id="branch_protection",
            status="warn",
            detail=f"skipped remote branch protection check for '{branch}'",
        )
    gh_path = shutil.which("gh")
    if not gh_path:
        return DiagnosticItem(
            id="branch_protection",
            status="warn",
            detail=f"gh CLI not installed; could not verify protection on '{branch}'",
        )
    owner_repo: tuple[str, str] | None = None
    for url in remotes.values():
        match = GITHUB_REMOTE_RE.match(url.strip())
        if match:
            owner_repo = (match.group(1), match.group(2))
            break
    if owner_repo is None:
        return DiagnosticItem(
            id="branch_protection",
            status="warn",
            detail=f"no GitHub remote found; could not verify protection on '{branch}'",
        )

    owner, repo = owner_repo
    proc = subprocess.run(
        [gh_path, "api", f"repos/{owner}/{repo}/branches/{branch}/protection"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode == 0:
        return DiagnosticItem(
            id="branch_protection",
            status="ok",
            detail=f"branch protection enabled on {owner}/{repo}:{branch}",
        )
    return DiagnosticItem(
        id="branch_protection",
        status="warn",
        detail=f"branch protection not confirmed on {owner}/{repo}:{branch}",
    )


def run_doctor(
    target: Path,
    *,
    nursery_root: Path | None = None,
    skip_network: bool = False,
    approval_branch: str = "main",
) -> tuple[int, list[DiagnosticItem]]:
    """Run all 7 diagnostic checks. Returns (exit_code, items)."""
    items: list[DiagnosticItem] = []
    n_root = nursery_root or _nursery_root()

    # 1. Git repository & remote check
    try:
        root = repo_root(target)
        remotes = list_remotes(root)
        if remotes:
            items.append(
                DiagnosticItem(
                    id="git_repo",
                    status="ok",
                    detail=f"git root {root} ({len(remotes)} remote(s))",
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    id="git_repo",
                    status="warn",
                    detail=f"git root {root} has no configured remotes",
                )
            )
    except GitError as error:
        items.append(DiagnosticItem(id="git_repo", status="error", detail=str(error)))
        return 2, items

    # 2. Garden definition (.garden/garden.toml) schema & strictness check
    def_path = root / GARDEN_DEF_REL_PATH
    try:
        definition = load_definition(def_path)
        items.append(
            DiagnosticItem(
                id="garden_definition",
                status="ok",
                detail=f"{GARDEN_DEF_REL_PATH} valid (template={definition.template.name}@v{definition.template.version})",
            )
        )
    except DefinitionError as error:
        items.append(
            DiagnosticItem(id="garden_definition", status="error", detail=str(error))
        )
        return 2, items

    # 3. Specs & approval branch trust anchor
    try:
        anchor = verify_trust_anchor(root, approval_branch=approval_branch)
        if anchor.specs_approved and not anchor.evaluation_criteria_changed:
            items.append(
                DiagnosticItem(
                    id="specs_and_trust",
                    status="ok",
                    detail=f"{len(anchor.specs)} approved spec(s) verified on '{anchor.approval_branch}'",
                )
            )
        elif anchor.specs_approved:
            items.append(
                DiagnosticItem(
                    id="specs_and_trust",
                    status="warn",
                    detail=f"specs approved, but evaluation criteria changed: {', '.join(anchor.evaluation_change_reasons)}",
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    id="specs_and_trust",
                    status="error",
                    detail=anchor.spec_failure_reason or "specs not approved",
                )
            )
    except DefinitionError as error:
        items.append(
            DiagnosticItem(id="specs_and_trust", status="error", detail=str(error))
        )

    # 4. Check command executables exist
    missing_bins: list[str] = []
    for check in definition.checks.values():
        exe = check.command[0]
        if shutil.which(exe) is None and not (root / exe).exists():
            missing_bins.append(f"{check.name}({exe})")
    if missing_bins:
        items.append(
            DiagnosticItem(
                id="check_commands",
                status="error",
                detail=f"executable(s) not found: {', '.join(missing_bins)}",
            )
        )
    else:
        items.append(
            DiagnosticItem(
                id="check_commands",
                status="ok",
                detail=f"all {len(definition.checks)} check executable(s) resolvable",
            )
        )

    # 5. Common harness (pstack) manifest & environment check
    manifest = _load_pstack_manifest(n_root)
    search_paths = (
        (manifest.get("resolution") or {}).get("search_paths", [])
        if isinstance(manifest, dict)
        else []
    )
    pinned_ref = manifest.get("pinned_ref") if isinstance(manifest, dict) else None
    found_paths: list[str] = []
    for raw_p in search_paths:
        p = Path(raw_p).expanduser()
        if not p.is_absolute():
            p = root / p
        if p.exists():
            found_paths.append(str(p))

    if pinned_ref and definition.common_harness.ref != pinned_ref:
        items.append(
            DiagnosticItem(
                id="common_harness",
                status="warn",
                detail=f"pstack ref {definition.common_harness.ref[:12]} differs from nursery pinned_ref {pinned_ref[:12]}",
            )
        )
    elif found_paths:
        items.append(
            DiagnosticItem(
                id="common_harness",
                status="ok",
                detail=f"{definition.common_harness.name} detected at {found_paths[0]}",
            )
        )
    else:
        items.append(
            DiagnosticItem(
                id="common_harness",
                status="warn",
                detail=f"{definition.common_harness.name} not found in default search paths",
            )
        )

    # 6. .garden/.gitignore check
    gi_path = root / ".garden" / ".gitignore"
    if gi_path.is_file():
        gi_lines = {line.strip() for line in gi_path.read_text(encoding="utf-8").splitlines()}
        required_ignores = {"runs/", "worktrees/"}
        missing_ignores = sorted(required_ignores - gi_lines)
        if not missing_ignores:
            items.append(
                DiagnosticItem(
                    id="garden_gitignore",
                    status="ok",
                    detail=".garden/.gitignore excludes runs/ and worktrees/",
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    id="garden_gitignore",
                    status="warn",
                    detail=f".garden/.gitignore missing: {', '.join(missing_ignores)}",
                )
            )
    else:
        items.append(
            DiagnosticItem(
                id="garden_gitignore",
                status="warn",
                detail=".garden/.gitignore not found",
            )
        )

    # 7. Approval branch protection check
    items.append(
        _check_branch_protection(
            remotes, definition.spec.approval_branch, skip_network=skip_network
        )
    )

    has_error = any(item.status == "error" for item in items)
    return (1 if has_error else 0), items
