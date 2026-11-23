#!/usr/bin/env python3
"""Executable verification harness for hiraoku-software-garden CLI.

Creates isolated disposable target repositories, runs the real `garden.py` CLI
as a user would, captures durable proof artifacts outside the disposable
workspace, and tears down only scratch state.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[4]
GARDEN_CLI = REPO_ROOT / "garden.py"
PSTACK_SHA = "7a583913eb1b8d1a0ee76753d1a2cf072e7b54f9"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _commit_all(cwd: Path, message: str) -> str:
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "--quiet", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


def _require_env(name: str) -> Path:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(f"error: {name} environment variable must be set")
    return Path(val).resolve()


def _seed_sample_cli(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "-c", "init.templateDir=", "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.name", "Garden Verify")
    _git(repo, "config", "user.email", "verify@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", "https://example.invalid/sample-cli.git")

    (repo / "src").mkdir()
    (repo / "scripts").mkdir()
    (repo / "formal").mkdir()
    (repo / "docs" / "specs").mkdir(parents=True)
    (repo / ".garden").mkdir()

    (repo / "pyproject.toml").write_text(
        '[project]\nname = "sample-cli"\n[project.scripts]\nsample-cli = "sample_cli:main"\n',
        encoding="utf-8",
    )
    (repo / "src" / "app.txt").write_text("OK\n", encoding="utf-8")
    (repo / "formal" / "model.qnt").write_text("module model {}\n", encoding="utf-8")

    (repo / "scripts" / "unit.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "val = Path('src/app.txt').read_text(encoding='utf-8').strip()\n"
        "if val == 'OK':\n"
        "    print('unit ok')\n"
        "    sys.exit(0)\n"
        "if val == 'TEMPFAIL':\n"
        "    print('env unavailable', file=sys.stderr)\n"
        "    sys.exit(75)\n"
        "print(f'expected OK, got {val}', file=sys.stderr)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    (repo / "scripts" / "e2e.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "ev = Path(os.environ['GARDEN_EVIDENCE_DIR']) / 'transcript.json'\n"
        "ev.write_text(json.dumps({'observed': 'ok'}), encoding='utf-8')\n"
        "print('e2e ok')\n",
        encoding="utf-8",
    )
    (repo / "scripts" / "formal.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "ev = Path(os.environ['GARDEN_EVIDENCE_DIR']) / 'formal.json'\n"
        "ev.write_text(json.dumps({\n"
        "    'schema_version': 1,\n"
        "    'tool': 'quint',\n"
        "    'model_path': 'formal/model.qnt',\n"
        "    'invariants': ['INV-001'],\n"
        "    'assumptions': ['n=2'],\n"
        "    'scope': 'bounded steps=5',\n"
        "    'completed': True,\n"
        "    'result': 'proved',\n"
        "}), encoding='utf-8')\n"
        "print('formal proved')\n",
        encoding="utf-8",
    )
    (repo / "scripts" / "mock_agent.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "wt = Path(os.environ['GARDEN_WORKTREE'])\n"
        "mode = (wt / 'src' / 'agent_mode.txt').read_text(encoding='utf-8').strip() if (wt / 'src' / 'agent_mode.txt').exists() else 'fix'\n"
        "res_path = Path(os.environ['GARDEN_AGENT_RESPONSE'])\n"
        "if mode == 'question':\n"
        "    res_path.write_text(json.dumps({\n"
        "        'schema_version': 1,\n"
        "        'outcome': 'needs_spec_decision',\n"
        "        'summary': 'behavior undefined on conflict',\n"
        "        'question': {'prompt': 'Keep latest or merge?', 'options': ['latest', 'merge'], 'impact': 'data loss risk', 'blocked_work': 'fix_unit'},\n"
        "    }), encoding='utf-8')\n"
        "elif mode == 'tamper':\n"
        "    (wt / 'docs' / 'specs' / 'sample-cli.md').write_text('tampered\\n', encoding='utf-8')\n"
        "    res_path.write_text(json.dumps({'schema_version': 1, 'outcome': 'completed', 'summary': 'done', 'changed_files': []}), encoding='utf-8')\n"
        "else:\n"
        "    (wt / 'src' / 'app.txt').write_text('OK\\n', encoding='utf-8')\n"
        "    res_path.write_text(json.dumps({\n"
        "        'schema_version': 1,\n"
        "        'outcome': 'completed',\n"
        "        'summary': 'restored src/app.txt to OK',\n"
        "        'changed_files': ['src/app.txt'],\n"
        "        'usage': {'input_tokens': 120, 'output_tokens': 40, 'cost_usd': 0.02},\n"
        "    }), encoding='utf-8')\n",
        encoding="utf-8",
    )

    (repo / "docs" / "specs" / "sample-cli.md").write_text(
        "---\n"
        "spec_id: sample-cli\n"
        "version: 1\n"
        "status: approved\n"
        "title: Sample CLI Verification Spec\n"
        "---\n\n"
        "## 受け入れ条件\n"
        "- **AC-001**: unit チェックが正常終了する。\n"
        "- **AC-002**: e2e チェックが証拠 transcript.json を出力する。\n\n"
        "## 不変条件\n"
        "- **INV-001**: 形式検証モデルが不変条件を満たす。\n",
        encoding="utf-8",
    )
    (repo / ".garden" / ".gitignore").write_text("runs/\nworktrees/\n", encoding="utf-8")

    py = json.dumps(sys.executable)
    (repo / ".garden" / "garden.toml").write_text(
        f"schema_version = 1\n\n"
        f"[template]\nname = \"cli\"\nversion = 1\n\n"
        f"[common_harness]\nname = \"pstack\"\nref = \"{PSTACK_SHA}\"\n\n"
        f"[target_harness]\nname = \"sample-cli-harness\"\nversion = 1\n\n"
        f"[spec]\npaths = [\"docs/specs/sample-cli.md\"]\napproval_branch = \"main\"\n\n"
        f"[delegation]\nmode = \"propose_pr\"\nallowed_paths = [\"src/**\"]\n"
        f"forbidden_paths = [\".github/workflows/**\"]\nmax_retries = 2\n"
        f"max_duration_sec = 120\nmax_cost_usd = 2.0\nrequire_checks = [\"unit\"]\n\n"
        f"[checks.unit]\nkind = \"check\"\ncommand = [{py}, \"scripts/unit.py\"]\n"
        f"timeout_sec = 30\ncovers = [\"AC-001\"]\ninconclusive_exit_codes = [75]\n\n"
        f"[checks.e2e]\nkind = \"check\"\ncommand = [{py}, \"scripts/e2e.py\"]\n"
        f"timeout_sec = 30\ncovers = [\"AC-002\"]\nrequired_evidence = [\"transcript.json\"]\n\n"
        f"[checks.formal_inv]\nkind = \"formal_check\"\ntool = \"quint\"\n"
        f"model_path = \"formal/model.qnt\"\ncommand = [{py}, \"scripts/formal.py\"]\n"
        f"timeout_sec = 30\ncovers = [\"INV-001\"]\ninvariants = [\"INV-001\"]\n\n"
        f"[agents.default]\nadapter = \"mock\"\ncommand = [{py}, \"scripts/mock_agent.py\"]\n"
        f"timeout_sec = 30\nmax_cost_usd = 1.0\n\n"
        f"[flows.verify_all]\nscope = \"all_specs\"\n"
        f"[flows.verify_all.delegation]\nmode = \"observe_only\"\nallowed_paths = []\nmax_retries = 0\n"
        f"[[flows.verify_all.steps]]\nid = \"s_unit\"\ntype = \"check\"\ncheck = \"unit\"\non_fail = \"escalate\"\n"
        f"[[flows.verify_all.steps]]\nid = \"final_gate\"\ntype = \"gate\"\nrequire_checks = [\"unit\", \"e2e\", \"formal_inv\"]\n"
        f"[[flows.verify_all.steps]]\nid = \"rep\"\ntype = \"report\"\nkind = \"auto\"\n\n"
        f"[flows.verify_and_fix]\nscope = \"all_specs\"\n"
        f"[[flows.verify_and_fix.steps]]\nid = \"s_unit\"\ntype = \"check\"\ncheck = \"unit\"\n"
        f"on_fail = \"fix_and_retry\"\nfix_step = \"fix_unit\"\nmax_retries = 2\n"
        f"[[flows.verify_and_fix.steps]]\nid = \"fix_unit\"\ntype = \"agent\"\nagent = \"default\"\nrun_on_demand = true\n"
        f"[[flows.verify_and_fix.steps]]\nid = \"final_gate\"\ntype = \"gate\"\nrequire_checks = [\"unit\", \"e2e\", \"formal_inv\"]\n",
        encoding="utf-8",
    )
    _commit_all(repo, "seed sample-cli")


def _seed_fresh_cli(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "-c", "init.templateDir=", "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.name", "Garden Verify")
    _git(repo, "config", "user.email", "verify@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", "https://example.invalid/fresh-cli.git")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "fresh-cli"\n[project.scripts]\nfresh-cli = "fresh:main"\n',
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# fresh-cli\n", encoding="utf-8")
    _commit_all(repo, "seed uninitialized cli repo")


def cmd_launch(_args: argparse.Namespace) -> int:
    verify_dir = _require_env("GARDEN_VERIFY_DIR")
    proof_dir = _require_env("GARDEN_PROOF_DIR")
    if verify_dir == proof_dir or proof_dir in verify_dir.parents or verify_dir in proof_dir.parents:
        raise SystemExit("error: GARDEN_VERIFY_DIR and GARDEN_PROOF_DIR must be disjoint directories")

    if verify_dir.exists():
        shutil.rmtree(verify_dir)
    verify_dir.mkdir(parents=True, exist_ok=True)
    proof_dir.mkdir(parents=True, exist_ok=True)

    sample_repo = verify_dir / "sample-cli"
    fresh_repo = verify_dir / "fresh-cli"
    _seed_sample_cli(sample_repo)
    _seed_fresh_cli(fresh_repo)

    py = json.dumps(sys.executable)
    catalog_path = verify_dir / "catalog.toml"
    catalog_path.write_text(
        "[repositories.sample-cli]\n"
        'path = "sample-cli"\n'
        'remote = "https://example.invalid/sample-cli.git"\n'
        'description = "Disposable seeded CLI target repo"\n'
        f'check = [{py}, "scripts/unit.py"]\n',
        encoding="utf-8",
    )

    info = {
        "nursery_root": str(REPO_ROOT),
        "nursery_commit": _git(REPO_ROOT, "rev-parse", "HEAD"),
        "verify_dir": str(verify_dir),
        "proof_dir": str(proof_dir),
        "sample_repo": str(sample_repo),
        "fresh_repo": str(fresh_repo),
        "catalog": str(catalog_path),
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    verify_dir = _require_env("GARDEN_VERIFY_DIR")
    proof_dir = _require_env("GARDEN_PROOF_DIR")
    checks = {
        "python_version": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 11),
        "git_available": shutil.which("git") is not None,
        "garden_cli_exists": GARDEN_CLI.is_file(),
        "nursery_commit": _git(REPO_ROOT, "rev-parse", "HEAD"),
        "verify_dir_exists": verify_dir.is_dir(),
        "sample_repo_ready": (verify_dir / "sample-cli" / ".garden" / "garden.toml").is_file(),
        "fresh_repo_ready": (verify_dir / "fresh-cli" / "pyproject.toml").is_file(),
        "catalog_ready": (verify_dir / "catalog.toml").is_file(),
        "proof_dir_writable": proof_dir.is_dir() and os.access(proof_dir, os.W_OK),
    }
    ok = all(
        checks[k]
        for k in (
            "python_ok",
            "git_available",
            "garden_cli_exists",
            "verify_dir_exists",
            "sample_repo_ready",
            "fresh_repo_ready",
            "catalog_ready",
            "proof_dir_writable",
        )
    )
    print(json.dumps({"healthy": ok, "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_cli(args: argparse.Namespace) -> int:
    proof_dir = _require_env("GARDEN_PROOF_DIR")
    proof_dir.mkdir(parents=True, exist_ok=True)
    cli_args = list(args.cli_args)
    if cli_args and cli_args[0] == "--":
        cli_args = cli_args[1:]
    if not cli_args:
        raise SystemExit("error: provide arguments to pass to garden.py after --")

    cmd = [sys.executable, str(GARDEN_CLI), *cli_args]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False)

    label = args.label or "cli"
    record = {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "feature": args.feature or "",
        "label": label,
        "command": cmd,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
    out_file = proof_dir / f"{label}.json"
    out_file.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def cmd_snapshot(args: argparse.Namespace) -> int:
    proof_dir = _require_env("GARDEN_PROOF_DIR")
    src = Path(args.source).resolve()
    dst = proof_dir / args.name
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    if src.is_dir():
        shutil.copytree(src, dst)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    else:
        raise SystemExit(f"error: snapshot source does not exist: {src}")
    print(json.dumps({"snapshot_saved": str(dst)}, ensure_ascii=False))
    return 0


def cmd_mutate(args: argparse.Namespace) -> int:
    verify_dir = _require_env("GARDEN_VERIFY_DIR")
    sample_repo = verify_dir / "sample-cli"
    if args.action == "set-app":
        (sample_repo / "src" / "app.txt").write_text(f"{args.value}\n", encoding="utf-8")
        commit = _commit_all(sample_repo, f"set app={args.value}")
        print(json.dumps({"mutated": "set-app", "value": args.value, "commit": commit}))
        return 0
    if args.action == "set-agent-mode":
        (sample_repo / "src" / "agent_mode.txt").write_text(f"{args.value}\n", encoding="utf-8")
        commit = _commit_all(sample_repo, f"set agent_mode={args.value}")
        print(json.dumps({"mutated": "set-agent-mode", "value": args.value, "commit": commit}))
        return 0
    if args.action == "set-spec-status":
        sp = sample_repo / "docs" / "specs" / "sample-cli.md"
        text = sp.read_text(encoding="utf-8")
        for s in ("approved", "draft", "superseded"):
            text = text.replace(f"status: {s}", f"status: {args.value}")
        sp.write_text(text, encoding="utf-8")
        commit = _commit_all(sample_repo, f"set spec status={args.value}")
        print(json.dumps({"mutated": "set-spec-status", "value": args.value, "commit": commit}))
        return 0
    raise SystemExit(f"unknown mutate action: {args.action}")


def cmd_cleanup(_args: argparse.Namespace) -> int:
    verify_dir = _require_env("GARDEN_VERIFY_DIR")
    proof_dir = _require_env("GARDEN_PROOF_DIR")
    removed = False
    if verify_dir.exists():
        shutil.rmtree(verify_dir)
        removed = True
    proof_files = sorted(str(p.relative_to(proof_dir)) for p in proof_dir.rglob("*") if p.is_file()) if proof_dir.exists() else []
    print(
        json.dumps(
            {
                "verify_dir_removed": removed,
                "verify_dir_exists": verify_dir.exists(),
                "proof_dir": str(proof_dir),
                "proof_files_retained": len(proof_files),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verification harness for hiraoku-software-garden.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("launch", help="create disposable target repos and catalog")
    sub.add_parser("doctor", help="read-only verification environment health check")

    cli_p = sub.add_parser("cli", help="run real garden.py CLI and record proof")
    cli_p.add_argument("--feature", default="")
    cli_p.add_argument("--label", default="cli")
    cli_p.add_argument("cli_args", nargs=argparse.REMAINDER)

    snap_p = sub.add_parser("snapshot", help="copy generated file or directory into GARDEN_PROOF_DIR")
    snap_p.add_argument("source")
    snap_p.add_argument("name")

    mut_p = sub.add_parser("mutate", help="mutate disposable sample-cli fixture for negative scenarios")
    mut_p.add_argument("action", choices=("set-app", "set-agent-mode", "set-spec-status"))
    mut_p.add_argument("value")

    sub.add_parser("cleanup", help="remove GARDEN_VERIFY_DIR while preserving GARDEN_PROOF_DIR")

    args = parser.parse_args()
    handlers = {
        "launch": cmd_launch,
        "doctor": cmd_doctor,
        "cli": cmd_cli,
        "snapshot": cmd_snapshot,
        "mutate": cmd_mutate,
        "cleanup": cmd_cleanup,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
