"""Unified CLI entry point for Software Garden (catalog, diagnostics, execution, templates)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from garden.catalog import check_repository, read_catalog, register_repository
from garden.definition import DefinitionError
from garden.doctor import run_doctor
from garden.engine import run_flow
from garden.feedback import (
    VALID_CATEGORIES,
    VALID_STATUSES,
    add_intervention,
    parse_feedback_comment,
    summarize_interventions,
    update_intervention,
)
from garden.fleet import collect_fleet_status
from garden.git_utils import GitError
from garden.harness_setup import (
    GUIDE_ADAPTERS,
    PSTACK_SYNC_ADAPTERS,
    build_harness_guide,
    format_harness_guide_markdown,
    sync_pstack,
)
from garden.metrics import collect_garden_metrics
from garden.publish import reflect_run
from garden.release_gate import run_template_release_gate
from garden.report import (
    clear_github_running,
    format_report,
    mark_github_running,
    post_github_report,
)
from garden.spec import SpecError
from garden.spec_diff import analyze_spec_diff
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

    reg_cmd = commands.add_parser(
        "register", help="register a target repository into the Nursery catalog (garden.toml)"
    )
    reg_cmd.add_argument("target", type=Path, help="path to the target Git repository")
    reg_cmd.add_argument("--id", dest="repo_id", default=None)
    reg_cmd.add_argument("--description", default=None)
    reg_cmd.add_argument("--remote", default=None)
    reg_cmd.add_argument(
        "--check",
        nargs=argparse.REMAINDER,
        dest="check_command",
        default=None,
        help="check command argv (pass last, e.g. --check python3 -m unittest)",
    )
    reg_cmd.add_argument("--force", action="store_true")

    # Target-repository onboarding & diagnostics
    init_cmd = commands.add_parser("init", help="initialize a target repository as a Garden")
    init_cmd.add_argument("--target", type=Path, default=Path.cwd())
    init_cmd.add_argument("--type", dest="template_type", default=None)
    init_cmd.add_argument("--version", type=int, default=None)
    init_cmd.add_argument(
        "--nursery-ref",
        default="main",
        help="Nursery Git ref (branch, tag, or commit SHA) to pin in generated workflow",
    )
    init_cmd.add_argument(
        "--with-standard-harness",
        action="store_true",
        help="populate template standard required_skills and required_mcp into .garden/garden.toml",
    )
    init_cmd.add_argument("--force", action="store_true")

    doc_cmd = commands.add_parser("doctor", help="diagnose a target repository Garden setup")
    doc_cmd.add_argument("--target", type=Path, default=Path.cwd())
    doc_cmd.add_argument("--approval-branch", default="main")
    doc_cmd.add_argument("--json", action="store_true", dest="as_json")
    doc_cmd.add_argument(
        "--skip-network",
        action="store_true",
        help="skip remote GitHub branch protection API check (does not disable --probe-mcp)",
    )
    doc_cmd.add_argument(
        "--strict-harness",
        action="store_true",
        help="require pstack core skills even for observe_only definitions without real agent steps",
    )
    doc_cmd.add_argument(
        "--probe-mcp",
        action="store_true",
        help="perform live connectivity/initialize probes against configured required MCP servers",
    )
    doc_cmd.add_argument(
        "--trust-project-mcp",
        action="store_true",
        help="explicitly approve launching stdio MCP commands defined in project-local config files during --probe-mcp",
    )

    har_cmd = commands.add_parser(
        "harness",
        help="inspect harness setup guidance or synchronize pinned pstack",
    )
    har_sub = har_cmd.add_subparsers(dest="harness_action", required=True)

    har_guide = har_sub.add_parser(
        "guide",
        help="show environment-specific setup instructions and template standard harness gaps",
    )
    har_guide.add_argument("--target", type=Path, default=Path.cwd())
    har_guide.add_argument(
        "--adapter",
        choices=("all", *GUIDE_ADAPTERS),
        default="all",
    )
    har_guide.add_argument("--strict-harness", action="store_true")
    har_guide.add_argument("--probe-mcp", action="store_true")
    har_guide.add_argument(
        "--trust-project-mcp",
        action="store_true",
        help="explicitly approve launching stdio MCP commands defined in project-local config files during --probe-mcp",
    )
    har_guide.add_argument("--json", action="store_true", dest="as_json")

    har_sync = har_sub.add_parser(
        "sync-pstack",
        help="plan or apply pinned pstack synchronization from harnesses/pstack.toml (dry-run by default)",
    )
    har_sync.add_argument(
        "--adapter",
        choices=("auto", *sorted(PSTACK_SYNC_ADAPTERS)),
        default="antigravity",
    )
    har_sync.add_argument("--dest", type=Path, default=None)
    har_sync.add_argument("--target", type=Path, default=Path.cwd())
    har_sync.add_argument(
        "--approval-branch",
        default=None,
        help="verify target's approved [common_harness].ref on this branch matches nursery pinned_ref",
    )
    har_sync.add_argument(
        "--flow",
        default=None,
        help="flow name (or 'auto') to scope --adapter auto to the executed flow's adapters",
    )
    har_sync.add_argument("--trigger-event", default="manual")
    har_sync.add_argument("--trigger-action", default=None)
    har_sync.add_argument("--trigger-label", default=None)
    har_sync.add_argument("--source", default=None)
    har_sync.add_argument("--apply", action="store_true")
    har_sync.add_argument(
        "--force",
        action="store_true",
        help="allow replacing pre-existing unmanaged skill directories with colliding names",
    )
    har_sync.add_argument("--json", action="store_true", dest="as_json")

    upg_cmd = commands.add_parser("upgrade", help="plan or apply a template upgrade safely")
    upg_cmd.add_argument("--target", type=Path, default=Path.cwd())
    upg_cmd.add_argument("--to", dest="to_version", type=int, default=None)
    upg_cmd.add_argument("--apply", action="store_true")

    # Deterministic flow execution
    run_cmd = commands.add_parser("run", help="execute a Garden flow deterministically")
    run_cmd.add_argument("flow", help="flow name defined in .garden/garden.toml (or 'auto' via [[triggers]])")
    run_cmd.add_argument("--target", type=Path, default=Path.cwd())
    run_cmd.add_argument("--approval-branch", default="main")
    run_cmd.add_argument("--trigger-event", default="manual")
    run_cmd.add_argument("--trigger-id", default="manual")
    run_cmd.add_argument("--trigger-action", default=None)
    run_cmd.add_argument("--trigger-label", default=None)
    run_cmd.add_argument("--issue", type=int, default=None)
    run_cmd.add_argument("--check-github-markers", action="store_true")
    run_cmd.add_argument("--existing-comments-file", type=Path, default=None)
    run_cmd.add_argument("--force", action="store_true")
    run_cmd.add_argument("--keep-worktree", action="store_true")
    run_cmd.add_argument("--no-worktree", action="store_true")
    run_cmd.add_argument("--include-proposed", action="store_true")
    run_cmd.add_argument("--json", action="store_true", dest="as_json")

    # Reporting & GitHub handoff
    rep_cmd = commands.add_parser("report", help="render or post a run summary report")
    rep_cmd.add_argument("run_dir", nargs="?", type=Path, default=None)
    rep_cmd.add_argument("--format", dest="fmt", choices=("md", "json"), default="md")
    rep_cmd.add_argument("--post-github-issue", type=int, default=None)
    rep_cmd.add_argument("--mark-running", type=int, default=None)
    rep_cmd.add_argument("--clear-running", type=int, default=None)

    ref_cmd = commands.add_parser(
        "reflect", help="evaluate Phase-6 reflection gate (dry-run by default)"
    )
    ref_cmd.add_argument("run_dir", type=Path)
    ref_cmd.add_argument("--base", default="main")
    ref_cmd.add_argument("--execute", action="store_true")

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

    fb_cmt = fb_sub.add_parser(
        "from-comment",
        help="parse a '/garden feedback ...' comment and record the intervention",
    )
    fb_cmt.add_argument("--target", type=Path, default=Path.cwd())
    fb_cmt_src = fb_cmt.add_mutually_exclusive_group(required=True)
    fb_cmt_src.add_argument("--body", default=None)
    fb_cmt_src.add_argument("--body-file", type=Path, default=None)
    fb_cmt.add_argument("--run-id", default=None, help="fallback run_id if omitted in comment")

    fb_upd = fb_sub.add_parser("update", help="update status or preventive details of an intervention")
    fb_upd.add_argument("--target", type=Path, default=Path.cwd())
    fb_upd.add_argument("--id", dest="intervention_id", required=True)
    fb_upd.add_argument("--status", choices=VALID_STATUSES, default=None)
    fb_upd.add_argument("--preventive-action", default=None)
    fb_upd.add_argument("--preventive-check-ref", default=None)
    fb_upd.add_argument("--triage-step", action="append", default=[])

    # Operational metrics & template release gate
    met_cmd = commands.add_parser(
        "metrics", help="aggregate operational health metrics from runs and interventions"
    )
    met_cmd.add_argument("--target", type=Path, default=Path.cwd())
    met_cmd.add_argument("--limit", type=int, default=None)

    gate_cmd = commands.add_parser(
        "template-gate",
        help="run template release gate (fresh init pass/fail + upgrade) and emit release-evidence.json",
    )
    gate_cmd.add_argument("--type", dest="template_type", default=None)
    gate_cmd.add_argument("--version", type=int, default=None)
    gate_cmd.add_argument("--all", dest="all_templates", action="store_true")
    gate_cmd.add_argument("--output", type=Path, default=None)

    status_cmd = commands.add_parser(
        "status",
        help="show aggregated health, doctor, template version, and metrics across all Gardens",
    )
    status_cmd.add_argument("--approval-branch", default="main")
    status_cmd.add_argument("--no-self", action="store_true", help="exclude Nursery self-dogfood garden")
    status_cmd.add_argument("--skip-network", action="store_true")
    status_cmd.add_argument("--json", action="store_true", dest="as_json")

    sdiff_cmd = commands.add_parser(
        "spec-diff",
        help="compare approved base spec against head/working tree and map impacted checks and flows",
    )
    sdiff_cmd.add_argument("--target", type=Path, default=Path.cwd())
    sdiff_cmd.add_argument("--base", default="main")
    sdiff_cmd.add_argument("--head", default=None)

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

    if args.action == "register":
        check_cmd = args.check_command
        if check_cmd and check_cmd[0] == "--":
            check_cmd = check_cmd[1:]
        try:
            registered = register_repository(
                args.catalog,
                args.workspace,
                args.target,
                repo_id=args.repo_id,
                description=args.description,
                remote=args.remote,
                check_command=check_cmd or None,
                force=args.force,
            )
        except (OSError, ValueError, GitError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(json.dumps(registered, ensure_ascii=False, indent=2))
        return 0

    if args.action == "init":
        try:
            result = init_garden(
                args.target,
                template_type=args.template_type,
                version=args.version,
                nursery_ref=args.nursery_ref,
                with_standard_harness=args.with_standard_harness,
                force=args.force,
            )
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.action == "doctor":
        code, items = run_doctor(
            args.target,
            skip_network=args.skip_network,
            approval_branch=args.approval_branch,
            strict_harness=args.strict_harness,
            probe_mcp=args.probe_mcp,
            trust_project_mcp=args.trust_project_mcp,
        )
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

    if args.action == "harness":
        try:
            if args.harness_action == "guide":
                guide = build_harness_guide(
                    args.target,
                    adapter=args.adapter,
                    strict_harness=args.strict_harness,
                    probe_mcp=args.probe_mcp,
                    trust_project_mcp=args.trust_project_mcp,
                )
                if args.as_json:
                    print(json.dumps(guide, ensure_ascii=False, indent=2))
                else:
                    print(format_harness_guide_markdown(guide))
                return 0 if guide["status"]["ok"] else 1
            if args.harness_action == "sync-pstack":
                synced = sync_pstack(
                    adapter=args.adapter,
                    dest=args.dest,
                    target_root=args.target,
                    approval_branch=args.approval_branch,
                    flow=args.flow,
                    trigger_event=args.trigger_event,
                    trigger_action=args.trigger_action,
                    trigger_label=args.trigger_label,
                    source=args.source,
                    apply=args.apply,
                    force=args.force,
                )
                if args.as_json:
                    print(json.dumps(synced, ensure_ascii=False, indent=2))
                else:
                    mode = "APPLIED" if synced["applied"] else "DRY-RUN"
                    dests = ", ".join(
                        f"{d['adapter']}->{d['dest']}" for d in synced["destinations"]
                    )
                    print(
                        f"[{mode}] pstack@{synced['pinned_ref'][:12]} ({dests})"
                    )
                    if not synced["applied"]:
                        print("Pass --apply to fetch and install the pinned pstack revision.")
                return 0
        except (OSError, ValueError, DefinitionError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        except RuntimeError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

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
        existing_comments: list[str] | None = None
        if args.existing_comments_file is not None:
            try:
                raw_comments = json.loads(
                    args.existing_comments_file.read_text(encoding="utf-8")
                )
                if not isinstance(raw_comments, list):
                    raise ValueError("existing-comments-file must contain a JSON array of strings")
                existing_comments = [str(c) for c in raw_comments]
            except (OSError, ValueError) as error:
                print(f"error: {error}", file=sys.stderr)
                return 2
        try:
            result = run_flow(
                args.target,
                args.flow,
                approval_branch=args.approval_branch,
                trigger_event=args.trigger_event,
                trigger_id=args.trigger_id,
                trigger_action=args.trigger_action,
                trigger_label=args.trigger_label,
                issue_or_pr=args.issue,
                force=args.force,
                keep_worktree=args.keep_worktree,
                use_worktree=not args.no_worktree,
                include_proposed=args.include_proposed,
                existing_comments=existing_comments,
                check_github_markers=args.check_github_markers,
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
            if args.mark_running is not None and args.clear_running is not None:
                raise ValueError("--mark-running and --clear-running cannot be combined")
            if args.mark_running is not None:
                marked = mark_github_running(args.mark_running)
                print(json.dumps(marked, ensure_ascii=False, indent=2))
                return 0
            if args.clear_running is not None:
                cleared = clear_github_running(args.clear_running)
                print(json.dumps(cleared, ensure_ascii=False, indent=2))
                return 0
            if args.run_dir is None:
                raise ValueError("run_dir is required unless --mark-running or --clear-running is set")
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

    if args.action == "reflect":
        try:
            reflected = reflect_run(
                args.run_dir, base_branch=args.base, execute=args.execute
            )
            print(json.dumps(reflected, ensure_ascii=False, indent=2))
            return 1 if reflected["allowed_action"] == "blocked" else 0
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
            if args.fb_action == "from-comment":
                body_text = (
                    args.body
                    if args.body is not None
                    else args.body_file.read_text(encoding="utf-8")
                )
                parsed = parse_feedback_comment(body_text, default_run_id=args.run_id)
                out_path, record = add_intervention(args.target, **parsed)
                print(
                    json.dumps(
                        {"path": str(out_path), "record": record},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.fb_action == "update":
                out_path, record = update_intervention(
                    args.target,
                    args.intervention_id,
                    status=args.status,
                    preventive_action=args.preventive_action,
                    preventive_check_ref=args.preventive_check_ref,
                    add_triage_steps=args.triage_step or None,
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

    if args.action == "metrics":
        try:
            metrics = collect_garden_metrics(args.target, limit=args.limit)
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return 0

    if args.action == "template-gate":
        try:
            evidence = run_template_release_gate(
                template_type=args.template_type,
                version=args.version,
                all_templates=args.all_templates,
                output_path=args.output,
            )
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        except RuntimeError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        return 0 if evidence["overall_passed"] else 1

    if args.action == "status":
        try:
            fleet = collect_fleet_status(
                args.catalog,
                args.workspace,
                include_self=not args.no_self,
                approval_branch=args.approval_branch,
                skip_network=args.skip_network,
            )
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        if args.as_json:
            print(json.dumps(fleet, ensure_ascii=False, indent=2))
        else:
            for g in fleet["gardens"]:
                latest = g.get("latest_run") or {}
                verdict = latest.get("verdict") or "-"
                tpl = g.get("template")
                tpl_str = f"{tpl['name']}@v{tpl['version']}" if tpl else "-"
                print(
                    f"[{g['health'].upper():12}] {g['repo']} ({tpl_str}) latest={verdict}"
                )
        return 1 if fleet["has_errors"] else 0

    if args.action == "spec-diff":
        try:
            diff_report = analyze_spec_diff(
                args.target, base_ref=args.base, head_ref=args.head
            )
        except (OSError, ValueError, DefinitionError, GitError, SpecError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(json.dumps(diff_report, ensure_ascii=False, indent=2))
        return 1 if diff_report["impact"]["requires_garden_update"] else 0

    parser.error(f"unknown action: {args.action}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
