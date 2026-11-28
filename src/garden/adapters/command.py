"""External CLI adapter implementing the file-based and provider-stdout JSON agent protocol."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from garden.adapters.base import AgentRequest, AgentResponse, AgentUsage, load_agent_response
from garden.adapters.providers import (
    build_agent_prompt,
    extract_provider_usage,
    merge_authoritative_usage,
    parse_provider_stdout,
    prepare_provider_command,
)
from garden.definition import AgentConfig


PROVIDER_ADAPTERS = ("codex", "claude", "gemini", "antigravity")


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
    prompt_path = step_dir / "prompt.md"
    stdout_file = step_dir / "stdout.log"
    stderr_file = step_dir / "stderr.log"

    req_path.write_text(
        json.dumps(request.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    prompt_text = build_agent_prompt(request)
    prompt_path.write_text(prompt_text, encoding="utf-8")

    env = os.environ.copy()
    env["GARDEN_AGENT_REQUEST"] = str(req_path.resolve())
    env["GARDEN_AGENT_RESPONSE"] = str(res_path.resolve())
    env["GARDEN_AGENT_PROMPT"] = str(prompt_path.resolve())
    env["GARDEN_WORKTREE"] = str(worktree.resolve())

    argv, stdin_text = prepare_provider_command(agent.adapter, agent.command, prompt_text)

    try:
        proc = subprocess.run(
            argv,
            cwd=worktree,
            env=env,
            input=stdin_text,
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

    if res_path.is_file():
        response = load_agent_response(res_path)
        if agent.adapter in PROVIDER_ADAPTERS:
            cli_usage, usage_err = extract_provider_usage(agent.adapter, proc.stdout)
            if usage_err is not None:
                response = AgentResponse(
                    outcome="failed",
                    summary=f"{agent.adapter} adapter emitted malformed usage statistics: {usage_err}",
                    usage=AgentUsage(cost_usd=None),
                )
            elif cli_usage is not None:
                response = AgentResponse(
                    outcome=response.outcome,
                    summary=response.summary,
                    changed_files=response.changed_files,
                    question=response.question,
                    usage=merge_authoritative_usage(response.usage, cli_usage),
                )
                res_path.write_text(
                    json.dumps(response.to_dict(), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
    elif agent.adapter in PROVIDER_ADAPTERS:
        response = parse_provider_stdout(agent.adapter, proc.stdout, proc.returncode)
        res_path.write_text(
            json.dumps(response.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
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
