"""Post-execution deterministic guard enforcing delegation boundaries against <target_commit>."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from garden.definition import DelegationConfig
from garden.git_utils import git_run


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    violations: tuple[str, ...]
    changed_files: tuple[str, ...]


def _strip_dot_slash(path_str: str) -> str:
    norm = path_str.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    norm = _strip_dot_slash(pattern)
    parts: list[str] = ["^"]
    i = 0
    n = len(norm)
    while i < n:
        ch = norm[i]
        if ch == "*":
            if i + 1 < n and norm[i + 1] == "*":
                if i + 2 < n and norm[i + 2] == "/":
                    parts.append("(?:.*/)?")
                    i += 3
                else:
                    parts.append(".*")
                    i += 2
            else:
                parts.append("[^/]*")
                i += 1
        elif ch == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(ch))
            i += 1
    parts.append("$")
    return re.compile("".join(parts))


def match_glob(path_str: str, pattern: str) -> bool:
    norm = _strip_dot_slash(path_str)
    return _glob_to_regex(pattern).match(norm) is not None


def _hash_path(path: Path) -> str:
    if path.is_symlink():
        return "symlink:" + hashlib.sha256(str(path.readlink()).encode("utf-8")).hexdigest()
    if path.is_file():
        return "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return "other"


def snapshot_untracked_files(worktree: Path) -> dict[str, str]:
    """Return {rel_path: content_digest} for all untracked files in worktree."""
    status = git_run(
        worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all", text=False
    )
    untracked: dict[str, str] = {}
    if status.returncode == 0 and status.stdout:
        for raw_entry in status.stdout.split(b"\x00"):
            if raw_entry.startswith(b"?? "):
                rel = raw_entry[3:].decode("utf-8", errors="replace")
                untracked[rel] = _hash_path(worktree / rel)
    return untracked


def collect_changes_since(
    worktree: Path,
    target_commit: str,
    *,
    baseline_untracked: dict[str, str] | None = None,
    ignore_prefixes: tuple[str, ...] = (),
) -> tuple[bool, tuple[str, ...]]:
    """Return (ancestor_ok, all_changed_and_untracked_paths) since target_commit."""
    anc = git_run(worktree, "merge-base", "--is-ancestor", target_commit, "HEAD")
    if anc.returncode != 0:
        return False, ()

    # Diff against target_commit without rename detection covers committed + staged + unstaged edits
    diff = git_run(worktree, "diff", "--name-only", "--no-renames", "-z", target_commit, text=False)
    changed: list[str] = []
    if diff.returncode == 0 and diff.stdout:
        for raw_entry in diff.stdout.split(b"\x00"):
            if raw_entry:
                changed.append(raw_entry.decode("utf-8", errors="replace"))

    baseline = baseline_untracked or {}
    current_untracked = snapshot_untracked_files(worktree)
    for rel_path in sorted(current_untracked.keys()):
        if rel_path in baseline and baseline[rel_path] == current_untracked[rel_path]:
            continue
        norm = _strip_dot_slash(rel_path)
        if any(norm.startswith(prefix) for prefix in ignore_prefixes):
            continue
        changed.append(rel_path)

    return True, tuple(dict.fromkeys(changed))


def verify_post_agent_guard(
    worktree: Path,
    target_commit: str,
    delegation: DelegationConfig,
    *,
    step_cost_usd: float = 0.0,
    agent_max_cost_usd: float | None = None,
    cumulative_cost_usd: float = 0.0,
    elapsed_sec: float = 0.0,
    baseline_untracked: dict[str, str] | None = None,
    ignore_prefixes: tuple[str, ...] = (),
) -> GuardResult:
    violations: list[str] = []

    ancestor_ok, changed_files = collect_changes_since(
        worktree,
        target_commit,
        baseline_untracked=baseline_untracked,
        ignore_prefixes=ignore_prefixes,
    )
    if not ancestor_ok:
        violations.append(
            f"history_rewritten: target_commit {target_commit[:12]} is no longer an ancestor of HEAD"
        )

    if delegation.mode == "observe_only" and changed_files:
        violations.append(
            f"observe_only_violation: modified files in observe_only mode: {', '.join(changed_files)}"
        )

    for rel_path in changed_files:
        for pattern in delegation.forbidden_paths:
            if match_glob(rel_path, pattern):
                violations.append(
                    f"forbidden_path_violation: '{rel_path}' matches forbidden pattern '{pattern}'"
                )
                break

        if delegation.allowed_paths:
            if not any(match_glob(rel_path, pat) for pat in delegation.allowed_paths):
                violations.append(
                    f"allowed_path_violation: '{rel_path}' is outside allowed_paths"
                )

    remaining_before_step = max(0.0, delegation.max_cost_usd - (cumulative_cost_usd - step_cost_usd))
    effective_step_cap = (
        min(agent_max_cost_usd, remaining_before_step)
        if agent_max_cost_usd is not None
        else remaining_before_step
    )
    if agent_max_cost_usd is not None and step_cost_usd > agent_max_cost_usd:
        violations.append(
            f"agent_cost_limit_exceeded: step cost ${step_cost_usd:.4f} > agent max ${agent_max_cost_usd:.4f}"
        )
    if step_cost_usd > effective_step_cap + 1e-9 or cumulative_cost_usd > delegation.max_cost_usd + 1e-9:
        violations.append(
            f"flow_cost_limit_exceeded: cumulative cost ${cumulative_cost_usd:.4f} > max ${delegation.max_cost_usd:.4f}"
        )

    if elapsed_sec > delegation.max_duration_sec:
        violations.append(
            f"duration_limit_exceeded: elapsed {elapsed_sec:.1f}s > max {delegation.max_duration_sec}s"
        )

    return GuardResult(
        ok=not violations,
        violations=tuple(violations),
        changed_files=changed_files,
    )
