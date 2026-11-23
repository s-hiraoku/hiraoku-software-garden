"""Phase-7 intervention record storage and aggregation (.garden/interventions/)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any


VALID_CATEGORIES = (
    "spec_gap",
    "harness_bug",
    "flaky_env",
    "code_defect",
    "delegation_limit",
)
VALID_STATUSES = ("open", "implemented", "verified")


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
