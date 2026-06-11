"""Run a single coder turn and parse its structured result.

A turn is ``docker exec ... claude --session-id <id> -p --output-format json``.
Completion, error, and cost all come from the parsed JSON + the process exit
code — no terminal scraping (ADR 0002). Usage-limit *classification* lands in T5;
T1 only needs success/failure + the session id + cost.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from . import containers


@dataclass(frozen=True)
class CoderTurnResult:
    """Outcome of one coder turn."""

    exit_code: int
    succeeded: bool
    session_id: str
    result_text: str
    cost_usd: float
    raw: dict | None
    stderr: str

    @property
    def timed_out(self) -> bool:
        return self.exit_code == _TIMEOUT_EXIT


_TIMEOUT_EXIT = 124  # conventional timeout exit; we set it ourselves on timeout


def parse_claude_result(stdout: str, *, exit_code: int, stderr: str) -> CoderTurnResult:
    """Parse ``claude --output-format json`` stdout into a CoderTurnResult.

    The payload is a single JSON object (``type: "result"``) carrying
    ``is_error``, ``result``, ``session_id`` and ``total_cost_usd``. A
    non-zero exit *or* ``is_error: true`` *or* unparseable output is a failure.
    """
    raw: dict | None = None
    try:
        parsed = json.loads(stdout) if stdout.strip() else None
        if isinstance(parsed, dict):
            raw = parsed
    except json.JSONDecodeError:
        raw = None

    is_error = bool(raw.get("is_error")) if raw else True
    # ``or ""`` normalises an explicit JSON ``null`` to "" (not the string "None").
    session_id = str(raw.get("session_id") or "") if raw else ""
    result_text = str(raw.get("result") or "") if raw else ""
    cost_usd = float(raw.get("total_cost_usd") or 0.0) if raw else 0.0
    # A non-empty session_id is required for success so later resume turns (T3)
    # always have a handle to resume.
    succeeded = exit_code == 0 and raw is not None and not is_error and bool(session_id)

    return CoderTurnResult(
        exit_code=exit_code,
        succeeded=succeeded,
        session_id=session_id,
        result_text=result_text,
        cost_usd=cost_usd,
        raw=raw,
        stderr=stderr,
    )


def run_coder_turn(
    *,
    container: str,
    prompt: str,
    session_id: str,
    resume: bool = False,
    timeout: int = 3600,
) -> CoderTurnResult:
    """Execute one coder turn in *container* and return its parsed result."""
    exec_cmd = containers.build_exec_command(
        name=container,
        tool="claude",
        prompt=prompt,
        session_id=session_id,
        resume=resume,
    )
    try:
        proc = containers.exec_turn(exec_cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CoderTurnResult(
            exit_code=_TIMEOUT_EXIT,
            succeeded=False,
            session_id=session_id,
            result_text="",
            cost_usd=0.0,
            raw=None,
            stderr=f"coder turn timed out after {timeout}s",
        )
    return parse_claude_result(
        proc.stdout, exit_code=proc.returncode, stderr=proc.stderr
    )
