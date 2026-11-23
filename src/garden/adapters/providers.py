"""Prompt synthesis and structured output extraction for external agent CLIs (codex/claude/antigravity)."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from garden.adapters.base import (
    VALID_OUTCOMES,
    AgentRequest,
    AgentResponse,
    AgentUsage,
)


JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
MAX_STDERR_EXCERPT_CHARS = 4000


def build_agent_prompt(request: AgentRequest) -> str:
    """Build a deterministic structured prompt from an AgentRequest."""
    spec_lines = [
        f"- {s['spec_id']} v{s['version']} ({s['path']}, blob={str(s.get('blob_sha', ''))[:12]})"
        for s in request.specs
    ] or ["- (none)"]

    deleg = request.delegation
    allowed = ", ".join(deleg.get("allowed_paths") or []) or "(none)"
    forbidden = ", ".join(deleg.get("forbidden_paths") or []) or "(none)"

    stderr_excerpt = ""
    stderr_path_str = request.context.get("stderr_path")
    if stderr_path_str:
        sp = Path(stderr_path_str)
        if sp.is_file():
            try:
                raw = sp.read_text(encoding="utf-8", errors="replace")
                if len(raw) > MAX_STDERR_EXCERPT_CHARS:
                    raw = "...(truncated)...\n" + raw[-MAX_STDERR_EXCERPT_CHARS:]
                stderr_excerpt = raw.strip()
            except OSError:
                stderr_excerpt = ""

    sections = [
        f"# Garden Agent Task ({request.purpose})",
        "",
        f"- Run ID: `{request.run_id}`",
        f"- Step ID: `{request.step_id}`",
        f"- Worktree: `{request.worktree_path}`",
        f"- Skill: `{request.skill or 'none'}`",
        "",
        "## Approved Specifications",
        *spec_lines,
        "",
        "## Delegation Boundaries (Strictly Enforced by Post-Run Guard)",
        f"- Mode: `{deleg.get('mode')}`",
        f"- Allowed paths: `{allowed}`",
        f"- Forbidden paths (NEVER touch or rename): `{forbidden}`",
        f"- Max step cost (USD): `{deleg.get('max_cost_usd')}`",
    ]

    if request.context:
        sections.extend(
            [
                "",
                "## Failure Context",
                f"- Failed step: `{request.context.get('failed_step_id')}`",
                f"- Failed check: `{request.context.get('failed_check')}`",
                f"- Evidence dir: `{request.context.get('evidence_dir')}`",
            ]
        )
        if stderr_excerpt:
            sections.extend(["", "### Recent stderr", "```text", stderr_excerpt, "```"])

    sections.extend(
        [
            "",
            "## Required Response Format",
            "Write the JSON below to `$GARDEN_AGENT_RESPONSE` OR emit it as your final ```json block:",
            "```json",
            json.dumps(
                {
                    "schema_version": 1,
                    "outcome": "completed | needs_spec_decision | blocked | failed",
                    "summary": "Brief description of action taken",
                    "changed_files": ["relative/path"],
                    "question": None,
                    "usage": {
                        "model": None,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": 0.0,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            "```",
            "If the specification is ambiguous, do NOT guess: return outcome='needs_spec_decision' with question={prompt, options, impact, blocked_work}.",
        ]
    )
    return "\n".join(sections) + "\n"


def _extract_json_objects(text: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    stripped = text.strip()
    if stripped:
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                candidates.append(obj)
        except json.JSONDecodeError:
            pass

    for match in JSON_FENCE_RE.finditer(text):
        try:
            obj = json.loads(match.group(1).strip())
            if isinstance(obj, dict):
                candidates.append(obj)
        except json.JSONDecodeError:
            continue

    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        brace = text.find("{", idx)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, brace)
            if isinstance(obj, dict):
                candidates.append(obj)
            idx = end
        except json.JSONDecodeError:
            idx = brace + 1
    return candidates


def _pick_garden_payload(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    for obj in reversed(candidates):
        if obj.get("schema_version") == 1 and obj.get("outcome") in VALID_OUTCOMES:
            return obj
    for obj in reversed(candidates):
        if obj.get("outcome") in VALID_OUTCOMES:
            return obj
    return None


def _build_response_from_payload(
    payload: dict[str, Any],
    *,
    fallback_usage: AgentUsage | None = None,
) -> AgentResponse:
    outcome = str(payload.get("outcome") or "failed")
    if outcome not in VALID_OUTCOMES:
        outcome = "failed"
    summary = str(payload.get("summary") or "")
    changed_raw = payload.get("changed_files") or []
    changed = (
        tuple(str(item) for item in changed_raw)
        if isinstance(changed_raw, list)
        else ()
    )
    question = payload.get("question")
    if outcome == "needs_spec_decision":
        if not isinstance(question, dict) or not question.get("prompt"):
            return AgentResponse(
                outcome="failed",
                summary="needs_spec_decision response missing question.prompt",
                usage=fallback_usage or AgentUsage(),
            )

    usage_raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    base = fallback_usage or AgentUsage()
    usage = AgentUsage(
        model=usage_raw.get("model") or base.model,
        input_tokens=int(usage_raw.get("input_tokens") or base.input_tokens or 0),
        output_tokens=int(usage_raw.get("output_tokens") or base.output_tokens or 0),
        cost_usd=float(
            usage_raw["cost_usd"]
            if usage_raw.get("cost_usd") is not None
            else base.cost_usd
        ),
    )
    return AgentResponse(
        outcome=outcome,
        summary=summary,
        changed_files=changed,
        question=question if isinstance(question, dict) else None,
        usage=usage,
    )


def parse_provider_stdout(
    adapter: str, stdout: str, exit_code: int
) -> AgentResponse:
    """Normalize provider CLI stdout (codex/claude/antigravity) into AgentResponse."""
    fallback_usage = AgentUsage()
    text_to_scan = stdout

    # Claude Code `--output-format json` wraps the assistant text in an envelope
    if adapter == "claude":
        try:
            envelope = json.loads(stdout.strip())
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, dict) and "result" in envelope:
            usage_obj = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
            fallback_usage = AgentUsage(
                model=envelope.get("model"),
                input_tokens=int(usage_obj.get("input_tokens") or 0),
                output_tokens=int(usage_obj.get("output_tokens") or 0),
                cost_usd=float(envelope.get("total_cost_usd") or 0.0),
            )
            inner = envelope.get("result")
            if isinstance(inner, dict):
                payload = _pick_garden_payload([inner])
                if payload is not None:
                    return _build_response_from_payload(payload, fallback_usage=fallback_usage)
            elif isinstance(inner, str):
                text_to_scan = inner

    candidates = _extract_json_objects(text_to_scan)
    payload = _pick_garden_payload(candidates)
    if payload is not None:
        resp = _build_response_from_payload(payload, fallback_usage=fallback_usage)
        if exit_code != 0 and resp.outcome == "completed":
            return AgentResponse(
                outcome="failed",
                summary=f"{adapter} exited with code {exit_code}: {resp.summary}",
                changed_files=resp.changed_files,
                question=resp.question,
                usage=resp.usage,
            )
        return resp

    return AgentResponse(
        outcome="failed",
        summary=(
            f"{adapter} adapter did not emit a valid Garden JSON response "
            f"(exit_code={exit_code})"
        ),
        usage=fallback_usage,
    )


def prepare_provider_command(
    adapter: str, command: tuple[str, ...], prompt: str
) -> tuple[list[str], str | None]:
    """Return (argv, stdin_text) for invoking a provider CLI safely."""
    has_placeholder = any("{prompt}" in arg for arg in command)
    if has_placeholder:
        argv = [arg.replace("{prompt}", prompt) for arg in command]
        return argv, None
    if adapter in ("codex", "claude", "antigravity"):
        return list(command), prompt
    return list(command), None
