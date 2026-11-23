"""Report generation and GitHub Issue/PR comment & label transition helper."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

from garden.artifacts import render_summary_markdown, resolve_report_kind


KIND_TO_LABEL = {
    "harvest": "garden:harvest-ready",
    "spec_question": "garden:needs-human",
    "escalation": "garden:needs-human",
}
MANAGED_LABELS = ("garden:running", "garden:needs-human", "garden:harvest-ready")


def load_run_data(run_dir: Path) -> dict[str, Any]:
    run_json = run_dir / "run.json"
    if not run_json.is_file():
        raise ValueError(f"{run_json}: run.json not found")
    data = json.loads(run_json.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"{run_json}: invalid run.json schema")
    return data


def format_report(run_dir: Path, fmt: str = "md") -> str:
    data = load_run_data(run_dir)
    if fmt == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)
    summary_path = run_dir / "summary.md"
    if summary_path.is_file():
        return summary_path.read_text(encoding="utf-8")
    return render_summary_markdown(data)


def label_transition_for(run_data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (labels_to_add, labels_to_remove) for a run's final state."""
    kind = resolve_report_kind(run_data)
    target_label = KIND_TO_LABEL.get(kind)
    add = [target_label] if target_label else []
    remove = [lbl for lbl in MANAGED_LABELS if lbl not in add]
    return add, remove


def post_github_report(
    run_dir: Path,
    issue_number: int,
    *,
    cwd: Path | None = None,
) -> dict[str, Any]:
    data = load_run_data(run_dir)
    body = format_report(run_dir, fmt="md")
    add_labels, remove_labels = label_transition_for(data)

    comment_proc = subprocess.run(
        ["gh", "issue", "comment", str(issue_number), "--body", body],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if comment_proc.returncode != 0:
        raise RuntimeError(
            f"gh issue comment failed: {comment_proc.stderr.strip() or comment_proc.stdout.strip()}"
        )

    label_args = ["gh", "issue", "edit", str(issue_number)]
    for lbl in add_labels:
        label_args.extend(["--add-label", lbl])
    for lbl in remove_labels:
        label_args.extend(["--remove-label", lbl])

    label_warning: str | None = None
    if add_labels or remove_labels:
        lbl_proc = subprocess.run(
            label_args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if lbl_proc.returncode != 0:
            label_warning = lbl_proc.stderr.strip() or "label update failed"

    return {
        "issue": issue_number,
        "comment_url": comment_proc.stdout.strip(),
        "added_labels": add_labels,
        "removed_labels": remove_labels,
        "label_warning": label_warning,
    }
