"""Operational health metrics across .garden/runs/ and .garden/interventions/ (Design §5)."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any

from garden.feedback import summarize_interventions


def _load_runs(target_root: Path) -> list[dict[str, Any]]:
    runs_dir = target_root / ".garden" / "runs"
    if not runs_dir.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for run_json in sorted(runs_dir.glob("*/run.json")):
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("schema_version") == 1:
            runs.append(data)
    # Sort oldest -> newest by started_at / run_id
    runs.sort(key=lambda r: (str(r.get("started_at") or ""), str(r.get("run_id") or "")))
    return runs


def collect_garden_metrics(
    target_root: Path, *, limit: int | None = None
) -> dict[str, Any]:
    """Aggregate run verdicts, retries, recurring failures, and intervention closure metrics."""
    if limit is not None and limit <= 0:
        raise ValueError("limit must be a positive integer")

    all_runs = _load_runs(target_root)
    window_runs = all_runs[-limit:] if limit is not None else all_runs

    verdict_counts: Counter[str] = Counter()
    escalation_reasons: Counter[str] = Counter()
    criteria_non_satisfied: Counter[str] = Counter()
    total_retries = 0
    total_duration = 0.0
    total_cost = 0.0

    for run in window_runs:
        verdict = str(run.get("verdict") or "UNKNOWN")
        verdict_counts[verdict] += 1
        total_retries += int(run.get("retries_used") or 0)
        total_duration += float(run.get("duration_sec") or 0.0)
        usage = run.get("usage") or {}
        total_cost += float(usage.get("cost_usd") or 0.0)

        esc = run.get("escalation")
        if isinstance(esc, dict) and esc.get("reason"):
            escalation_reasons[str(esc["reason"])] += 1

        criteria = run.get("criteria_results") or {}
        if isinstance(criteria, dict):
            for cid, info in criteria.items():
                if isinstance(info, dict) and info.get("verdict") != "satisfied":
                    criteria_non_satisfied[str(cid)] += 1

    n = len(window_runs)

    def _rate(count: int) -> float:
        return round(count / n, 4) if n else 0.0

    interventions_raw = summarize_interventions(target_root)
    int_total = int(interventions_raw["total"])
    by_status = interventions_raw["by_status"]
    verified_count = int(by_status.get("verified", 0))
    open_count = int(by_status.get("open", 0))
    implemented_count = int(by_status.get("implemented", 0))

    recent_runs = [
        {
            "run_id": r.get("run_id"),
            "flow": r.get("flow"),
            "commit": (r.get("target") or {}).get("commit", "")[:7],
            "verdict": r.get("verdict"),
            "duration_sec": r.get("duration_sec"),
            "retries_used": r.get("retries_used", 0),
            "started_at": r.get("started_at"),
        }
        for r in reversed(window_runs[-20:])
    ]

    return {
        "schema_version": 1,
        "target": str(target_root.resolve()),
        "window_size": n,
        "total_runs_recorded": len(all_runs),
        "verdict_counts": dict(verdict_counts),
        "rates": {
            "satisfied": _rate(verdict_counts.get("SATISFIED", 0)),
            "unsatisfied": _rate(verdict_counts.get("UNSATISFIED", 0)),
            "inconclusive": _rate(verdict_counts.get("INCONCLUSIVE", 0)),
            "needs_human": _rate(verdict_counts.get("NEEDS_HUMAN", 0)),
            "aborted": _rate(verdict_counts.get("ABORTED", 0)),
        },
        "total_retries": total_retries,
        "avg_duration_sec": round(total_duration / n, 3) if n else 0.0,
        "total_cost_usd": round(total_cost, 4),
        "escalation_reasons": dict(escalation_reasons),
        "criteria_non_satisfied_counts": dict(criteria_non_satisfied),
        "recurring_failure_criteria": sorted(
            cid for cid, cnt in criteria_non_satisfied.items() if cnt >= 2
        ),
        "interventions": {
            "total": int_total,
            "by_category": interventions_raw["by_category"],
            "by_status": by_status,
            "open_count": open_count,
            "implemented_count": implemented_count,
            "verified_count": verified_count,
            "verified_rate": round(verified_count / int_total, 4) if int_total else 0.0,
        },
        "recent_runs": recent_runs,
    }
