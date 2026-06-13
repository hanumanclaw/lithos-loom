"""Tests for ``lithos_loom.subscriptions._develop_pr_merge`` (#87).

The reconcile polls a delivered PR's merge state and acts on the open Lithos
task: merged → complete; closed-unmerged / deleted → one-shot
``[DeliveredPRClosed]`` finding + leave open; still-open → no-op. A
``develop_pr_merge_state`` + ``develop_pr_merge_url`` marker scoped to the
resolved PR de-dups across sweeps while letting a replacement PR recover.
GitHub + Lithos are stubbed; the ``task`` is a minimal id + metadata object.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.errors import LithosClientError
from lithos_loom.github_client import GitHubError, PullRequest
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._develop_pr_merge import (
    DELIVERED_PR_CLOSED,
    MERGE_STATE_KEY,
    MERGE_STATE_URL_KEY,
    _parse_pr_url,
    reconcile_develop_pr,
)

_PR_URL = "https://github.com/agent-lore/lithos-loom/pull/7"
_PR_URL_2 = "https://github.com/agent-lore/lithos-loom/pull/8"  # a replacement PR


def _ctx(lithos: Any) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos,
        logger=logging.getLogger("test-develop-pr-merge"),
        agent_id="lithos-loom-agent",
    )


def _task(metadata: dict[str, Any], *, task_id: str = "t-1") -> Any:
    return SimpleNamespace(id=task_id, metadata=metadata)


def _pr(*, state: str, merged: bool, sha: str | None = "abc123") -> PullRequest:
    return PullRequest(
        repo="agent-lore/lithos-loom",
        number=7,
        state=state,
        merged=merged,
        merged_at=datetime(2026, 6, 13, tzinfo=UTC) if merged else None,
        merge_commit_sha=sha if merged else None,
    )


# ── _parse_pr_url ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (_PR_URL, ("agent-lore/lithos-loom", 7)),
        ("https://github.com/o/r/pull/123", ("o/r", 123)),
        ("https://github.com/o/r/issues/5", (None, None)),  # issue, not pull
        ("https://github.com/o/r/pull/notanum", (None, None)),
        ("https://example.com/o/r/pull/1", (None, None)),
        ("not a url", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_pr_url(url: object, expected: tuple[str | None, int | None]) -> None:
    assert _parse_pr_url(url) == expected


# ── reconcile_develop_pr ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merged_pr_completes_task_and_marks() -> None:
    github = AsyncMock()
    github.get_pull_request.return_value = _pr(state="closed", merged=True)
    lithos = AsyncMock()
    task = _task({"develop_pr_url": _PR_URL})

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome == "merged"
    lithos.task_complete.assert_awaited_once_with(task_id="t-1")
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1",
        metadata={MERGE_STATE_KEY: "merged", MERGE_STATE_URL_KEY: _PR_URL},
    )
    lithos.finding_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_unmerged_leaves_open_with_finding() -> None:
    github = AsyncMock()
    github.get_pull_request.return_value = _pr(state="closed", merged=False)
    lithos = AsyncMock()
    task = _task({"develop_pr_url": _PR_URL})

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome == "closed_unmerged"
    lithos.task_complete.assert_not_awaited()
    summary = lithos.finding_post.await_args.kwargs["summary"]
    assert summary.startswith(DELIVERED_PR_CLOSED)
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1",
        metadata={MERGE_STATE_KEY: "closed_unmerged", MERGE_STATE_URL_KEY: _PR_URL},
    )


@pytest.mark.asyncio
async def test_open_pr_is_noop() -> None:
    github = AsyncMock()
    github.get_pull_request.return_value = _pr(state="open", merged=False)
    lithos = AsyncMock()

    outcome = await reconcile_develop_pr(
        _task({"develop_pr_url": _PR_URL}), github, _ctx(lithos)
    )

    assert outcome == "still_open"
    lithos.task_complete.assert_not_awaited()
    lithos.finding_post.assert_not_awaited()
    lithos.task_update.assert_not_awaited()  # no marker → re-poll next sweep


@pytest.mark.asyncio
async def test_deleted_pr_marks_gone_with_finding() -> None:
    github = AsyncMock()
    github.get_pull_request.return_value = None  # 404
    lithos = AsyncMock()

    outcome = await reconcile_develop_pr(
        _task({"develop_pr_url": _PR_URL}), github, _ctx(lithos)
    )

    assert outcome == "gone"
    lithos.task_complete.assert_not_awaited()
    assert DELIVERED_PR_CLOSED in lithos.finding_post.await_args.kwargs["summary"]
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1", metadata={MERGE_STATE_KEY: "gone", MERGE_STATE_URL_KEY: _PR_URL}
    )


@pytest.mark.asyncio
async def test_terminal_marker_for_same_url_skips_without_github_call() -> None:
    """De-dup: a marker terminal for the CURRENT develop_pr_url is a skip."""
    github = AsyncMock()
    lithos = AsyncMock()
    task = _task(
        {
            "develop_pr_url": _PR_URL,
            MERGE_STATE_KEY: "merged",
            MERGE_STATE_URL_KEY: _PR_URL,  # same url it resolved
        }
    )

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome is None
    github.get_pull_request.assert_not_awaited()  # de-dup: no re-poll
    lithos.task_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_replacement_pr_recovers_after_abandoned_one() -> None:
    """The natural recovery path: an old PR was abandoned (marker
    closed_unmerged for the OLD url), the task was re-developed into a NEW PR
    (develop_pr_url changed). The stale marker must NOT suppress the new PR —
    it's re-evaluated and, on merge, completes."""
    github = AsyncMock()
    github.get_pull_request.return_value = _pr(state="closed", merged=True)
    lithos = AsyncMock()
    task = _task(
        {
            "develop_pr_url": _PR_URL_2,  # the replacement PR
            MERGE_STATE_KEY: "closed_unmerged",
            MERGE_STATE_URL_KEY: _PR_URL,  # stale: points at the abandoned PR
        }
    )

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome == "merged"
    github.get_pull_request.assert_awaited_once_with("agent-lore/lithos-loom", 8)
    lithos.task_complete.assert_awaited_once_with(task_id="t-1")
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1",
        metadata={MERGE_STATE_KEY: "merged", MERGE_STATE_URL_KEY: _PR_URL_2},
    )


@pytest.mark.asyncio
async def test_replacement_pr_also_closed_reposts_for_new_url() -> None:
    """A replacement PR that's also closed-unmerged re-posts the finding (it's a
    different url than the recorded marker) and re-marks for the new url."""
    github = AsyncMock()
    github.get_pull_request.return_value = _pr(state="closed", merged=False)
    lithos = AsyncMock()
    task = _task(
        {
            "develop_pr_url": _PR_URL_2,
            MERGE_STATE_KEY: "closed_unmerged",
            MERGE_STATE_URL_KEY: _PR_URL,
        }
    )

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome == "closed_unmerged"
    assert DELIVERED_PR_CLOSED in lithos.finding_post.await_args.kwargs["summary"]
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1",
        metadata={MERGE_STATE_KEY: "closed_unmerged", MERGE_STATE_URL_KEY: _PR_URL_2},
    )


@pytest.mark.asyncio
async def test_issue_linked_task_is_skipped() -> None:
    """Issue-linked tasks close via the existing issue mirror — don't double-handle."""
    github = AsyncMock()
    lithos = AsyncMock()
    task = _task(
        {
            "develop_pr_url": _PR_URL,
            "github_issue_url": "https://github.com/o/r/issues/3",
        }
    )

    assert await reconcile_develop_pr(task, github, _ctx(lithos)) is None
    github.get_pull_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_develop_pr_url_is_skipped() -> None:
    github = AsyncMock()
    assert await reconcile_develop_pr(_task({}), github, _ctx(AsyncMock())) is None
    github.get_pull_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_swallows_task_not_found_and_still_marks() -> None:
    """An already-terminal task (operator completed it first) is a no-op, not crash."""
    github = AsyncMock()
    github.get_pull_request.return_value = _pr(state="closed", merged=True)
    lithos = AsyncMock()
    lithos.task_complete.side_effect = LithosClientError("task_not_found", "gone")
    task = _task({"develop_pr_url": _PR_URL})

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome == "merged"  # no exception escaped
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1",
        metadata={MERGE_STATE_KEY: "merged", MERGE_STATE_URL_KEY: _PR_URL},
    )


@pytest.mark.asyncio
async def test_malformed_url_frictions_and_marks_unparseable() -> None:
    github = AsyncMock()
    lithos = AsyncMock()
    task = _task({"develop_pr_url": "https://github.com/o/r/pull/notanum"})

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))

    assert outcome == "unparseable"
    github.get_pull_request.assert_not_awaited()
    assert "[Friction]" in lithos.finding_post.await_args.kwargs["summary"]
    lithos.task_update.assert_awaited_once_with(
        task_id="t-1",
        metadata={
            MERGE_STATE_KEY: "unparseable",
            MERGE_STATE_URL_KEY: "https://github.com/o/r/pull/notanum",
        },
    )


@pytest.mark.asyncio
async def test_transient_github_error_leaves_marker_unset() -> None:
    """A GH 5xx/auth blip must not mark the task terminal — retry next sweep."""
    github = AsyncMock()
    github.get_pull_request.side_effect = GitHubError("503 server error")
    lithos = AsyncMock()
    task = _task({"develop_pr_url": _PR_URL})

    outcome = await reconcile_develop_pr(task, github, _ctx(lithos))  # no raise

    assert outcome == "error"
    lithos.task_complete.assert_not_awaited()
    lithos.task_update.assert_not_awaited()  # marker unset → retried next sweep
    lithos.finding_post.assert_not_awaited()
