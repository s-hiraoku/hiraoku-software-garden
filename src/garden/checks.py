"""Check and formal_check runner implementing Layer-1 deterministic verdict rules."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from garden.definition import CheckConfig


FORMAL_RESULTS = ("proved", "counterexample_found", "inconclusive")


@dataclass(frozen=True)
class CheckExecutionResult:
    check_name: str
    kind: str
    status: str  # passed | failed | inconclusive
    reason: str
    exit_code: int | None
    duration_sec: float
    stdout_path: str
    stderr_path: str
    evidence_dir: str
    report: dict[str, Any] | None = None
    formal: dict[str, Any] | None = None
    detail: str | None = None


def _validate_formal_json(
    formal_path: Path, check: CheckConfig
) -> tuple[dict[str, Any] | None, str | None]:
    if not formal_path.is_file() or formal_path.stat().st_size == 0:
        return None, f"missing formal metadata: {formal_path.name}"
    try:
        data = json.loads(formal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return None, f"invalid formal metadata JSON: {error}"
    if not isinstance(data, dict):
        return None, "formal metadata must be a JSON object"
    required_keys = (
        "schema_version",
        "tool",
        "model_path",
        "invariants",
        "assumptions",
        "scope",
        "completed",
        "result",
    )
    missing = [k for k in required_keys if k not in data]
    if missing:
        return data, f"formal metadata missing keys: {', '.join(missing)}"
    if data.get("schema_version") != 1:
        return data, "formal metadata schema_version must be 1"
    if not isinstance(data.get("completed"), bool):
        return data, "formal metadata 'completed' must be boolean"
    if data.get("result") not in FORMAL_RESULTS:
        return data, f"formal metadata 'result' must be one of {', '.join(FORMAL_RESULTS)}"
    if check.tool and data.get("tool") != check.tool:
        return data, f"formal metadata tool mismatch: expected {check.tool}, got {data.get('tool')}"
    return data, None


def run_check(
    check: CheckConfig,
    *,
    worktree: Path,
    run_id: str,
    step_id: str,
    step_dir: Path,
    env_override: dict[str, str] | None = None,
    command_prefix: tuple[str, ...] = (),
) -> CheckExecutionResult:
    step_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = step_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stdout_file = step_dir / "stdout.log"
    stderr_file = step_dir / "stderr.log"

    env = dict(env_override) if env_override is not None else os.environ.copy()
    env["GARDEN_RUN_ID"] = run_id
    env["GARDEN_STEP_ID"] = step_id
    env["GARDEN_EVIDENCE_DIR"] = str(evidence_dir.resolve())
    report_file: Path | None = None
    if check.report_path:
        report_file = evidence_dir / check.report_path
        report_file.parent.mkdir(parents=True, exist_ok=True)
        env["GARDEN_REPORT_PATH"] = str(report_file.resolve())

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [*command_prefix, *check.command],
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
            timeout=check.timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        duration = round(time.monotonic() - started, 3)
        stdout_text = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        stderr_text = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
        stdout_file.write_text(stdout_text, encoding="utf-8")
        stderr_file.write_text(stderr_text, encoding="utf-8")
        return CheckExecutionResult(
            check_name=check.name,
            kind=check.kind,
            status="inconclusive",
            reason="timeout",
            exit_code=None,
            duration_sec=duration,
            stdout_path=str(stdout_file),
            stderr_path=str(stderr_file),
            evidence_dir=str(evidence_dir),
            detail=f"command timed out after {check.timeout_sec}s",
        )
    except OSError as error:
        duration = round(time.monotonic() - started, 3)
        stdout_file.write_text("", encoding="utf-8")
        stderr_file.write_text(str(error), encoding="utf-8")
        return CheckExecutionResult(
            check_name=check.name,
            kind=check.kind,
            status="inconclusive",
            reason="command_unavailable",
            exit_code=None,
            duration_sec=duration,
            stdout_path=str(stdout_file),
            stderr_path=str(stderr_file),
            evidence_dir=str(evidence_dir),
            detail=str(error),
        )

    duration = round(time.monotonic() - started, 3)
    stdout_file.write_text(proc.stdout, encoding="utf-8")
    stderr_file.write_text(proc.stderr, encoding="utf-8")

    # Priority 2: explicit inconclusive exit codes (default [75])
    if proc.returncode in check.inconclusive_exit_codes:
        return CheckExecutionResult(
            check_name=check.name,
            kind=check.kind,
            status="inconclusive",
            reason="exit_code_inconclusive",
            exit_code=proc.returncode,
            duration_sec=duration,
            stdout_path=str(stdout_file),
            stderr_path=str(stderr_file),
            evidence_dir=str(evidence_dir),
            detail=f"exited with inconclusive code {proc.returncode}",
        )

    # Priority 3: structured report JSON declaring inconclusive
    report_data: dict[str, Any] | None = None
    if report_file is not None and report_file.is_file():
        try:
            parsed = json.loads(report_file.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                report_data = parsed
        except (OSError, json.JSONDecodeError) as error:
            return CheckExecutionResult(
                check_name=check.name,
                kind=check.kind,
                status="inconclusive",
                reason="invalid_report",
                exit_code=proc.returncode,
                duration_sec=duration,
                stdout_path=str(stdout_file),
                stderr_path=str(stderr_file),
                evidence_dir=str(evidence_dir),
                detail=f"could not parse report JSON: {error}",
            )
        if report_data.get("status") == "inconclusive":
            return CheckExecutionResult(
                check_name=check.name,
                kind=check.kind,
                status="inconclusive",
                reason="reported_inconclusive",
                exit_code=proc.returncode,
                duration_sec=duration,
                stdout_path=str(stdout_file),
                stderr_path=str(stderr_file),
                evidence_dir=str(evidence_dir),
                report=report_data,
                detail=str(report_data.get("detail") or "check reported inconclusive"),
            )

    # Priority 4: required evidence missing or empty when exit code is 0
    if proc.returncode == 0 and check.required_evidence:
        missing_ev: list[str] = []
        for rel in check.required_evidence:
            ev_path = evidence_dir / rel
            if not ev_path.is_file() or ev_path.stat().st_size == 0:
                missing_ev.append(rel)
        if missing_ev:
            return CheckExecutionResult(
                check_name=check.name,
                kind=check.kind,
                status="inconclusive",
                reason="missing_evidence",
                exit_code=proc.returncode,
                duration_sec=duration,
                stdout_path=str(stdout_file),
                stderr_path=str(stderr_file),
                evidence_dir=str(evidence_dir),
                report=report_data,
                detail=f"missing or empty required evidence: {', '.join(missing_ev)}",
            )

    # Priority 5: formal_check metadata verification
    formal_data: dict[str, Any] | None = None
    if check.kind == "formal_check":
        formal_path = evidence_dir / "formal.json"
        formal_data, formal_err = _validate_formal_json(formal_path, check)
        if formal_err is not None:
            return CheckExecutionResult(
                check_name=check.name,
                kind=check.kind,
                status="inconclusive",
                reason="formal_incomplete",
                exit_code=proc.returncode,
                duration_sec=duration,
                stdout_path=str(stdout_file),
                stderr_path=str(stderr_file),
                evidence_dir=str(evidence_dir),
                report=report_data,
                formal=formal_data,
                detail=formal_err,
            )
        assert formal_data is not None
        if not formal_data["completed"] or formal_data["result"] == "inconclusive":
            return CheckExecutionResult(
                check_name=check.name,
                kind=check.kind,
                status="inconclusive",
                reason="formal_incomplete",
                exit_code=proc.returncode,
                duration_sec=duration,
                stdout_path=str(stdout_file),
                stderr_path=str(stderr_file),
                evidence_dir=str(evidence_dir),
                report=report_data,
                formal=formal_data,
                detail="formal verification did not complete or returned inconclusive",
            )
        if formal_data["result"] == "counterexample_found":
            return CheckExecutionResult(
                check_name=check.name,
                kind=check.kind,
                status="failed",
                reason="formal_counterexample",
                exit_code=proc.returncode,
                duration_sec=duration,
                stdout_path=str(stdout_file),
                stderr_path=str(stderr_file),
                evidence_dir=str(evidence_dir),
                report=report_data,
                formal=formal_data,
                detail="formal verification found a counterexample",
            )

    # Priority 6 & 7: standard exit code evaluation
    if proc.returncode == 0:
        return CheckExecutionResult(
            check_name=check.name,
            kind=check.kind,
            status="passed",
            reason="ok",
            exit_code=0,
            duration_sec=duration,
            stdout_path=str(stdout_file),
            stderr_path=str(stderr_file),
            evidence_dir=str(evidence_dir),
            report=report_data,
            formal=formal_data,
        )

    return CheckExecutionResult(
        check_name=check.name,
        kind=check.kind,
        status="failed",
        reason="assertion_failed",
        exit_code=proc.returncode,
        duration_sec=duration,
        stdout_path=str(stdout_file),
        stderr_path=str(stderr_file),
        evidence_dir=str(evidence_dir),
        report=report_data,
        formal=formal_data,
        detail=f"command exited with code {proc.returncode}",
    )
