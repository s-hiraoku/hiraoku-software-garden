"""Run Artifacts writer and structured summary renderer (.garden/runs/<run-id>/)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def detect_workflow_run_url() -> str | None:
    """Build the GitHub Actions workflow run URL when executing inside GitHub Actions."""
    server = os.environ.get("GITHUB_SERVER_URL", "").strip().rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip().strip("/")
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def resolve_report_kind(run_data: dict[str, Any], requested_kind: str = "auto") -> str:
    if requested_kind != "auto":
        return requested_kind
    verdict = run_data.get("verdict")
    escalation = run_data.get("escalation") or {}
    if verdict == "SATISFIED":
        flow_scope = run_data.get("flow_scope")
        eval_changed = bool((run_data.get("target") or {}).get("evaluation_criteria_changed"))
        if flow_scope == "all_specs" and not eval_changed:
            return "harvest"
        return "verified"
    if escalation.get("kind") == "spec_question":
        return "spec_question"
    return "escalation"


def render_summary_markdown(
    run_data: dict[str, Any], *, requested_kind: str = "auto"
) -> str:
    kind = resolve_report_kind(run_data, requested_kind)
    run_id = run_data["run_id"]
    idem = run_data["idempotency_key"]
    verdict = run_data["verdict"]
    target = run_data.get("target") or {}
    commit_short = str(target.get("commit") or "")[:7]
    flow = run_data.get("flow") or ""
    duration_sec = run_data.get("duration_sec", 0.0)
    retries_used = run_data.get("retries_used", 0)
    usage = run_data.get("usage") or {}
    cost_usd = float(usage.get("cost_usd") or 0.0)

    marker = (
        f"<!-- garden:v1 kind={kind} run_id={run_id} "
        f"idempotency_key={idem} verdict={verdict} commit={commit_short} -->"
    )

    headings = {
        "harvest": f"### [Garden] 収穫の準備ができました（`{flow}`）",
        "verified": f"### [Garden] 検証が完了しました（`{flow}`）",
        "spec_question": f"### [Garden] 仕様の判断が必要です（`{flow}`）",
        "escalation": f"### [Garden] 人の確認が必要です（`{flow}` / `{verdict}`）",
    }
    heading = headings.get(kind, headings["escalation"])

    specs = (run_data.get("versions") or {}).get("specs") or []
    spec_desc = (
        ", ".join(
            f"`{s['spec_id']}` v{s['version']} (`{str(s.get('blob_sha', ''))[:7]}`)"
            for s in specs
        )
        or "なし"
    )

    lines = [
        marker,
        heading,
        "",
        f"- **対象版**: `{commit_short}` / **承認済み仕様**: {spec_desc}",
        f"- **判定**: `{verdict}`（所要 {duration_sec:.1f}s / 再試行 {retries_used}回 / 費用 ${cost_usd:.2f}）",
    ]

    if target.get("evaluation_criteria_changed"):
        reasons = ", ".join(target.get("evaluation_change_reasons") or [])
        lines.append(
            f"- **評価条件の変更**: あり（`auto_merge` 禁止 / 理由: {reasons or 'definition_or_spec_changed'}）"
        )

    criteria = run_data.get("criteria_results") or {}
    if criteria:
        lines.extend(
            [
                "",
                "| 条件 ID | 判定 | 根拠チェック | 備考 / 未確認の点 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for cid, info in criteria.items():
            c_verdict = info.get("verdict", "inconclusive")
            checks = ", ".join(f"`{c}`" for c in (info.get("checks") or [])) or "なし"
            note = info.get("note") or "なし"
            lines.append(f"| `{cid}` | `{c_verdict}` | {checks} | {note} |")

    escalation = run_data.get("escalation")
    if isinstance(escalation, dict):
        lines.extend(["", "#### 人に求める判断 / 詳細"])
        if escalation.get("reason"):
            lines.append(f"- **理由**: `{escalation['reason']}`")
        if escalation.get("detail"):
            lines.append(f"- **詳細**: {escalation['detail']}")
        question = escalation.get("question")
        if isinstance(question, dict):
            if question.get("prompt"):
                lines.append(f"- **決めてほしい問い**: {question['prompt']}")
            if question.get("options"):
                opts = " / ".join(str(o) for o in question["options"])
                lines.append(f"- **選択肢**: {opts}")
            if question.get("impact"):
                lines.append(f"- **影響**: {question['impact']}")
            if question.get("blocked_work"):
                lines.append(f"- **保留している仕事**: {question['blocked_work']}")

    artifacts_url = run_data.get("artifacts_url") or detect_workflow_run_url()
    if isinstance(artifacts_url, str) and artifacts_url.strip():
        lines.extend(["", f"- **証拠 Artifacts**: {artifacts_url.strip()}"])

    if kind == "harvest":
        lines.extend(
            ["", "- **次の判断**: 成果物と証拠を確認し、完成・出荷を判断してください。"]
        )

    return "\n".join(lines) + "\n"


def write_step_json(step_dir: Path, step_data: dict[str, Any]) -> Path:
    step_dir.mkdir(parents=True, exist_ok=True)
    path = step_dir / "step.json"
    path.write_text(
        json.dumps(step_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def write_run_artifacts(
    run_dir: Path,
    run_data: dict[str, Any],
    *,
    requested_report_kind: str = "auto",
) -> tuple[Path, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    run_json_path = run_dir / "run.json"
    run_json_path.write_text(
        json.dumps(run_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary_md = render_summary_markdown(run_data, requested_kind=requested_report_kind)
    summary_path = run_dir / "summary.md"
    summary_path.write_text(summary_md, encoding="utf-8")
    return run_json_path, summary_path
