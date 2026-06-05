"""story-develop entry point — dual-mode CLI.

Supports two invocation modes:

**Standalone** (operator runs directly)::

    python -m lithos_loom.plugins.story_develop \
        --repo ~/projects/my-project \
        --description "Implement feature X" \
        --max-rounds 3

**Daemon** (loom route runner calls this)::

    python -m lithos_loom.plugins.story_develop \
        --task-json /tmp/loom/abc/task.json \
        --work-dir /tmp/loom/abc \
        --result-file /tmp/loom/abc/result.json

Mode detection: if ``--task-json`` is present → daemon mode.
Otherwise → standalone mode (requires ``--repo`` + one of
``--description`` / ``--task-id``).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── Shared types ───────────────────────────────────────────────────────


@dataclass
class TaskContext:
    """Normalised task context consumed by develop().

    Both entry modes build one of these; develop() doesn't care
    which mode constructed it.
    """

    run_id: str  # unique per invocation (task-id or short uuid)
    title: str
    description: str
    repo: Path
    branch: str  # base branch for worktree
    coder_tool: str  # "claude", "codex", etc.
    reviewer_tools: list[str]  # one per reviewer
    max_rounds: int
    work_dir: Path  # where worktree + .handoff/ live
    lithos_url: str | None  # None = offline
    task_id: str | None  # Lithos task ID (if available)


@dataclass
class DevelopResult:
    """Returned by develop(); callers format for their output mode."""

    status: str  # "succeeded", "failed", "interrupted"
    rounds_completed: int
    approved_by: list[str]  # reviewer names that gave LGTM
    remaining_findings: list[dict[str, Any]]
    commits: list[str]  # SHAs of new commits
    conversation_log: list[Path]  # ordered handoff files
    error: dict[str, str] | None  # {category, message} on failure


# ── Core function (mode-agnostic) ──────────────────────────────────────


def develop(ctx: TaskContext) -> DevelopResult:
    """Run the full implement → review → dialogue cycle.

    This is the function both modes call. It:
    1. Creates a worktree off ctx.branch in ctx.work_dir
    2. Starts a tmux server (develop-{ctx.run_id})
    3. Launches coder + reviewer containers in tmux panes
    4. Injects the initial coding prompt
    5. Watches .handoff/ for handoff files
    6. Routes handoffs between agents
    7. Returns when all reviewers LGTM or max_rounds exhausted
    """
    raise NotImplementedError("story-develop core loop — Phase 1 build target")


# ── Standalone mode ────────────────────────────────────────────────────


def _run_standalone(args: argparse.Namespace) -> int:
    """Build TaskContext from CLI flags, run develop(), print results."""
    import uuid

    run_id = args.task_id or uuid.uuid4().hex[:12]

    # If --task-id given, fetch from Lithos
    description = args.description
    title = args.description  # standalone: title = description
    if args.task_id and not args.no_lithos:
        # TODO: fetch from Lithos via lithos_url
        title = f"Task {args.task_id}"
        description = description or f"(fetched from Lithos task {args.task_id})"

    work_dir = (
        Path(args.work_dir)
        if args.work_dir
        else Path(tempfile.mkdtemp(prefix=f"develop-{run_id}-"))
    )

    ctx = TaskContext(
        run_id=run_id,
        title=title,
        description=description or "",
        repo=Path(args.repo).expanduser().resolve(),
        branch=args.branch,
        coder_tool=args.coder,
        reviewer_tools=args.reviewer or ["codex"],
        max_rounds=args.max_rounds,
        work_dir=work_dir,
        lithos_url=None if args.no_lithos else args.lithos_url,
        task_id=args.task_id,
    )

    result = develop(ctx)

    # Human-readable output
    print(f"\n{'═' * 60}")
    print(f"  story-develop: {result.status}")
    print(f"  rounds: {result.rounds_completed}")
    if result.approved_by:
        print(f"  approved by: {', '.join(result.approved_by)}")
    if result.commits:
        print(f"  commits: {len(result.commits)}")
        for sha in result.commits:
            print(f"    {sha}")
    if result.error:
        print(f"  error: [{result.error['category']}] {result.error['message']}")
    print(f"  work-dir: {ctx.work_dir}")
    print(f"{'═' * 60}\n")

    return 0 if result.status == "succeeded" else 1


# ── Daemon mode ────────────────────────────────────────────────────────


def _run_daemon(args: argparse.Namespace) -> int:
    """Read task.json, run develop(), write result.json."""
    import json

    from lithos_loom.plugin_runner import write_result_atomically

    task_json_path = Path(args.task_json)
    work_dir = Path(args.work_dir)
    result_file = Path(args.result_file)

    task_data = json.loads(task_json_path.read_text())

    # Extract project config from the daemon-provided task.json
    # The daemon enriches task.json with the resolved project entry
    project = task_data.get("project", {})
    develop_cfg = project.get("develop", {})
    task = task_data.get("task", task_data)

    ctx = TaskContext(
        run_id=task.get("id", "unknown"),
        title=task.get("title", ""),
        description=task.get("description", ""),
        repo=Path(project.get("repo", ".")),
        branch=task.get("metadata", {}).get("integration_branch", "main"),
        coder_tool=develop_cfg.get("coder", {}).get("tool", "claude"),
        reviewer_tools=[
            r.get("tool", "codex")
            for r in develop_cfg.get("reviewers", [{"tool": "codex"}])
        ],
        max_rounds=develop_cfg.get("max_rounds", 5),
        work_dir=work_dir,
        lithos_url=None,  # daemon handles Lithos interaction
        task_id=task.get("id"),
    )

    result = develop(ctx)

    # Write result.json per the Loom plugin contract
    payload: dict[str, Any] = {
        "schema_version": 1,
        "task_id": ctx.task_id or ctx.run_id,
        "status": result.status,
        "exit_code": 0 if result.status == "succeeded" else 1,
        "commits": result.commits,
    }
    if result.error:
        payload["error"] = result.error

    write_result_atomically(result_file, payload)
    return 0 if result.status == "succeeded" else 1


# ── Arg parsing ────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lithos_loom.plugins.story_develop",
        description="Implement + review a task via conversational agent dialogue.",
    )

    # -- Daemon mode flags (mutually exclusive with standalone) --
    daemon = parser.add_argument_group("daemon mode (called by loom route runner)")
    daemon.add_argument(
        "--task-json", type=str, default=None, help="Path to task.json (daemon mode)"
    )
    daemon.add_argument(
        "--result-file",
        type=str,
        default=None,
        help="Path to write result.json (daemon mode)",
    )

    # -- Standalone mode flags --
    standalone = parser.add_argument_group("standalone mode (operator runs directly)")
    standalone.add_argument(
        "--repo", type=str, default=None, help="Path to the project repository"
    )
    standalone.add_argument(
        "--description", type=str, default=None, help="Free-text task description"
    )
    standalone.add_argument(
        "--task-id",
        type=str,
        default=None,
        help="Lithos task ID (fetches title + description)",
    )
    standalone.add_argument(
        "--branch",
        type=str,
        default="main",
        help="Base branch for worktree (default: main)",
    )
    standalone.add_argument(
        "--coder",
        type=str,
        default="claude",
        help="Coding agent tool (default: claude)",
    )
    standalone.add_argument(
        "--reviewer",
        type=str,
        action="append",
        default=None,
        help="Reviewer tool (repeatable; default: codex)",
    )
    standalone.add_argument(
        "--max-rounds",
        type=int,
        default=5,
        help="Max review dialogue rounds (default: 5)",
    )
    standalone.add_argument(
        "--lithos-url", type=str, default=None, help="Lithos server URL (for --task-id)"
    )
    standalone.add_argument(
        "--no-lithos", action="store_true", help="Skip all Lithos interaction"
    )
    standalone.add_argument(
        "--develop-config",
        type=str,
        default=None,
        help="Path to reviewer config TOML (optional)",
    )

    # -- Shared flags --
    parser.add_argument(
        "--work-dir",
        type=str,
        default=None,
        help="Working directory (default: temp dir)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Mode detection: --task-json present → daemon mode
    if args.task_json is not None:
        # Daemon mode: validate required daemon flags
        if args.result_file is None:
            parser.error(
                "--result-file is required in daemon mode (--task-json present)"
            )
        if args.work_dir is None:
            parser.error("--work-dir is required in daemon mode")
        return _run_daemon(args)
    else:
        # Standalone mode: validate required standalone flags
        if args.repo is None:
            parser.error("--repo is required in standalone mode")
        if args.description is None and args.task_id is None:
            parser.error(
                "one of --description or --task-id is required in standalone mode"
            )
        return _run_standalone(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
