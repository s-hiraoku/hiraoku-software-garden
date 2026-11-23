"""Nursery catalog (garden.toml) loader and check runner."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tomllib

from garden.git_utils import GitError, git_output


def read_catalog(path: Path) -> dict[str, dict]:
    with path.open("rb") as file:
        repositories = tomllib.load(file).get("repositories")
    if not isinstance(repositories, dict):
        raise ValueError("catalog needs a [repositories] table")
    for repo_id, entry in repositories.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{repo_id}: expected a repository table")
        for field in ("path", "remote", "description"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f"{repo_id}: {field} must be a non-empty string")
        command = entry.get("check")
        if not isinstance(command, list) or not command or not all(
            isinstance(arg, str) and arg for arg in command
        ):
            raise ValueError(f"{repo_id}: check must be a non-empty array of strings")
    return repositories


def check_repository(repo_id: str, entry: dict, workspace: Path) -> dict:
    path = (workspace / entry["path"]).resolve()
    result = {"repo": repo_id, "path": str(path), "command": entry["check"]}
    try:
        root = Path(git_output(path, "rev-parse", "--show-toplevel")).resolve()
        if root != path:
            raise ValueError(f"expected checkout root, found {root}")
        remotes = git_output(path, "remote").splitlines()
        if not any(
            git_output(path, "remote", "get-url", "--", remote) == entry["remote"]
            for remote in remotes
        ):
            raise ValueError("no Git fetch URL matches the catalog remote")
        result["commit"] = git_output(path, "rev-parse", "HEAD")
        result["dirty"] = bool(
            git_output(path, "status", "--porcelain", "--untracked-files=normal")
        )
        print(f"[{repo_id}] {path}", file=sys.stderr, flush=True)
        run = subprocess.run(
            entry["check"], cwd=path, stdout=sys.stderr, check=False
        )
    except GitError as error:
        return result | {"status": "unavailable", "detail": str(error)}
    except (OSError, ValueError) as error:
        return result | {"status": "unavailable", "detail": str(error)}

    return result | {
        "status": "passed" if run.returncode == 0 else "failed",
        "exit_code": run.returncode,
    }
