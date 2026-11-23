"""Unified CLI entry point for Software Garden (catalog, diagnostics, execution, templates)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from garden.catalog import check_repository, read_catalog
from garden.definition import DefinitionError
from garden.doctor import run_doctor
from garden.engine import run_flow
from garden.feedback import VALID_CATEGORIES, VALID_STATUSES, add_intervention, summarize_interventions
from garden.git_utils import GitError
from garden.report import format_report, post_github_report
from garden.templates import apply_upgrade, init_garden, plan_upgrade


def _default_nursery_home() -> Path:
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    home = _default_nursery_home()
    parser = argparse.ArgumentParser(
        prog="garden",
        description="Run Nursery catalog checks and deterministic Garden workflows.",
    )
    parser.add_argument("--catalog", type=Path, default=home / "garden.toml")
    parser.add_argument("--workspace", type=Path, default=home.parent)
    commands = parser.add_subparsers(dest="action", required=True)

    # Existing catalog subcommands (100% backwards-compatible)
    commands.add_parser("list", help="list registered repositories")
    check = commands.add_parser("check", help="run the checks for selected repositories")
    check.add_argument("repo", nargs="+", help="repository ID from the catalog")

    # Target-repository onboarding & diagnostics
    init_cmd = commands.add_parser("init", help="initialize a target repository as a Garden")
    init_cmd.add_argument("--target", type=Path, default=Path.cwd())
    init_cmd.add_argument("--type", dest="template_type", default=None)
    init_cmd.add_argument("--version", type=int, default=None)
    init_cmd.add_argument("--force", action="store_true")

    doc_cmd = commands.add_parser("doctor", help="diagnose a target repository Garden setup")
    doc_cmd.add_argument("--target", type=Path, default=Path.cwd())
    doc_cmd.add_argument("--json", action="store_true", dest="as_json")
    doc_cmd.add_argument("--skip-network", action="store_true")

    upg_cmd = commands.add_parser("upgrade", help="plan or apply a template upgrade safely")
    upg_cmd.add_argument("--target", type=Path, default=Path.cwd())
    upg_cmd.add_argument("--to", dest="to_version", type=int, default=None)
    upg_cmd.add_argument("--apply", action="store_true")

    # Deterministic flow execution
    run_cmd = commands.add_parser("run", help="execute a Garden flow deterministically")
    run_cmd.add_argument("flow", help="flow name defined in .garden/garden.toml")
    run_cmd.add_argument("--target", type=Path, default=Path.cwd())
    run_cmd.add_argument("--trigger-event", default="manual")
    run_cmd.add_argument("--trigger-id", default="manual")
    run_cmd.add_argument("--issue", type=int, default=None)
    run_cmd.add_argument("--force", action="store_true")
    run_cmd.add_argument("--keep-worktree", action="store_true")
    run_cmd.add_argument("--no-worktree", action="store_true")
    run_cmd.add_argument("--include-proposed", action="store_true")
    run_cmd.add_argument("--json", action="store_true", dest="as_json")

    # Reporting & GitHub handoff
    rep_cmd = commands.add_parser("report", help="render or post a run summary report")
    rep_cmd.add_argument("run_dir", type=Path)
    rep_cmd.add_argument("--format", dest="fmt", choices=("md", "json"), default="md")
    rep_cmd.add_argument("--post-github-issue", type=int, default=None)

    # Phase-7 intervention feedback
    fb_cmd = commands.add_parser("feedback", help="record or summarize human interventions")
    fb_sub = fb_cmd.add_subparsers(dest="fb_action", required=True)
    fb_add = fb_sub.add_parser("add", help="record a human intervention")
    fb_add.add_argument("--target", type=Path, default=Path.cwd())
    fb_add.add_argument("--run-id", required=True)
    fb_add.add_argument("--category", choices=VALID_CATEGORIES, required=True)
    fb_add.add_argument("--summary", required=True)
    fb_add.add_argument("--triage-step", action="append", default=[])
    fb_add.add_argument("--preventive-action", default="")
    fb_add.add_argument("--preventive-check-ref", default="")
    fb_add.add_argument("--status", choices=VALID_STATUSES, default="open")

    fb_sum = fb_sub.add_parser("summary", help="summarize recorded interventions")
    fb_sum.add_argument("--target", type=Path, default=Path.cwd())

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.action in ("list", "check"):
        try:
            repositories = read_catalog(args.catalog)
        except (OSError, ValueError) as error:
            parser.error(f"{args.catalog}: {error}")

        if args.action == "list":
            print(json.dumps(repositories, ensure_ascii=False, indent=2))
            return 0

        if not repositories:
            parser.error(
                f"{args.catalog}: no repositories registered; add a [repositories.<id>] entry"
            )
        unknown = [repo_id for repo_id in args.repo if repo_id not in repositories]
        if unknown:
            parser.error("unknown repositories: " + ", ".join(unknown))

        results = [
            check_repository(repo_id, repositories[repo_id], args.workspace)
            for repo_id in dict.fromkeys(args.repo)
        ]
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return int(any(result["status"] != "passed" for result in results))

    if args.action == "init":
        try:
            result = init_garden(
                args.target,
                template_type=args.template_type,
                version=args.version,
                force=args.force,
            )
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.action == "doctor":
        code, items = run_doctor(args.target, skip_network=args.skip_network)
        if args.as_json:
            print(
                json.dumps(
                    {"exit_code": code, "items": [i.to_dict() for i in items]},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            for item in items:
                print(f"[{item.status.upper():5}] {item.id}: {item.detail}")
        return code

    if args.action == "upgrade":
        try:
            if args.apply:
                plan = apply_upgrade(args.target, to_version=args.to_version)
            else:
                plan = plan_upgrade(args.target, to_version=args.to_version)
        except (OSError, ValueError, DefinitionError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        output = plan.to_dict() | {"applied": bool(args.apply)}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    if args.action == "run":
        try:
            result = run_flow(
                args.target,
                args.flow,
                trigger_event=args.trigger_event,
                trigger_id=args.trigger_id,
                issue_or_pr=args.issue,
                force=args.force,
                keep_worktree=args.keep_worktree,
                use_worktree=not args.no_worktree,
                include_proposed=args.include_proposed,
            )
        except (DefinitionError, GitError, ValueError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2

        payload = {
            "run_id": result.run_id,
            "verdict": result.verdict,
            "exit_code": result.exit_code,
            "skipped_duplicate": result.skipped_duplicate,
            "run_dir": str(result.run_dir) if result.run_dir else None,
            "run": result.run_data,
        }
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                json.dumps(
                    {
                        "run_id": result.run_id,
                        "verdict": result.verdict,
                        "skipped_duplicate": result.skipped_duplicate,
                        "run_dir": str(result.run_dir) if result.run_dir else None,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return result.exit_code

    if args.action == "report":
        try:
            if args.post_github_issue is not None:
                posted = post_github_report(args.run_dir, args.post_github_issue)
                print(json.dumps(posted, ensure_ascii=False, indent=2))
                return 0
            print(format_report(args.run_dir, fmt=args.fmt))
            return 0
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        except RuntimeError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if args.action == "feedback":
        try:
            if args.fb_action == "add":
                out_path, record = add_intervention(
                    args.target,
                    run_id=args.run_id,
                    category=args.category,
                    summary=args.summary,
                    manual_triage_steps=args.triage_step,
                    preventive_action=args.preventive_action,
                    preventive_check_ref=args.preventive_check_ref,
                    status=args.status,
                )
                print(
                    json.dumps(
                        {"path": str(out_path), "record": record},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            summary = summarize_interventions(args.target)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2

    parser.error(f"unknown action: {args.action}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
