"""Subprocess + smoke tests for the github-watcher child entry.

Confirms the supervisor can ``python -m`` the child cleanly. Without a
real Lithos and a real ``gh`` login the child can't actually do work,
but the disabled-gate path returns 0 immediately and is testable in
the CI sandbox.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from textwrap import dedent


def _no_watcher_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"
            """
        )
    )
    return cfg


def _disabled_watcher_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            [github_watcher]
            enabled = false
            """
        )
    )
    return cfg


async def test_github_watcher_child_exits_nonzero_without_section(
    tmp_path: Path,
) -> None:
    """Defensive: section missing → child exits non-zero so supervisor sees it."""
    cfg = _no_watcher_config(tmp_path)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lithos_loom.children.github_watcher",
        "--config",
        str(cfg),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
    assert rc == 1


async def test_github_watcher_child_exits_nonzero_when_disabled(
    tmp_path: Path,
) -> None:
    """Same defensive behaviour when the section is present but enabled=false."""
    cfg = _disabled_watcher_config(tmp_path)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lithos_loom.children.github_watcher",
        "--config",
        str(cfg),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
    assert rc == 1


async def test_github_watcher_child_module_is_importable() -> None:
    """The child module must expose ``main`` and be runnable via -m."""
    import lithos_loom.children.github_watcher as mod

    assert callable(mod.main)


async def test_github_watcher_child_wires_both_directions() -> None:
    """Slice 7.2: child imports both the GH→Lithos sync handler and the
    Lithos→GH push handler. A regression here (e.g. circular import or
    rename without updating the child) would crash at module load."""
    import lithos_loom.children.github_watcher as mod

    # Both handler factories must be in scope on the module.
    assert callable(mod.make_github_issue_sync_handler)
    assert callable(mod.make_github_issue_push_handler)
    # And both event-type constants used by the bus subscribe calls.
    assert mod.GITHUB_ISSUE_EVENT_TYPE == "github.issue.seen"
    assert "lithos.task.completed" in mod.LITHOS_TASK_EVENT_TYPES
    assert "lithos.task.cancelled" in mod.LITHOS_TASK_EVENT_TYPES
    assert "lithos.task.updated" in mod.LITHOS_TASK_EVENT_TYPES


async def test_reconcile_pass_redispatches_gh_linked_tasks() -> None:
    """PR-review finding 4 (round 5, 2026-05-30): the periodic
    reconciliation pass scans Lithos for tasks carrying
    metadata.github_issue_url and re-dispatches them through the push
    handler. GH-unlinked tasks must be skipped — they're noise.

    Round 6 update: terminal tasks now also fire ``task.updated`` so a
    rename dropped during a long outage gets reconciled alongside the
    close event."""
    import logging
    from datetime import UTC, datetime, timedelta
    from typing import Any
    from unittest.mock import AsyncMock

    from lithos_loom.children.github_watcher import _run_reconcile_pass
    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext

    open_linked = Task(
        id="task-a",
        title="Linked",
        status="open",
        tags=("github-issue",),
        metadata={"github_issue_url": "https://github.com/x/y/issues/1"},
        claims=(),
    )
    open_unlinked = Task(
        id="task-b",
        title="No GH link",
        status="open",
        tags=(),
        metadata={"project": "other"},
        claims=(),
    )
    completed_linked = Task(
        id="task-c",
        title="Done linked",
        status="completed",
        tags=("github-issue",),
        metadata={"github_issue_url": "https://github.com/x/y/issues/2"},
        claims=(),
        resolved_at=datetime(2026, 5, 29, tzinfo=UTC),
    )
    lithos = AsyncMock()
    lithos.task_list = AsyncMock(
        side_effect=[
            [open_linked, open_unlinked],  # open
            [completed_linked],  # completed
            [],  # cancelled
        ]
    )
    handler_calls: list[str] = []

    async def push_handler(event: Any, _ctx: Any) -> None:
        handler_calls.append(event.type)

    ctx = SubscriptionContext(
        lithos=lithos,
        logger=logging.getLogger("test-reconcile"),
        agent_id="test-agent",
    )
    await _run_reconcile_pass(
        lithos=lithos,
        push_handler=push_handler,
        ctx=ctx,
        resolved_window=timedelta(days=7),
        github=AsyncMock(),
        pr_merge_enabled=False,
    )
    # Open task → one updated event (title sync).
    # Terminal task → updated + close so title drift is reconciled too.
    # GH-unlinked task was filtered out.
    assert handler_calls == [
        "lithos.task.updated",  # open_linked
        "lithos.task.updated",  # completed_linked title
        "lithos.task.completed",  # completed_linked close
    ]


async def test_reconcile_pass_skips_terminal_scan_when_window_disabled() -> None:
    """PR-review finding 1 (round 6, 2026-05-30): resolved_replay_days=0
    means "operator opted out of resolved replay". The sweep used to
    treat that as resolved_since=None and walk *every* terminal task
    ever, which grows unboundedly. The terminal scans must skip
    entirely while the open-task title sweep still runs.
    """
    import logging
    from typing import Any
    from unittest.mock import AsyncMock

    from lithos_loom.children.github_watcher import _run_reconcile_pass
    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext

    open_linked = Task(
        id="task-a",
        title="Linked",
        status="open",
        tags=("github-issue",),
        metadata={"github_issue_url": "https://github.com/x/y/issues/1"},
        claims=(),
    )
    lithos = AsyncMock()
    lithos.task_list = AsyncMock(return_value=[open_linked])
    handler_calls: list[str] = []

    async def push_handler(event: Any, _ctx: Any) -> None:
        handler_calls.append(event.type)

    ctx = SubscriptionContext(
        lithos=lithos,
        logger=logging.getLogger("test-reconcile"),
        agent_id="test-agent",
    )
    await _run_reconcile_pass(
        lithos=lithos,
        push_handler=push_handler,
        ctx=ctx,
        resolved_window=None,
        github=AsyncMock(),
        pr_merge_enabled=False,
    )
    # Only the open-task scan ran; no completed / cancelled queries.
    assert lithos.task_list.await_count == 1
    assert lithos.task_list.await_args.kwargs["status"] == "open"
    # And only the open task's title sync fired.
    assert handler_calls == ["lithos.task.updated"]


async def test_reconcile_pass_polls_develop_pr_tasks_when_enabled() -> None:
    """#87: the sweep checks open non-issue tasks carrying develop_pr_url and
    completes them when the PR has merged."""
    import logging
    from unittest.mock import AsyncMock

    from lithos_loom.children.github_watcher import _run_reconcile_pass
    from lithos_loom.github_client import PullRequest
    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext

    develop_task = Task(
        id="task-pr",
        title="PR task",
        status="open",
        tags=("trigger:story-develop",),
        metadata={"develop_pr_url": "https://github.com/o/r/pull/9"},
        claims=(),
    )
    lithos = AsyncMock()
    lithos.task_list = AsyncMock(return_value=[develop_task])
    github = AsyncMock()
    github.get_pull_request = AsyncMock(
        return_value=PullRequest(
            repo="o/r",
            number=9,
            state="closed",
            merged=True,
            merged_at=None,
            merge_commit_sha="sha9",
        )
    )
    ctx = SubscriptionContext(
        lithos=lithos, logger=logging.getLogger("test-pr"), agent_id="a"
    )
    await _run_reconcile_pass(
        lithos=lithos,
        push_handler=AsyncMock(),
        ctx=ctx,
        resolved_window=None,
        github=github,
        pr_merge_enabled=True,
    )
    github.get_pull_request.assert_awaited_once_with("o/r", 9)
    lithos.task_complete.assert_awaited_once_with(task_id="task-pr")


async def test_reconcile_pass_skips_pr_poll_when_disabled() -> None:
    """pr_merge_poll_enabled=false runs the watcher for issue sync only."""
    import logging
    from unittest.mock import AsyncMock

    from lithos_loom.children.github_watcher import _run_reconcile_pass
    from lithos_loom.lithos_client import Task
    from lithos_loom.subscriptions import SubscriptionContext

    develop_task = Task(
        id="task-pr",
        title="PR task",
        status="open",
        tags=(),
        metadata={"develop_pr_url": "https://github.com/o/r/pull/9"},
        claims=(),
    )
    lithos = AsyncMock()
    lithos.task_list = AsyncMock(return_value=[develop_task])
    github = AsyncMock()
    ctx = SubscriptionContext(
        lithos=lithos, logger=logging.getLogger("test-pr"), agent_id="a"
    )
    await _run_reconcile_pass(
        lithos=lithos,
        push_handler=AsyncMock(),
        ctx=ctx,
        resolved_window=None,
        github=github,
        pr_merge_enabled=False,
    )
    github.get_pull_request.assert_not_awaited()


def test_configure_logging_silences_mcp_sse_at_critical() -> None:
    """At any level, the MCP SDK's per-reconnect tracebacks are pinned to CRITICAL.

    Same noise suppression as obsidian-sync — without this, every Lithos
    restart shows an SDK traceback that buries our own reconnect timeline.
    """
    import logging

    from lithos_loom.children.github_watcher import _configure_logging

    logging.getLogger("mcp.client.sse").setLevel(logging.NOTSET)
    _configure_logging("info")
    assert logging.getLogger("mcp.client.sse").level == logging.CRITICAL
