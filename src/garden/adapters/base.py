"""Agent Adapter Protocol data structures and JSON serialization."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


VALID_OUTCOMES = ("completed", "needs_spec_decision", "blocked", "failed")


@dataclass(frozen=True)
class AgentUsage:
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
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
    usage_raw = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    usage = AgentUsage(
        model=usage_raw.get("model"),
        input_tokens=int(usage_raw.get("input_tokens") or 0),
        output_tokens=int(usage_raw.get("output_tokens") or 0),
        cost_usd=float(usage_raw.get("cost_usd") or 0.0),
    )
    return AgentResponse(
        outcome=outcome,
        summary=summary,
        changed_files=changed,
        question=question if isinstance(question, dict) else None,
        usage=usage,
    )
