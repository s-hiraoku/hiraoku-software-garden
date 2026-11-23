"""External CLI adapter implementing the file-based JSON agent protocol."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from garden.adapters.base import AgentRequest, AgentResponse, load_agent_response
from garden.definition import AgentConfig


def run_command_adapter(
    agent: AgentConfig,
    request: AgentRequest,
    *,
    worktree: Path,
    step_dir: Path,
) -> tuple[AgentResponse, int | None]:
    step_dir.mkdir(parents=True, exist_ok=True)
    req_path = step_dir / "request.json"
    res_path = step_dir / "response.json"
    stdout_file = step_dir / "stdout.log"
    stderr_file = step_dir / "stderr.log"

    req_path.write_text(
        json.dumps(request.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["GARDEN_AGENT_REQUEST"] = str(req_path.resolve())
    env["GARDEN_AGENT_RESPONSE"] = str(res_path.resolve())
    env["GARDEN_WORKTREE"] = str(worktree.resolve())

    try:
        proc = subprocess.run(
            list(agent.command),
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
            timeout=agent.timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout_file.write_text(
            error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or ""),
            encoding="utf-8",
        )
        stderr_file.write_text(
            error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or ""),
            encoding="utf-8",
        )
        return (
            AgentResponse(
                outcome="failed",
                summary=f"agent command timed out after {agent.timeout_sec}s",
            ),
            None,
        )
    except OSError as error:
        stdout_file.write_text("", encoding="utf-8")
        stderr_file.write_text(str(error), encoding="utf-8")
        return (
            AgentResponse(
                outcome="failed",
                summary=f"agent command unavailable: {error}",
            ),
            None,
        )

    stdout_file.write_text(proc.stdout, encoding="utf-8")
    stderr_file.write_text(proc.stderr, encoding="utf-8")

    response = load_agent_response(res_path)
    if proc.returncode != 0 and response.outcome == "completed":
        response = AgentResponse(
            outcome="failed",
            summary=f"agent exited with code {proc.returncode}: {response.summary}",
            changed_files=response.changed_files,
            question=response.question,
            usage=response.usage,
        )
    return response, proc.returncode
