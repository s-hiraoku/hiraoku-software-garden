"""Report generation, HTML marker parsing, and GitHub Issue/PR comment & label transitions."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any

from garden.artifacts import render_summary_markdown, resolve_report_kind


KIND_TO_LABEL = {
    "harvest": "garden:harvest-ready",
    "spec_question": "garden:needs-human",
    "escalation": "garden:needs-human",
}
MANAGED_LABELS = ("garden:running", "garden:needs-human", "garden:harvest-ready")
MANAGED_LABEL_SPECS: dict[str, tuple[str, str]] = {
    "garden:harvest-ready": ("0e8a16", "Software Garden verified all approved criteria"),
    "garden:needs-human": ("d93f0b", "Software Garden requires human review or decision"),
    "garden:running": ("1d76db", "Software Garden verification is in progress"),
}
MARKER_RE = re.compile(r"<!--\s*garden:v1\s+([^>]+?)\s*-->")
KV_RE = re.compile(r"([a-zA-Z0-9_]+)=([^\s]+)")


def parse_garden_markers(text: str) -> list[dict[str, str]]:
    """Extract all `<!-- garden:v1 key=value ... -->` markers from comment text."""
    markers: list[dict[str, str]] = []
    for match in MARKER_RE.finditer(text):
        pairs = dict(KV_RE.findall(match.group(1)))
        if pairs:
            markers.append(pairs)
    return markers


def find_satisfied_marker(
    comments: list[str], idempotency_key: str
) -> dict[str, str] | None:
    """Return the most recent SATISFIED marker matching idempotency_key, if any."""
    for body in reversed(comments):
        for marker in reversed(parse_garden_markers(body)):
            if (
                marker.get("idempotency_key") == idempotency_key
                and marker.get("verdict") == "SATISFIED"
            ):
                return marker
    return None


def fetch_github_issue_comments(
    issue_number: int, *, cwd: Path | None = None
) -> list[str]:
    """Fetch comment bodies from a GitHub Issue or PR using `gh issue view`."""
    proc = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--json", "comments"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    raw_comments = data.get("comments") if isinstance(data, dict) else []
    if not isinstance(raw_comments, list):
        return []
    return [
        str(item.get("body") or "")
        for item in raw_comments
        if isinstance(item, dict)
    ]


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

    label_warnings: list[str] = []
    # Ensure labels to add exist in the target repository before applying them
    for lbl in add_labels:
        color, desc = MANAGED_LABEL_SPECS.get(lbl, ("ededed", "Managed by Software Garden"))
        subprocess.run(
            ["gh", "label", "create", lbl, "--force", "--color", color, "--description", desc],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )

    if add_labels:
        add_args = ["gh", "issue", "edit", str(issue_number)]
        for lbl in add_labels:
            add_args.extend(["--add-label", lbl])
        add_proc = subprocess.run(
            add_args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if add_proc.returncode != 0:
            label_warnings.append(add_proc.stderr.strip() or "add-label failed")

    # Only remove managed labels that are actually attached to the Issue / PR
    actually_removed: list[str] = []
    if remove_labels:
        existing_labels: set[str] | None = None
        view_proc = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--json", "labels"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if view_proc.returncode == 0:
            try:
                parsed = json.loads(view_proc.stdout)
                existing_labels = {
                    str(item.get("name"))
                    for item in (parsed.get("labels") or [])
                    if isinstance(item, dict) and item.get("name")
                }
            except json.JSONDecodeError:
                existing_labels = None

        to_remove = (
            [lbl for lbl in remove_labels if lbl in existing_labels]
            if existing_labels is not None
            else remove_labels
        )
        if to_remove:
            rem_args = ["gh", "issue", "edit", str(issue_number)]
            for lbl in to_remove:
                rem_args.extend(["--remove-label", lbl])
            rem_proc = subprocess.run(
                rem_args,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            if rem_proc.returncode == 0:
                actually_removed = to_remove
            else:
                label_warnings.append(rem_proc.stderr.strip() or "remove-label failed")

    label_warning = "; ".join(label_warnings) if label_warnings else None

    return {
        "issue": issue_number,
        "comment_url": comment_proc.stdout.strip(),
        "added_labels": add_labels,
        "removed_labels": actually_removed,
        "label_warning": label_warning,
    }
