"""story-develop entry point (T1 — standalone only).

Standalone mode::

    python -m lithos_loom.plugins.story_develop \\
        --repo ~/projects/foo --description "Add a CLI flag"

Daemon mode (``--task-json/--work-dir/--result-file``) arrives with T10. The
shared core is :func:`lithos_loom.plugins.story_develop.develop.develop`.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

from .config import DEFAULT_CODER_TOOL, DEFAULT_IMAGE, DevelopConfig
from .develop import develop


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m lithos_loom.plugins.story_develop",
        description="Run the story-develop cycle (T1: one coder, one turn).",
    )
    p.add_argument("--repo", required=True, type=Path, help="Path to the project repo")
    p.add_argument("--description", required=True, help="Free-text task description")
    p.add_argument(
        "--coder",
        default=DEFAULT_CODER_TOOL,
        choices=["claude"],  # only claude in T1; codex arrives with T5/T6
        help="Coding agent tool",
    )
    p.add_argument("--image", default=DEFAULT_IMAGE, help="Agent container image")
    p.add_argument("--branch", default="main", help="Base branch for the worktree")
    p.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Per-run state dir (default: a fresh temp dir)",
    )
    p.add_argument(
        "--coder-timeout",
        type=int,
        default=3600,
        help="Max seconds for the coder turn",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    if not args.description.strip():
        print("error: --description must not be empty", file=sys.stderr)
        return 2

    repo = args.repo.expanduser().resolve()
    if not (repo / ".git").exists():
        print(f"error: {repo} is not a git repository", file=sys.stderr)
        return 2

    work_dir = (
        args.work_dir.expanduser().resolve()
        if args.work_dir is not None
        else Path(tempfile.mkdtemp(prefix="lithos-loom-develop-"))
    )

    config = DevelopConfig(
        repo=repo,
        description=args.description,
        work_dir=work_dir,
        coder=args.coder,
        image=args.image,
        base_branch=args.branch,
    )

    result = develop(config, coder_timeout=args.coder_timeout)

    print()
    print(f"story-develop run {result.run_id}: {result.status.upper()}")
    print(f"  branch:   {result.branch}")
    print(f"  worktree: {result.worktree}")
    print(f"  commits:  {len(result.commits)}")
    print(f"  cost:     ${result.coder_cost_usd:.4f}")
    print(f"  {result.message}")
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
