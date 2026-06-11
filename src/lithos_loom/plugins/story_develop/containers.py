"""Per-agent container plumbing for ``story-develop``.

Two layers, deliberately split:

* **pure builders** (:func:`build_run_command`, :func:`build_exec_command`) that
  return ``docker`` argv lists — unit-tested without Docker;
* **thin wrappers** (:func:`start_container`, :func:`exec_turn`,
  :func:`stop_container`) that actually shell out — monkeypatched in
  orchestration tests, exercised for real only in the integration test.

Design per ADR 0002 + the PRD: long-lived idle container (``sleep infinity``)
that we ``docker exec`` into per turn; hardened profile (``cap_drop: ALL``,
``no-new-privileges``); per-run ``CLAUDE_CONFIG_DIR`` with only the single auth
file bind-mounted in (RW, for token refresh) — never the whole ``~/.claude``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from .config import (
    CLAUDE_CONFIG_MOUNT,
    WORKSPACE_MOUNT,
    DevelopConfig,
)


def container_name(run_id: str, agent: str) -> str:
    """Stable, unique-per-run container name, e.g. ``loom-develop-ab12cd34-coder``."""
    return f"loom-develop-{run_id}-{agent}"


def build_run_command(
    *,
    name: str,
    image: str,
    worktree: Path,
    config_dir: Path,
    handoff_dir: Path,
    auth_source_dir: Path,
    auth_files: Sequence[str],
    skills_dir: Path | None = None,
    read_only_worktree: bool = False,
) -> list[str]:
    """Build the ``docker run`` argv for a long-lived idle agent container.

    The container does nothing but ``sleep`` — turns are injected later via
    :func:`build_exec_command`.

    Mounts:

    * the worktree at ``/workspace`` (RW, or RO for reviewers);
    * *handoff_dir* at ``/workspace/.handoff`` (RW) — a separate dir outside the
      worktree, so the worktree stays git-clean;
    * *config_dir* (per-run) at ``/claude_config`` (RW, holds the transcript);
    * each of *auth_files* individually from *auth_source_dir* (RW, token
      refresh) — never the whole config dir;
    * *skills_dir* at ``/claude_config/skills`` (RO) when provided, so
      operator-installed skills are available (feasibility gate G2).
    """
    workspace_mount = f"{worktree}:{WORKSPACE_MOUNT}"
    if read_only_worktree:
        workspace_mount += ":ro"

    cmd: list[str] = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--init",
        "--name",
        name,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "-v",
        workspace_mount,
        "-v",
        f"{handoff_dir}:{WORKSPACE_MOUNT}/.handoff",
        "-v",
        f"{config_dir}:{CLAUDE_CONFIG_MOUNT}",
    ]
    for fname in auth_files:
        cmd += ["-v", f"{auth_source_dir / fname}:{CLAUDE_CONFIG_MOUNT}/{fname}"]
    if skills_dir is not None:
        cmd += ["-v", f"{skills_dir}:{CLAUDE_CONFIG_MOUNT}/skills:ro"]
    cmd += ["-e", f"CLAUDE_CONFIG_DIR={CLAUDE_CONFIG_MOUNT}"]
    cmd += ["--entrypoint", "sleep", image, "infinity"]
    return cmd


def build_exec_command(
    *,
    name: str,
    tool: str,
    prompt: str,
    session_id: str,
    resume: bool = False,
    workdir: str = WORKSPACE_MOUNT,
) -> list[str]:
    """Build the ``docker exec`` argv for one coder turn.

    ``--session-id`` controls the session on the first turn; ``--resume`` reloads
    it on later turns (T3). Output is ``--output-format json`` so completion /
    cost / errors come from structured output, not pane scraping.
    """
    if tool != "claude":  # codex support arrives with T5/T6
        raise ValueError(f"unsupported coder tool for T1: {tool!r}")

    session_flag = ["--resume", session_id] if resume else ["--session-id", session_id]
    return [
        "docker",
        "exec",
        "-w",
        workdir,
        name,
        "claude",
        *session_flag,
        "-p",
        "--dangerously-skip-permissions",
        "--output-format",
        "json",
        prompt,
    ]


def resolve_auth_files(config: DevelopConfig, candidates: Sequence[str]) -> list[str]:
    """Return the subset of *candidates* that exist in the operator config dir."""
    return [f for f in candidates if (config.claude_config_dir / f).is_file()]


# --- thin side-effecting wrappers (monkeypatched in unit tests) -------------


def start_container(run_cmd: Sequence[str]) -> str:
    """Run ``docker run -d`` and return the container id (stdout)."""
    result = subprocess.run(list(run_cmd), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"docker run failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def exec_turn(
    exec_cmd: Sequence[str], *, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run ``docker exec`` for one turn with stdin closed (no 3s stdin wait)."""
    return subprocess.run(
        list(exec_cmd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def stop_container(name: str) -> None:
    """Force-remove the container; never raises (teardown must be best-effort)."""
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
        text=True,
    )
