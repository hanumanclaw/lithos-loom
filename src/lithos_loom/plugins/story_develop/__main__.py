"""story-develop entry point (T3 — standalone only).

Standalone mode::

    python -m lithos_loom.plugins.story_develop \\
        --repo ~/projects/foo --description "Add a CLI flag"

Runs the implement → review → fix loop: a coder implements the task, a reviewer
reviews it, and the coder fixes and the reviewer re-reviews each round until the
reviewer approves (LGTM or below the block threshold) or ``--max-rounds`` is
hit. Leaves a branch with per-round commits and a conversation log. Daemon mode
(``--task-json/--work-dir/--result-file``) arrives with T10. The shared core is
:func:`lithos_loom.plugins.story_develop.develop.develop`.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

from .config import (
    DEFAULT_BLOCK_THRESHOLD,
    DEFAULT_CODER_TOOL,
    DEFAULT_IMAGE,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_REVIEWER_NAME,
    DEFAULT_TEST_TIMEOUT,
    DevelopConfig,
    is_valid_reviewer_name,
)
from .develop import develop


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m lithos_loom.plugins.story_develop",
        description="Run the story-develop loop (coder implements; reviewer reviews; "
        "iterate to approval or --max-rounds).",
    )
    p.add_argument("--repo", required=True, type=Path, help="Path to the project repo")
    p.add_argument("--description", required=True, help="Free-text task description")
    p.add_argument(
        "--coder",
        default=DEFAULT_CODER_TOOL,
        choices=["claude"],  # only claude until T5/T6
        help="Coding agent tool",
    )
    p.add_argument(
        "--reviewer",
        default=DEFAULT_REVIEWER_NAME,
        help="Reviewer name (its persona/focus); single reviewer until T6",
    )
    p.add_argument(
        "--block-threshold",
        default=DEFAULT_BLOCK_THRESHOLD,
        choices=["critical", "major", "minor"],
        help="Findings below this severity do not block",
    )
    p.add_argument(
        "--max-rounds",
        type=int,
        default=DEFAULT_MAX_ROUNDS,
        help="Max implement→review→fix rounds before stopping unapproved",
    )
    p.add_argument(
        "--no-test-gate",
        action="store_true",
        help="Skip the per-round objective test gate (throwaway container)",
    )
    p.add_argument(
        "--test-command",
        default=None,
        help="Test command for the gate (overrides auto-detection)",
    )
    p.add_argument(
        "--block-on-red",
        action="store_true",
        help="A red test gate prevents approval (default: recorded, non-blocking)",
    )
    p.add_argument(
        "--test-timeout",
        type=int,
        default=DEFAULT_TEST_TIMEOUT,
        help="Max seconds for one test-gate run",
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
        "--coder-timeout", type=int, default=3600, help="Max seconds for the coder turn"
    )
    p.add_argument(
        "--reviewer-timeout",
        type=int,
        default=3600,
        help="Max seconds for a reviewer turn",
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

    if not is_valid_reviewer_name(args.reviewer):
        print(
            f"error: invalid --reviewer {args.reviewer!r}: use lowercase "
            "alphanumerics + hyphens (e.g. 'code-quality')",
            file=sys.stderr,
        )
        return 2

    if args.max_rounds < 1:
        print("error: --max-rounds must be >= 1", file=sys.stderr)
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
        reviewer=args.reviewer,
        block_threshold=args.block_threshold,
        max_rounds=args.max_rounds,
        test_gate=not args.no_test_gate,
        test_command=args.test_command,
        block_on_red=args.block_on_red,
        test_timeout=args.test_timeout,
        image=args.image,
        base_branch=args.branch,
    )

    result = develop(
        config,
        coder_timeout=args.coder_timeout,
        reviewer_timeout=args.reviewer_timeout,
    )

    print()
    print(f"story-develop run {result.run_id}: {result.status.upper()}")
    print(f"  branch:   {result.branch}")
    print(f"  worktree: {result.worktree}")
    print(f"  rounds:   {result.rounds}")
    print(f"  commits:  {len(result.commits)}")
    if result.review is not None:
        r = result.review
        sev = f" (max severity: {r.max_severity})" if r.max_severity else ""
        gate = "passes threshold" if r.passed else "BLOCKS"
        print(f"  review:   [{r.reviewer}] {r.status} — {gate}{sev}")
        print(f"            {r.findings_count} finding(s)")
    if result.test_gate is not None:
        g = result.test_gate
        blocking = " — BLOCKS approval" if (not g.passed and args.block_on_red) else ""
        print(f"  gate:     {g.verdict} (`{g.command}`, exit {g.exit_code}){blocking}")
    if result.conversation_log is not None:
        print(f"  log:      {result.conversation_log}")
    print(f"  cost:     ${result.total_cost_usd:.4f}")
    print(f"  {result.message}")
    if result.status == "max_rounds":
        print(
            "\n  Not approved within max-rounds. Inspect the branch + conversation "
            "log above;\n  re-run with a higher --max-rounds, or attach to the "
            "worktree to intervene."
        )
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
