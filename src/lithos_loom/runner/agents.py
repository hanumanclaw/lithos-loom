"""Coding-agent subprocess runner (US-12).

Stub — lifted from Ralph++ and adapted to Loom's plugin work-dir layout.
Captures stream-json to ``{work_dir}/{task.id}/agent-output.jsonl`` and parses
for cost / turn count / tool-call summaries.

On timeout: SIGTERM, then SIGKILL after 5s grace.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentResult:
    exit_code: int
    duration_seconds: float
    turns: int
    cost_usd: float
    output_path: Path
    interrupted: bool


def run_claude(
    prompt: str,
    cwd: Path,
    claude_config_dir: Path | None = None,
    output_format: str = "stream-json",
    timeout: int = 3600,
) -> AgentResult:
    """Stub — implement per docs/prd/mvp.md US-12."""
    raise NotImplementedError("runner.agents.run_claude — implement per US-12")


def run_codex(
    prompt: str,
    cwd: Path,
    codex_config_dir: Path | None = None,
    timeout: int = 3600,
) -> AgentResult:
    """Stub — Codex mirror of :func:`run_claude`."""
    raise NotImplementedError("runner.agents.run_codex — implement per US-12")
