"""Objective per-round test gate — host-orchestrated, container-executed.

After each round commit, the committed tree (and only the committed tree — no
worktree cruft, no agent-mutated state) is exported via ``git archive`` into a
fresh directory and the project's test command runs against it in a one-shot
throwaway container. The agents play no part: the result is an independent
check on the coder's self-reported test results (PRD decision #10).

Untrusted repo tests must never run on the bare host — that would defeat the
sandbox. The throwaway container uses the same hardened profile as the agent
containers (``cap_drop ALL``, ``no-new-privileges``) but mounts nothing except
the exported tree and a per-run package cache.

Layered like :mod:`containers`: pure command builders (unit-tested without
Docker) + thin side-effecting wrappers (monkeypatched in orchestration tests).
"""

from __future__ import annotations

import contextlib
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import WORKSPACE_MOUNT

# Mounted into the gate container so uv/npm package downloads are shared
# between rounds of the same run (each container is still throwaway).
CACHE_MOUNT = "/gate_cache"
OUTPUT_TAIL_CHARS = 4000
_TIMEOUT_EXIT = 124


@dataclass(frozen=True)
class GateResult:
    """Outcome of one test-gate run against a round commit."""

    command: str
    exit_code: int
    passed: bool
    output_tail: str

    @property
    def timed_out(self) -> bool:
        return self.exit_code == _TIMEOUT_EXIT

    @property
    def verdict(self) -> str:
        if self.timed_out:
            return "TIMEOUT"
        return "GREEN" if self.passed else "RED"


def export_tree(worktree: Path, sha: str, dest: Path) -> None:
    """Export the tree of *sha* into *dest* (``git archive | tar -x``).

    Exports exactly the committed content — no ``.git``, no untracked files —
    so the gate cannot be influenced by uncommitted worktree state.
    """
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", sha],
        cwd=worktree,
        capture_output=True,
        timeout=120,
    )
    if archive.returncode != 0:
        raise RuntimeError(
            f"git archive {sha} failed (exit {archive.returncode}): "
            f"{archive.stderr.decode(errors='replace').strip()}"
        )
    untar = subprocess.run(
        ["tar", "-x", "-C", str(dest)],
        input=archive.stdout,
        capture_output=True,
        timeout=120,
    )
    if untar.returncode != 0:
        raise RuntimeError(
            f"tar -x failed (exit {untar.returncode}): "
            f"{untar.stderr.decode(errors='replace').strip()}"
        )


def build_probe_command(*, image: str, tools: list[str]) -> list[str]:
    """Build a one-shot ``docker run`` that prints which *tools* exist in *image*."""
    script = "; ".join(
        f"command -v {shlex.quote(t)} >/dev/null 2>&1 && echo {shlex.quote(t)}"
        for t in tools
    )
    return [
        "docker",
        "run",
        "--rm",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--entrypoint",
        "sh",
        image,
        "-c",
        script or "true",
    ]


def build_gate_command(
    *,
    name: str,
    image: str,
    tree: Path,
    cache_dir: Path,
    command: str,
) -> list[str]:
    """Build the one-shot ``docker run`` argv for a gate run.

    The container is named so a timed-out run can be force-removed. The tree
    mounts RW (test runs create caches/venvs); the cache dir persists across
    rounds of the same run so dependency downloads happen once.
    """
    return [
        "docker",
        "run",
        "--rm",
        "--init",
        "--name",
        name,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "-v",
        f"{tree}:{WORKSPACE_MOUNT}",
        "-v",
        f"{cache_dir}:{CACHE_MOUNT}",
        "-e",
        f"UV_CACHE_DIR={CACHE_MOUNT}/uv",
        "-e",
        f"npm_config_cache={CACHE_MOUNT}/npm",
        "-w",
        WORKSPACE_MOUNT,
        "--entrypoint",
        "sh",
        image,
        "-c",
        command,
    ]


def select_command(candidates: list[str], available_tools: list[str]) -> str | None:
    """First candidate whose argv[0] is in *available_tools* (gate's pick)."""
    for cmd in candidates:
        argv0 = cmd.split()[0] if cmd.split() else ""
        if argv0 in available_tools:
            return cmd
    return None


# --- thin side-effecting wrappers (monkeypatched in unit tests) -------------


def probe_tools(image: str, tools: list[str]) -> list[str]:
    """Return the subset of *tools* that exist in *image* (one probe container).

    Degrades to "no tools found" on any infra failure (docker missing, probe
    timeout) — the caller then skips the gate with a warning rather than the
    probe crashing the run.
    """
    if not tools:
        return []
    try:
        proc = subprocess.run(
            build_probe_command(image=image, tools=tools),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    found = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    return [t for t in tools if t in found]


def run_gate_container(
    gate_cmd: list[str], *, name: str, command: str, timeout: int
) -> GateResult:
    """Run the gate container and capture its outcome; never raises on red."""
    try:
        proc = subprocess.run(
            gate_cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # The --rm container keeps running after the host-side timeout; kill it.
        # Best-effort: a cleanup failure must not turn the TIMEOUT into a crash.
        with contextlib.suppress(OSError):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        return GateResult(
            command=command,
            exit_code=_TIMEOUT_EXIT,
            passed=False,
            output_tail=f"test gate timed out after {timeout}s",
        )
    tail = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
    return GateResult(
        command=command,
        exit_code=proc.returncode,
        passed=proc.returncode == 0,
        output_tail=tail[-OUTPUT_TAIL_CHARS:],
    )
