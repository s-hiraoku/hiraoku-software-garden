"""Phase-7 intervention record storage and aggregation (.garden/interventions/)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
from typing import Any


VALID_CATEGORIES = (
    "spec_gap",
    "harness_bug",
    "flaky_env",
    "code_defect",
    "delegation_limit",
)
VALID_STATUSES = ("open", "implemented", "verified")
_INTERVENTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def interventions_dir(target_root: Path) -> Path:
    return target_root / ".garden" / "interventions"


def add_intervention(
    target_root: Path,
    *,
    run_id: str,
    category: str,
    summary: str,
    manual_triage_steps: list[str] | None = None,
    preventive_action: str = "",
    preventive_check_ref: str = "",
    status: str = "open",
) -> tuple[Path, dict[str, Any]]:
    if category not in VALID_CATEGORIES:
        raise ValueError(
            f"category must be one of {', '.join(VALID_CATEGORIES)}"
        )
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {', '.join(VALID_STATUSES)}")
    if not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    if any(ch in run_id for ch in ("\n", "\r")) or any(ord(ch) < 32 for ch in run_id):
        raise ValueError("run_id must not contain newlines or control characters")
    if not summary.strip():
        raise ValueError("summary must be a non-empty string")

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    short_hash = hashlib.sha256(
        f"{stamp}:{run_id}:{category}:{summary}".encode("utf-8")
    ).hexdigest()[:4]
    intervention_id = f"{stamp}-{short_hash}"

    record: dict[str, Any] = {
        "schema_version": 1,
        "intervention_id": intervention_id,
        "run_id": run_id,
        "category": category,
        "summary": summary,
        "manual_triage_steps": list(manual_triage_steps or []),
        "preventive_action": preventive_action,
        "preventive_check_ref": preventive_check_ref,
        "status": status,
        "recorded_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    out_dir = interventions_dir(target_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{intervention_id}.json"
    out_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return out_path, record


def summarize_interventions(target_root: Path) -> dict[str, Any]:
    out_dir = interventions_dir(target_root)
    records: list[dict[str, Any]] = []
    if out_dir.is_dir():
        for path in sorted(out_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("schema_version") == 1:
                records.append(data)

    by_category = Counter(r.get("category", "unknown") for r in records)
    by_status = Counter(r.get("status", "unknown") for r in records)
    return {
        "total": len(records),
        "by_category": dict(by_category),
        "by_status": dict(by_status),
        "records": records,
    }


def update_intervention(
    target_root: Path,
    intervention_id: str,
    *,
    status: str | None = None,
    preventive_action: str | None = None,
    preventive_check_ref: str | None = None,
    add_triage_steps: list[str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Update status or preventive metadata of an existing intervention record."""
    if not _INTERVENTION_ID_RE.match(intervention_id):
        raise ValueError(f"invalid intervention_id: {intervention_id!r}")
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {', '.join(VALID_STATUSES)}")

    path = interventions_dir(target_root) / f"{intervention_id}.json"
    if not path.is_file():
        raise ValueError(f"intervention not found: {intervention_id}")

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt intervention record {path}: {exc}") from exc

    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ValueError(f"unsupported intervention schema in {path}")

    if status is not None:
        record["status"] = status
    if preventive_action is not None:
        record["preventive_action"] = preventive_action
    if preventive_check_ref is not None:
        record["preventive_check_ref"] = preventive_check_ref
    if add_triage_steps:
        existing = list(record.get("manual_triage_steps") or [])
        existing.extend(add_triage_steps)
        record["manual_triage_steps"] = existing

    record["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path, record


def parse_feedback_comment(
    body: str, *, default_run_id: str | None = None
) -> dict[str, Any]:
    """Parse a `/garden feedback <category> <summary>` GitHub Issue/PR comment."""
    lines = body.splitlines()
    cmd_index: int | None = None
    cmd_line = ""
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped == "/garden feedback" or stripped.startswith("/garden feedback "):
            cmd_index = idx
            cmd_line = stripped
            break
    if cmd_index is None:
        raise ValueError("comment does not contain a '/garden feedback' command")

    rest = cmd_line[len("/garden feedback") :].strip()
    if not rest:
        raise ValueError(
            "missing category and summary after '/garden feedback'"
        )

    try:
        tokens = shlex.split(rest)
    except ValueError as exc:
        raise ValueError(f"failed to parse '/garden feedback' arguments: {exc}") from exc

    if not tokens:
        raise ValueError("missing category after '/garden feedback'")

    category = tokens[0]
    if category not in VALID_CATEGORIES:
        raise ValueError(
            f"category must be one of {', '.join(VALID_CATEGORIES)} (got {category!r})"
        )

    run_id = default_run_id or ""
    status = "open"
    preventive_action = ""
    preventive_check_ref = ""
    triage_steps: list[str] = []
    summary_parts: list[str] = []

    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--run-id", "--triage", "--preventive", "--ref", "--status"):
            if i + 1 >= len(tokens):
                raise ValueError(f"flag {tok} requires a value")
            val = tokens[i + 1]
            if tok == "--run-id":
                run_id = val
            elif tok == "--triage":
                triage_steps.append(val)
            elif tok == "--preventive":
                preventive_action = val
            elif tok == "--ref":
                preventive_check_ref = val
            elif tok == "--status":
                status = val
            i += 2
            continue
        summary_parts.append(tok)
        i += 1

    # Also accept optional key: value lines following the command line
    for raw in lines[cmd_index + 1 :]:
        line = raw.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key_norm = key.strip().lower()
        val_norm = val.strip()
        if not val_norm:
            continue
        if key_norm == "run_id":
            run_id = val_norm
        elif key_norm == "triage":
            triage_steps.append(val_norm)
        elif key_norm in ("preventive", "preventive_action"):
            preventive_action = val_norm
        elif key_norm in ("ref", "preventive_check_ref"):
            preventive_check_ref = val_norm
        elif key_norm == "status":
            status = val_norm

    summary = " ".join(summary_parts).strip()
    if not summary:
        raise ValueError("summary must be non-empty in '/garden feedback'")
    if not run_id.strip():
        raise ValueError("run_id is required (pass --run-id or 'run_id: <id>')")
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {', '.join(VALID_STATUSES)}")

    return {
        "run_id": run_id.strip(),
        "category": category,
        "summary": summary,
        "manual_triage_steps": triage_steps,
        "preventive_action": preventive_action,
        "preventive_check_ref": preventive_check_ref,
        "status": status,
    }
