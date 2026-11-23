"""Subprocess helpers for querying Git repositories deterministically."""

from __future__ import annotations

from pathlib import Path
import subprocess


class GitError(RuntimeError):
    """Raised when a Git command fails."""


def git_run(path: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=text,
        check=False,
    )


def git_output(path: Path, *args: str) -> str:
    result = git_run(path, *args, text=True)
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def git_bytes(path: Path, *args: str) -> bytes:
    result = git_run(path, *args, text=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(stderr or f"git {' '.join(args)} failed")
    return result.stdout


def try_git_output(path: Path, *args: str) -> str | None:
    result = git_run(path, *args, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def repo_root(path: Path) -> Path:
    return Path(git_output(path, "rev-parse", "--show-toplevel")).resolve()


def head_commit(path: Path) -> str:
    return git_output(path, "rev-parse", "HEAD")


def is_dirty(path: Path) -> bool:
    return bool(git_output(path, "status", "--porcelain", "--untracked-files=normal"))


def list_remotes(path: Path) -> dict[str, str]:
    names = git_output(path, "remote").splitlines()
    remotes: dict[str, str] = {}
    for name in names:
        if not name:
            continue
        url = try_git_output(path, "remote", "get-url", "--", name)
        if url is not None:
            remotes[name] = url
    return remotes


def resolve_branch_commit(path: Path, branch: str) -> tuple[str, str]:
    """Resolve an approval branch name strictly to refs/heads/<branch> or refs/remotes/origin/<branch>.

    Rejects raw symbolic refs (such as HEAD) or path traversal so an untrusted
    branch cannot spoof the Trust Anchor.
    """
    if (
        not branch
        or branch == "HEAD"
        or branch.startswith("refs/")
        or ".." in branch
        or any(ch.isspace() for ch in branch)
    ):
        raise GitError(f"invalid approval branch name '{branch}'")
    candidates = [
        f"refs/heads/{branch}",
        f"refs/remotes/origin/{branch}",
    ]
    for candidate in candidates:
        sha = try_git_output(path, "rev-parse", "--verify", f"{candidate}^{{commit}}")
        if sha:
            return candidate, sha
    raise GitError(f"approval branch '{branch}' could not be resolved")


def blob_sha_at(path: Path, rev: str, rel_path: str) -> str | None:
    return try_git_output(path, "rev-parse", "--verify", f"{rev}:{rel_path}")


def read_file_at(path: Path, rev: str, rel_path: str) -> bytes | None:
    result = git_run(path, "show", f"{rev}:{rel_path}", text=False)
    if result.returncode != 0:
        return None
    return result.stdout
