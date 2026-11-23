"""Deterministic mock adapter delegating to command protocol for reproducible tests."""

from __future__ import annotations

from pathlib import Path

from garden.adapters.base import AgentRequest, AgentResponse
from garden.adapters.command import run_command_adapter
from garden.definition import AgentConfig


def run_mock_adapter(
    agent: AgentConfig,
    request: AgentRequest,
    *,
    worktree: Path,
    step_dir: Path,
) -> tuple[AgentResponse, int | None]:
    """Run a configured mock script using the exact same JSON protocol as external CLIs."""
    return run_command_adapter(agent, request, worktree=worktree, step_dir=step_dir)
