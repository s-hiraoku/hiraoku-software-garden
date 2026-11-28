"""Agent Adapter Protocol data structures and JSON serialization."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Any


VALID_OUTCOMES = ("completed", "needs_spec_decision", "blocked", "failed")


def parse_nonneg_int_field(
    raw: Any, field_name: str, *, default: int = 0
) -> tuple[int | None, str | None]:
    """Validate an optional non-negative integer usage field.

    Absent (None) returns `(default, None)`. Present-but-malformed values return
    `(None, error_message)` instead of raising ValueError.
    """
    if raw is None:
        return default, None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, f"invalid {field_name}: expected non-negative integer, got {raw!r}"
    if raw < 0:
        return None, f"invalid {field_name}: expected non-negative integer, got {raw!r}"
    return raw, None


def parse_nonneg_float_field(
    raw: Any, field_name: str, *, default: float | None = None
) -> tuple[float | None, str | None]:
    """Validate an optional non-negative finite float usage field.

    Absent (None) returns `(default, None)`. Present-but-malformed values return
    `(None, error_message)` instead of raising ValueError.
    """
    if raw is None:
        return default, None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, f"invalid {field_name}: expected non-negative number, got {raw!r}"
    val = float(raw)
    if not math.isfinite(val) or val < 0.0:
        return None, f"invalid {field_name}: expected non-negative finite number, got {raw!r}"
    return val, None


@dataclass(frozen=True)
class AgentUsage:
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0

    @property
    def cost_unknown(self) -> bool:
        return self.cost_usd is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "cost_unknown": self.cost_usd is None,
        }


@dataclass(frozen=True)
class AgentRequest:
    run_id: str
    step_id: str
    purpose: str
    skill: str | None
    worktree_path: str
    specs: list[dict[str, Any]]
    delegation: dict[str, Any]
    context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "purpose": self.purpose,
            "skill": self.skill,
            "worktree_path": self.worktree_path,
            "specs": self.specs,
            "delegation": self.delegation,
            "context": self.context,
        }


@dataclass(frozen=True)
class AgentResponse:
    outcome: str
    summary: str
    changed_files: tuple[str, ...] = ()
    question: dict[str, Any] | None = None
    usage: AgentUsage = field(default_factory=AgentUsage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "outcome": self.outcome,
            "summary": self.summary,
            "changed_files": list(self.changed_files),
            "question": self.question,
            "usage": self.usage.to_dict(),
        }


def load_agent_response(path: Path) -> AgentResponse:
    if not path.is_file():
        return AgentResponse(
            outcome="failed",
            summary=f"adapter did not produce response file: {path.name}",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return AgentResponse(
            outcome="failed",
            summary=f"invalid adapter response JSON: {error}",
        )
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return AgentResponse(
            outcome="failed",
            summary="adapter response missing schema_version=1",
        )
    outcome = data.get("outcome")
    if outcome not in VALID_OUTCOMES:
        return AgentResponse(
            outcome="failed",
            summary=f"adapter response invalid outcome: {outcome!r}",
        )
    summary = str(data.get("summary") or "")
    changed_raw = data.get("changed_files") or []
    changed = (
        tuple(str(item) for item in changed_raw)
        if isinstance(changed_raw, list)
        else ()
    )
    question = data.get("question")
    if outcome == "needs_spec_decision":
        if not isinstance(question, dict) or not question.get("prompt"):
            return AgentResponse(
                outcome="failed",
                summary="needs_spec_decision response missing question.prompt",
            )
    usage_val = data.get("usage")
    if usage_val is not None and not isinstance(usage_val, dict):
        return AgentResponse(
            outcome="failed",
            summary="adapter response usage must be a JSON object",
        )
    usage_raw = usage_val if isinstance(usage_val, dict) else {}
    in_tok, in_err = parse_nonneg_int_field(usage_raw.get("input_tokens"), "usage.input_tokens")
    if in_err is not None:
        return AgentResponse(outcome="failed", summary=f"adapter response {in_err}")
    out_tok, out_err = parse_nonneg_int_field(usage_raw.get("output_tokens"), "usage.output_tokens")
    if out_err is not None:
        return AgentResponse(outcome="failed", summary=f"adapter response {out_err}")
    cost_default = 0.0 if usage_raw.get("cost_unknown") is not True else None
    cost_usd, cost_err = parse_nonneg_float_field(
        usage_raw.get("cost_usd"), "usage.cost_usd", default=cost_default
    )
    if cost_err is not None:
        return AgentResponse(outcome="failed", summary=f"adapter response {cost_err}")
    model_raw = usage_raw.get("model")
    model = str(model_raw).strip() if isinstance(model_raw, str) and model_raw.strip() else None
    usage = AgentUsage(
        model=model,
        input_tokens=in_tok or 0,
        output_tokens=out_tok or 0,
        cost_usd=cost_usd,
    )
    return AgentResponse(
        outcome=outcome,
        summary=summary,
        changed_files=changed,
        question=question if isinstance(question, dict) else None,
        usage=usage,
    )
