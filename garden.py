"""Run existing checks in target repositories from the Nursery repository."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tomllib


def read_catalog(path):
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


def git_output(path, *args):
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def check_repository(repo_id, entry, workspace):
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
    except subprocess.CalledProcessError as error:
        return result | {"status": "unavailable", "detail": error.stderr.strip()}
    except (OSError, ValueError) as error:
        return result | {"status": "unavailable", "detail": str(error)}

    return result | {
        "status": "passed" if run.returncode == 0 else "failed",
        "exit_code": run.returncode,
    }


def main():
    home = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=home / "garden.toml")
    parser.add_argument("--workspace", type=Path, default=home.parent)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="list registered repositories")
    check = commands.add_parser("check", help="run the checks for selected repositories")
    check.add_argument("repo", nargs="+", help="repository ID from the catalog")
    args = parser.parse_args()

    try:
        repositories = read_catalog(args.catalog)
    except (OSError, ValueError) as error:
        parser.error(f"{args.catalog}: {error}")

    if args.action == "list":
        print(json.dumps(repositories, ensure_ascii=False, indent=2))
        return 0

    if not repositories:
        parser.error(f"{args.catalog}: no repositories registered; add a [repositories.<id>] entry")

    unknown = [repo_id for repo_id in args.repo if repo_id not in repositories]
    if unknown:
        parser.error("unknown repositories: " + ", ".join(unknown))

    results = [
        check_repository(repo_id, repositories[repo_id], args.workspace)
        for repo_id in dict.fromkeys(args.repo)
    ]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return int(any(result["status"] != "passed" for result in results))


if __name__ == "__main__":
    sys.exit(main())
