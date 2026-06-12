"""story-develop entry point (standalone).

Standalone mode::

    python -m lithos_loom.plugins.story_develop \\
        --repo ~/projects/foo --description "Add a CLI flag"

    # or develop a Lithos task directly (full round-trip, T8):
    python -m lithos_loom.plugins.story_develop \\
        --repo ~/projects/foo --task-id <uuid>

Runs the implement → review → fix loop: a coder implements the task, a reviewer
reviews it, and the coder fixes and the reviewer re-reviews each round until the
reviewer approves (LGTM or below the block threshold) or ``--max-rounds`` is
hit. Leaves a branch with per-round commits and a conversation log. With
``--task-id`` the task (title, body, acceptance criteria) is fetched from
Lithos up front and the outcome (verdicts, open findings, branch, cost) is
posted back when the run ends. Daemon mode
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
    DEFAULT_MAX_PAUSE_MINUTES,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_PAUSE_POLL_MINUTES,
    DEFAULT_REVIEWER_NAME,
    DEFAULT_TEST_TIMEOUT,
    DevelopConfig,
    ReviewerSpec,
    is_valid_reviewer_name,
    load_develop_config,
)
from .develop import develop
from .lithos_io import (
    DEFAULT_LITHOS_URL,
    LithosIOError,
    complete_task,
    fetch_task_context,
    post_results,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m lithos_loom.plugins.story_develop",
        description="Run the story-develop loop (coder implements; reviewer reviews; "
        "iterate to approval or --max-rounds).",
    )
    p.add_argument("--repo", required=True, type=Path, help="Path to the project repo")
    p.add_argument(
        "--description",
        default=None,
        help="Free-text task description (optional with --task-id: the task's "
        "title + body are used)",
    )
    p.add_argument(
        "--task-id",
        default=None,
        help="Lithos task to develop: fetches title/body/acceptance criteria "
        "up front and posts the outcome back when the run ends. The task IS "
        "the description (incompatible with --description — the audit trail "
        "must not lie about what was developed)",
    )
    p.add_argument(
        "--complete-on-approval",
        action="store_true",
        help="With --task-id: mark the Lithos task completed when the run is "
        "approved. Default OFF — agent approval means a reviewed branch "
        "exists, not that the work is merged",
    )
    p.add_argument(
        "--lithos-url",
        default=DEFAULT_LITHOS_URL,
        help="Lithos MCP base URL (used with --task-id)",
    )
    p.add_argument(
        "--no-lithos",
        action="store_true",
        help="Pure-offline run: never touch Lithos (incompatible with --task-id)",
    )
    p.add_argument(
        "--acceptance-criteria",
        default=None,
        metavar="TEXT|@FILE",
        help="Definition of done shown to the coder AND every reviewer; "
        "@path reads a file. Default: task metadata (with --task-id), "
        "else the description",
    )
    p.add_argument(
        "--coder",
        default=DEFAULT_CODER_TOOL,
        choices=["claude"],  # only claude until T5/T6
        help="Coding agent tool",
    )
    p.add_argument(
        "--reviewer",
        action="append",
        default=None,
        help="Reviewer name (repeatable; each gets its own RO container). "
        f"Default: one '{DEFAULT_REVIEWER_NAME}' reviewer",
    )
    p.add_argument(
        "--develop-config",
        type=Path,
        default=None,
        help="TOML file of full reviewer specs ([[reviewers]] tables with "
        "name/tool/block_threshold/system_prompt/fallback_chain); "
        "mutually exclusive with --reviewer",
    )
    p.add_argument(
        "--block-threshold",
        default=DEFAULT_BLOCK_THRESHOLD,
        choices=["critical", "major", "minor"],
        help="Findings below this severity do not block (applies to all "
        "--reviewer names; per-reviewer thresholds need --develop-config)",
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
    p.add_argument(
        "--max-cost-usd",
        type=float,
        default=None,
        help="Stop the run when total agent spend reaches this (default: none)",
    )
    p.add_argument(
        "--max-pause-minutes",
        type=int,
        default=DEFAULT_MAX_PAUSE_MINUTES,
        help="Total usage-limit pause budget for the run (then: interrupted)",
    )
    p.add_argument(
        "--pause-poll-minutes",
        type=int,
        default=DEFAULT_PAUSE_POLL_MINUTES,
        help="Retry cadence while usage-limited with no known reset time",
    )
    p.add_argument(
        "--reviewer-fallback",
        action="append",
        default=None,
        metavar="TOOL",
        help="Alternate reviewer tool tried when the current one is "
        "usage-limited (repeatable; tried in order)",
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

    # --- resolve the task source (T8) -------------------------------------
    if args.task_id is not None and args.no_lithos:
        print("error: --task-id and --no-lithos are incompatible", file=sys.stderr)
        return 2
    if args.task_id is not None and args.description is not None:
        # The task IS the description: a run that reports "developing task X"
        # while prompting the coder with unrelated operator text would be a
        # lying audit trail. Refine the definition of done via
        # --acceptance-criteria, or fix the task body itself.
        print(
            "error: --task-id and --description are incompatible (the task's "
            "title + body are the description; use --acceptance-criteria to "
            "refine the definition of done)",
            file=sys.stderr,
        )
        return 2
    if args.complete_on_approval and args.task_id is None:
        print("error: --complete-on-approval requires --task-id", file=sys.stderr)
        return 2
    if args.task_id is None and args.description is None:
        print("error: one of --description or --task-id is required", file=sys.stderr)
        return 2
    if args.description is not None and not args.description.strip():
        print("error: --description must not be empty", file=sys.stderr)
        return 2

    acceptance_criteria = args.acceptance_criteria
    if acceptance_criteria is not None and acceptance_criteria.startswith("@"):
        ac_path = Path(acceptance_criteria[1:]).expanduser()
        try:
            acceptance_criteria = ac_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(
                f"error: cannot read --acceptance-criteria file: {exc}",
                file=sys.stderr,
            )
            return 2
    if acceptance_criteria is not None and not acceptance_criteria.strip():
        print("error: --acceptance-criteria must not be empty", file=sys.stderr)
        return 2

    description = args.description
    if args.task_id is not None:
        try:
            ctx = fetch_task_context(args.lithos_url, args.task_id)
        except LithosIOError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        description = ctx.task_text
        # explicit flag > task metadata > (effective fallback to description)
        acceptance_criteria = acceptance_criteria or ctx.acceptance_criteria
        print(f"developing Lithos task {ctx.task_id}: {ctx.title}")
    assert description is not None  # guaranteed by the validation above

    # --- resolve the reviewer panel (T6) ---------------------------------
    if args.develop_config is not None and args.reviewer:
        print(
            "error: --develop-config and --reviewer are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    if args.develop_config is not None:
        try:
            specs = load_develop_config(args.develop_config.expanduser())
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        names = args.reviewer or [DEFAULT_REVIEWER_NAME]
        if len(set(names)) != len(names):
            print(f"error: duplicate --reviewer names: {names}", file=sys.stderr)
            return 2
        for name in names:
            if not is_valid_reviewer_name(name):
                print(
                    f"error: invalid --reviewer {name!r}: use lowercase "
                    "alphanumerics + hyphens (e.g. 'code-quality')",
                    file=sys.stderr,
                )
                return 2
        specs = tuple(
            ReviewerSpec(
                name=name,
                block_threshold=args.block_threshold,
                fallback_chain=tuple(args.reviewer_fallback or ()),
            )
            for name in names
        )

    if args.max_rounds < 1:
        print("error: --max-rounds must be >= 1", file=sys.stderr)
        return 2

    if args.pause_poll_minutes < 1:
        print("error: --pause-poll-minutes must be >= 1", file=sys.stderr)
        return 2

    if args.max_pause_minutes < 0:
        print("error: --max-pause-minutes must be >= 0", file=sys.stderr)
        return 2

    if args.max_cost_usd is not None and args.max_cost_usd <= 0:
        print("error: --max-cost-usd must be > 0", file=sys.stderr)
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
        description=description,
        work_dir=work_dir,
        acceptance_criteria=acceptance_criteria,
        coder=args.coder,
        reviewers=specs,
        max_rounds=args.max_rounds,
        test_gate=not args.no_test_gate,
        test_command=args.test_command,
        block_on_red=args.block_on_red,
        test_timeout=args.test_timeout,
        max_pause_minutes=args.max_pause_minutes,
        pause_poll_minutes=args.pause_poll_minutes,
        reviewer_fallback_chain=tuple(args.reviewer_fallback or ()),
        max_cost_usd=args.max_cost_usd,
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
    for r in result.reviews:
        sev = f" (max severity: {r.max_severity})" if r.max_severity else ""
        gate = "passes threshold" if r.passed else "BLOCKS"
        print(
            f"  review:   [{r.reviewer}] {r.status} — {gate}{sev}; "
            f"{r.findings_count} finding(s)"
        )
    if result.test_gate is not None:
        g = result.test_gate
        blocking = " — BLOCKS approval" if (not g.passed and args.block_on_red) else ""
        print(f"  gate:     {g.verdict} (`{g.command}`, exit {g.exit_code}){blocking}")
    if result.conversation_log is not None:
        print(f"  log:      {result.conversation_log}")
    print(f"  cost:     ${result.total_cost_usd:.4f}")
    if args.task_id is not None:
        posted = post_results(args.lithos_url, args.task_id, result)
        print(
            f"  lithos:   {'results posted to' if posted else 'POSTING FAILED for'} "
            f"task {args.task_id}"
        )
        if args.complete_on_approval and result.approved:
            done = complete_task(args.lithos_url, args.task_id, result)
            print(
                f"  lithos:   task {args.task_id} "
                f"{'marked completed' if done else 'COMPLETION FAILED'}"
            )
    print(f"  {result.message}")
    if result.status == "max_rounds":
        print(
            "\n  Not approved within max-rounds. Inspect the branch + conversation "
            "log above;\n  re-run with a higher --max-rounds, or attach to the "
            "worktree to intervene."
        )
    elif result.status == "disputed":
        print(
            "\n  Dispute deadlock: the coder formally disputes finding(s) the "
            "reviewer keeps blocking.\n  Read the conversation log and decide — "
            "this needs a human."
        )
    elif result.status == "stalled":
        print(
            "\n  Stalled: no progress across consecutive rounds (no new commit "
            "and/or unchanged\n  blocking findings). Inspect the conversation log "
            "before re-running."
        )
    elif result.status == "cost_exceeded":
        print("\n  Stopped at the --max-cost-usd ceiling.")
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
