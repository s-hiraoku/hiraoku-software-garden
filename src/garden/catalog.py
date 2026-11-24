"""Nursery catalog (garden.toml) loader and check runner."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

from garden.git_utils import GitError, git_output

_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


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


def _slugify_repo_id(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-_")
    if not slug or not slug[0].isalnum():
        raise ValueError(f"cannot derive valid repository ID from {name!r}; specify --id")
    return slug


def _format_repository_block(repo_id: str, entry: dict) -> str:
    cmd_toml = ", ".join(json.dumps(arg, ensure_ascii=False) for arg in entry["check"])
    return (
        f"[repositories.{repo_id}]\n"
        f"path = {json.dumps(entry['path'], ensure_ascii=False)}\n"
        f"remote = {json.dumps(entry['remote'], ensure_ascii=False)}\n"
        f"description = {json.dumps(entry['description'], ensure_ascii=False)}\n"
        f"check = [{cmd_toml}]\n"
    )


def register_repository(
    catalog_path: Path,
    workspace: Path,
    target_path: Path,
    *,
    repo_id: str | None = None,
    description: str | None = None,
    remote: str | None = None,
    check_command: list[str] | None = None,
    force: bool = False,
) -> dict:
    """Register a target Git repository into the Nursery garden.toml catalog."""
    target = target_path.resolve()
    if not target.is_dir():
        raise ValueError(f"target directory does not exist: {target}")

    try:
        root = Path(git_output(target, "rev-parse", "--show-toplevel")).resolve()
    except GitError as exc:
        raise ValueError(f"target is not a Git repository: {exc}") from exc

    resolved_id = repo_id.strip() if repo_id else _slugify_repo_id(root.name)
    if not _REPO_ID_RE.match(resolved_id):
        raise ValueError(
            f"invalid repo id {resolved_id!r}: must match ^[A-Za-z0-9][A-Za-z0-9_-]*$"
        )

    if remote is not None:
        resolved_remote = remote.strip()
    else:
        remotes = [r for r in git_output(root, "remote").splitlines() if r.strip()]
        if not remotes:
            raise ValueError(
                f"no Git remote configured in {root}; pass --remote explicitly"
            )
        chosen = "origin" if "origin" in remotes else remotes[0]
        resolved_remote = git_output(root, "remote", "get-url", "--", chosen)
    if not resolved_remote:
        raise ValueError("remote URL must be non-empty")

    if check_command:
        resolved_check = list(check_command)
    elif (root / ".garden" / "garden.toml").is_file():
        nursery_garden_py = catalog_path.resolve().parent / "garden.py"
        if nursery_garden_py.is_file():
            rel_garden_py = Path(os.path.relpath(nursery_garden_py, root)).as_posix()
            resolved_check = [
                "python3",
                rel_garden_py,
                "doctor",
                "--target",
                ".",
                "--skip-network",
            ]
        else:
            resolved_check = ["garden", "doctor", "--target", ".", "--skip-network"]
    else:
        raise ValueError(
            "pass --check <cmd>... or run 'garden init' in the target repository first"
        )

    resolved_desc = (description or f"Target repository {root.name}").strip()
    rel_path = Path(os.path.relpath(root, workspace.resolve())).as_posix()

    existing = read_catalog(catalog_path)
    if resolved_id in existing and not force:
        raise ValueError(
            f"repository '{resolved_id}' is already registered in {catalog_path} (use --force to overwrite)"
        )

    entry = {
        "path": rel_path,
        "remote": resolved_remote,
        "description": resolved_desc,
        "check": resolved_check,
    }

    updated_repos = dict(existing)
    updated_repos[resolved_id] = entry

    if not updated_repos:
        rendered = "[repositories]\n"
    else:
        blocks = [
            _format_repository_block(rid, data)
            for rid, data in updated_repos.items()
        ]
        rendered = "\n".join(blocks)

    # Validate generated TOML in memory before overwriting catalog_path
    tomllib.loads(rendered)
    catalog_path.write_text(rendered, encoding="utf-8")
    read_catalog(catalog_path)
    return {"repo": resolved_id, "catalog": str(catalog_path.resolve()), "entry": entry}
