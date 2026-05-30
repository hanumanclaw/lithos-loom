"""Tests for ``lithos_loom.subscriptions._github_issue_sync``.

Handler-level tests with stubbed Lithos + GitHub clients. The handler is
event-driven; we exercise it by constructing ``github.issue.seen`` events
and asserting which Lithos / GitHub calls land.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event
from lithos_loom.errors import LithosClientError
from lithos_loom.github_client import GitHubAuthError
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._github_issue_sync import (
    EVENT_TYPE,
    GITHUB_ISSUE_TAG,
    make_handler,
)

# ── Builders ──────────────────────────────────────────────────────────


def _event(
    *,
    repo: str = "agent-lore/lithos-loom",
    number: int = 42,
    title: str = "Test issue",
    body: str = "issue body",
    state: str = "open",
    state_reason: str | None = None,
    labels: list[str] | None = None,
    slug: str = "lithos-loom",
    html_url: str = "https://github.com/agent-lore/lithos-loom/issues/42",
    author: str = "alice",
    exclude_labels: list[str] | None = None,
    exclude_authors: list[str] | None = None,
) -> Event:
    return Event(
        type=EVENT_TYPE,
        timestamp=datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        payload={
            "slug": slug,
            "repo": repo,
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "state_reason": state_reason,
            "labels": labels or ["bug"],
            "author": author,
            "html_url": html_url,
            "updated_at": "2026-05-29T12:00:00+00:00",
            "exclude_labels": exclude_labels or [],
            "exclude_authors": exclude_authors or [],
        },
    )


def _task(
    *,
    task_id: str = "task-123",
    status: str = "open",
    url: str = "https://github.com/agent-lore/lithos-loom/issues/42",
    title: str = "Test issue",
    description: str | None = "issue body",
    tags: tuple[str, ...] = ("bug", GITHUB_ISSUE_TAG),
    metadata: dict[str, Any] | None = None,
) -> Task:
    if metadata is None:
        metadata = {"github_issue_url": url, "project": "lithos-loom"}
    return Task(
        id=task_id,
        title=title,
        status=status,
        tags=tags,
        metadata=metadata,
        claims=(),
        description=description,
    )


def _ctx(lithos: Any) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos,
        logger=logging.getLogger("test-github-issue-sync"),
        agent_id="lithos-loom-agent",
    )


def _stub_lithos() -> AsyncMock:
    client = AsyncMock()
    client.task_create = AsyncMock(return_value="task-new-1")
    client.task_get = AsyncMock(return_value=None)
    client.task_list = AsyncMock(return_value=[])
    client.task_complete = AsyncMock()
    client.task_cancel = AsyncMock()
    client.task_update = AsyncMock()
    client.finding_post = AsyncMock(return_value="finding-1")
    return client


def _stub_github() -> AsyncMock:
    gh = AsyncMock()
    gh.update_issue_body = AsyncMock()
    # Default: no fresh body available → marker writer falls back to the
    # poll-event body. Tests that exercise the race-narrowing fetch
    # override this with their own get_issue return value.
    gh.get_issue = AsyncMock(return_value=None)
    return gh


# ── New issue → create task + write marker ────────────────────────────


@pytest.mark.asyncio
async def test_new_open_issue_creates_task_and_writes_marker() -> None:
    lithos = _stub_lithos()
    github = _stub_github()
    handler = make_handler(github)
    await handler(_event(), _ctx(lithos))

    lithos.task_create.assert_awaited_once()
    create_kwargs = lithos.task_create.await_args.kwargs
    assert create_kwargs["title"] == "Test issue"
    assert create_kwargs["description"] == "issue body"
    assert create_kwargs["metadata"]["github_issue_url"] == (
        "https://github.com/agent-lore/lithos-loom/issues/42"
    )
    assert create_kwargs["metadata"]["github_issue_number"] == 42
    assert create_kwargs["metadata"]["project"] == "lithos-loom"
    assert create_kwargs["metadata"]["github_labels"] == ["bug"]
    assert GITHUB_ISSUE_TAG in create_kwargs["tags"]
    assert "bug" in create_kwargs["tags"]

    github.update_issue_body.assert_awaited_once()
    update_args = github.update_issue_body.await_args
    assert update_args.args[0] == "agent-lore/lithos-loom"
    assert update_args.args[1] == 42
    assert "<!-- lithos:task-new-1 -->" in update_args.args[2]


@pytest.mark.asyncio
async def test_new_closed_issue_skipped() -> None:
    """We don't backfill closures that have no Lithos task representation."""
    lithos = _stub_lithos()
    github = _stub_github()
    handler = make_handler(github)
    await handler(_event(state="closed", state_reason="completed"), _ctx(lithos))
    lithos.task_create.assert_not_awaited()
    github.update_issue_body.assert_not_awaited()


# ── Existing task via marker ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_marker_open_issue_is_noop() -> None:
    """An already-linked open issue produces no Lithos or GitHub writes."""
    lithos = _stub_lithos()
    existing = _task(task_id="task-123", status="open")
    lithos.task_get = AsyncMock(return_value=existing)
    github = _stub_github()
    handler = make_handler(github)
    await handler(_event(body="some text\n\n<!-- lithos:task-123 -->"), _ctx(lithos))
    lithos.task_create.assert_not_awaited()
    lithos.task_complete.assert_not_awaited()
    lithos.task_cancel.assert_not_awaited()
    github.update_issue_body.assert_not_awaited()


@pytest.mark.asyncio
async def test_marker_gh_closed_completed_completes_task() -> None:
    lithos = _stub_lithos()
    existing = _task(task_id="task-123", status="open")
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())

    await handler(
        _event(
            body="text\n<!-- lithos:task-123 -->",
            state="closed",
            state_reason="completed",
        ),
        _ctx(lithos),
    )
    lithos.task_complete.assert_awaited_once_with(
        task_id="task-123", agent="lithos-loom-agent"
    )
    lithos.task_cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_marker_gh_closed_not_planned_cancels_task() -> None:
    lithos = _stub_lithos()
    existing = _task(task_id="task-123", status="open")
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())

    await handler(
        _event(
            body="<!-- lithos:task-123 -->",
            state="closed",
            state_reason="not_planned",
        ),
        _ctx(lithos),
    )
    lithos.task_cancel.assert_awaited_once()
    cancel_kwargs = lithos.task_cancel.await_args.kwargs
    assert cancel_kwargs["task_id"] == "task-123"
    assert "not_planned" in cancel_kwargs["reason"]


@pytest.mark.asyncio
async def test_marker_gh_closed_already_terminal_in_lithos_is_noop() -> None:
    """Steady-state idempotency: closed on GH, already cancelled in Lithos."""
    lithos = _stub_lithos()
    existing = _task(task_id="task-123", status="cancelled")
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())

    await handler(
        _event(
            body="<!-- lithos:task-123 -->",
            state="closed",
            state_reason="not_planned",
        ),
        _ctx(lithos),
    )
    lithos.task_complete.assert_not_awaited()
    lithos.task_cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_marker_gh_closed_without_state_reason_logs_skip() -> None:
    """GH supports closing without a reason; we leave the task open."""
    lithos = _stub_lithos()
    existing = _task(task_id="task-123", status="open")
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())

    await handler(
        _event(
            body="<!-- lithos:task-123 -->",
            state="closed",
            state_reason=None,
        ),
        _ctx(lithos),
    )
    lithos.task_complete.assert_not_awaited()
    lithos.task_cancel.assert_not_awaited()


# ── Marker missing but task exists (operator-deleted marker) ──────────


@pytest.mark.asyncio
async def test_orphan_marker_recovery_rewrites_not_duplicates() -> None:
    """No marker on issue + Lithos task carries the URL → re-write marker, no dup."""
    url = "https://github.com/agent-lore/lithos-loom/issues/42"
    lithos = _stub_lithos()
    existing = _task(task_id="task-zombie", status="open", url=url)
    # marker-less issue → task_get isn't called via marker; URL scan finds the task.
    lithos.task_list = AsyncMock(return_value=[existing])
    github = _stub_github()
    handler = make_handler(github)

    await handler(_event(body="no marker here", html_url=url), _ctx(lithos))

    # Re-wrote marker pointing at the existing task.
    github.update_issue_body.assert_awaited_once()
    body = github.update_issue_body.await_args.args[2]
    assert "<!-- lithos:task-zombie -->" in body
    # Did NOT create a duplicate task.
    lithos.task_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_orphan_marker_recovery_then_reconciles_close() -> None:
    """If the marker-less issue is now closed on GH, mirror the close."""
    url = "https://github.com/x/y/issues/1"
    lithos = _stub_lithos()
    existing = _task(task_id="task-abc", status="open", url=url)
    lithos.task_list = AsyncMock(return_value=[existing])
    handler = make_handler(_stub_github())

    await handler(
        _event(
            body="(no marker)",
            state="closed",
            state_reason="completed",
            html_url=url,
        ),
        _ctx(lithos),
    )
    lithos.task_complete.assert_awaited_once_with(
        task_id="task-abc", agent="lithos-loom-agent"
    )


# ── Marker points at deleted task ─────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_marker_creates_fresh_task() -> None:
    """Operator force-deleted the Lithos task. Marker still on GH → create new."""
    lithos = _stub_lithos()
    # task_get(task_id="task-deleted") raises task_not_found.
    lithos.task_get = AsyncMock(
        side_effect=LithosClientError("task_not_found", "deleted")
    )
    lithos.task_create = AsyncMock(return_value="task-fresh")
    # No URL match either (the deleted task is gone).
    lithos.task_list = AsyncMock(return_value=[])
    github = _stub_github()
    handler = make_handler(github)

    await handler(_event(body="text\n<!-- lithos:task-deleted -->"), _ctx(lithos))
    lithos.task_create.assert_awaited_once()
    # New marker written.
    github.update_issue_body.assert_awaited_once()
    new_body = github.update_issue_body.await_args.args[2]
    assert "<!-- lithos:task-fresh -->" in new_body
    assert "task-deleted" not in new_body


# ── Robustness ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unexpected_event_type_is_skipped() -> None:
    lithos = _stub_lithos()
    handler = make_handler(_stub_github())
    await handler(
        Event(
            type="some.other.event",
            timestamp=datetime(2026, 5, 29, tzinfo=UTC),
            payload={},
        ),
        _ctx(lithos),
    )
    lithos.task_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_payload_is_logged_not_raised() -> None:
    """A malformed payload should drop, not crash the runner."""
    lithos = _stub_lithos()
    handler = make_handler(_stub_github())
    await handler(
        Event(
            type=EVENT_TYPE,
            timestamp=datetime(2026, 5, 29, tzinfo=UTC),
            payload={"only": "this-key"},
        ),
        _ctx(lithos),
    )
    lithos.task_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_marker_write_failure_after_create_propagates() -> None:
    """PR-review finding 1 (round 3, 2026-05-30): a marker write failure
    after task_create succeeds now propagates so the watcher's
    dispatcher freezes the cursor; the next poll's URL-match recovery
    re-writes the marker. Swallowing it advanced the cursor and left
    the issue permanently unmarked.
    """
    lithos = _stub_lithos()
    lithos.task_create = AsyncMock(return_value="task-new")
    github = _stub_github()
    github.update_issue_body.side_effect = GitHubAuthError("403 denied")
    handler = make_handler(github)

    with pytest.raises(GitHubAuthError):
        await handler(_event(), _ctx(lithos))


@pytest.mark.asyncio
async def test_task_create_failure_propagates_to_dispatcher() -> None:
    """PR-review finding 1 (round 3, 2026-05-30): a failed task_create
    used to be swallowed as [Friction] and the handler returned
    normally — the watcher then advanced past the issue and stranded
    it permanently. Propagation lets the dispatcher freeze the cursor.
    """
    lithos = _stub_lithos()
    lithos.task_create = AsyncMock(
        side_effect=LithosClientError("invalid_input", "missing field")
    )
    github = _stub_github()
    handler = make_handler(github)

    with pytest.raises(LithosClientError):
        await handler(_event(), _ctx(lithos))
    # Marker write never reached because create raised first.
    github.update_issue_body.assert_not_awaited()


@pytest.mark.asyncio
async def test_orphan_marker_path_propagates_lithos_list_failure() -> None:
    """PR-review finding 2 (round 3, 2026-05-30): a failed task_list during
    URL-match recovery used to be swallowed and the handler fell through
    to ``task_create``, producing a duplicate task on transient transport
    errors. Now it propagates so the dispatcher freezes the cursor."""
    lithos = _stub_lithos()
    lithos.task_list = AsyncMock(side_effect=OSError("connection refused"))
    handler = make_handler(_stub_github())

    with pytest.raises(OSError, match="connection refused"):
        await handler(_event(body="no marker"), _ctx(lithos))
    # Critical: NO duplicate task created when transient lookup failed.
    lithos.task_create.assert_not_awaited()


# ── Tag carry-over ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_labels_become_tags_with_github_issue_marker_tag() -> None:
    lithos = _stub_lithos()
    handler = make_handler(_stub_github())
    await handler(_event(labels=["bug", "ui", "needs-triage"]), _ctx(lithos))
    create_kwargs = lithos.task_create.await_args.kwargs
    assert "bug" in create_kwargs["tags"]
    assert "ui" in create_kwargs["tags"]
    assert "needs-triage" in create_kwargs["tags"]
    assert GITHUB_ISSUE_TAG in create_kwargs["tags"]


@pytest.mark.asyncio
async def test_marker_preserves_existing_body_text() -> None:
    """The marker is appended; the rest of the issue body is preserved."""
    lithos = _stub_lithos()
    lithos.task_create = AsyncMock(return_value="task-x")
    github = _stub_github()
    handler = make_handler(github)
    await handler(_event(body="Steps:\n1. foo\n2. bar"), _ctx(lithos))
    body = github.update_issue_body.await_args.args[2]
    assert "Steps:" in body
    assert "1. foo" in body
    assert "<!-- lithos:task-x -->" in body
    # Marker lands at the tail.
    assert body.rstrip().endswith("<!-- lithos:task-x -->")


# ── Race-narrowing fetch before marker PATCH ───────────────────────────


@pytest.mark.asyncio
async def test_marker_write_uses_fresh_body_from_get_issue() -> None:
    """Regression for PR-review finding 2: the marker writer was applying
    the marker to the poll-event body and PATCHing the full result. If
    the operator edited the body between poll and PATCH, their edit was
    overwritten. The writer must re-fetch via get_issue first.
    """
    from lithos_loom.github_client import Issue as IssueShape

    lithos = _stub_lithos()
    lithos.task_create = AsyncMock(return_value="task-x")
    github = _stub_github()
    # Simulate operator editing the body between poll and PATCH: the
    # poll event carries "original body"; get_issue returns the freshly
    # edited "EDITED" version.
    fresh = IssueShape(
        repo="agent-lore/lithos-loom",
        number=42,
        title="Test issue",
        body="EDITED by operator",
        state="open",
        state_reason=None,
        labels=("bug",),
        author="alice",
        updated_at=datetime(2026, 5, 29, 12, 0, 1, tzinfo=UTC),
        html_url="https://github.com/agent-lore/lithos-loom/issues/42",
    )
    github.get_issue = AsyncMock(return_value=fresh)
    handler = make_handler(github)

    await handler(_event(body="original body from poll"), _ctx(lithos))

    github.get_issue.assert_awaited_once()
    body_written = github.update_issue_body.await_args.args[2]
    # Operator's edit is preserved in the patched body.
    assert "EDITED by operator" in body_written
    # The stale poll-event body is NOT what we wrote.
    assert "original body from poll" not in body_written
    # Canonical marker still lands.
    assert "<!-- lithos:task-x -->" in body_written


@pytest.mark.asyncio
async def test_marker_write_falls_back_to_event_body_when_refetch_fails() -> None:
    """If get_issue raises or returns None, we still write the marker —
    using the stale poll-event body is better than skipping the marker
    entirely (which would trigger orphan-marker recovery next poll)."""
    from lithos_loom.github_client import GitHubAuthError

    lithos = _stub_lithos()
    lithos.task_create = AsyncMock(return_value="task-x")
    github = _stub_github()
    github.get_issue = AsyncMock(side_effect=GitHubAuthError("403 denied"))
    handler = make_handler(github)

    await handler(_event(body="from poll event"), _ctx(lithos))

    github.update_issue_body.assert_awaited_once()
    body_written = github.update_issue_body.await_args.args[2]
    assert "from poll event" in body_written
    assert "<!-- lithos:task-x -->" in body_written


# ── Exclude filters (PRD story #64) ───────────────────────────────────


@pytest.mark.asyncio
async def test_excluded_label_skips_task_create() -> None:
    """PR-review finding 6 (2026-05-30): a new issue carrying a project-
    excluded label is dropped at import time before task_create."""
    lithos = _stub_lithos()
    github = _stub_github()
    handler = make_handler(github)
    await handler(
        _event(
            body="no marker",
            labels=["automated", "bug"],
            exclude_labels=["automated"],
        ),
        _ctx(lithos),
    )
    lithos.task_create.assert_not_awaited()
    github.update_issue_body.assert_not_awaited()


@pytest.mark.asyncio
async def test_excluded_author_skips_task_create() -> None:
    """Dependabot-style automated issues are dropped at import time by
    matching the GH author login against the project's exclude list."""
    lithos = _stub_lithos()
    github = _stub_github()
    handler = make_handler(github)
    await handler(
        _event(
            body="no marker",
            author="dependabot[bot]",
            exclude_authors=["dependabot[bot]"],
        ),
        _ctx(lithos),
    )
    lithos.task_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_exclude_filter_does_not_block_already_linked_task() -> None:
    """PRD: exclude is *only* at import time. An issue that was already
    imported, then later had an excluded label added, must still drift-
    sync (and close-mirror) — we don't strand the existing task."""
    lithos = _stub_lithos()
    existing = _task(
        task_id="task-old",
        status="open",
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug"],
            "github_state_snapshot": "open",
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())
    await handler(
        _event(
            body="text\n<!-- lithos:task-old -->",
            labels=["automated", "bug"],
            exclude_labels=["automated"],
        ),
        _ctx(lithos),
    )
    # Drift still fires; the task is not abandoned.
    lithos.task_update.assert_awaited()


@pytest.mark.asyncio
async def test_no_exclude_filter_proceeds_as_normal() -> None:
    """Sanity: an empty exclude list does not block the create path."""
    lithos = _stub_lithos()
    github = _stub_github()
    handler = make_handler(github)
    await handler(
        _event(body="no marker", labels=["bug"]),
        _ctx(lithos),
    )
    lithos.task_create.assert_awaited_once()


# ── Slice 7.2: GH→Lithos drift sync ───────────────────────────────────


@pytest.mark.asyncio
async def test_drift_title_change_pushes_to_lithos() -> None:
    """GH title differs from Lithos task title → task_update(title=...)."""
    lithos = _stub_lithos()
    existing = _task(
        task_id="task-123",
        status="open",
        title="Old title",
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug"],
            "github_state_snapshot": "open",
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())
    await handler(
        _event(body="issue body\n<!-- lithos:task-123 -->", title="New title"),
        _ctx(lithos),
    )
    lithos.task_update.assert_awaited_once()
    update_kwargs = lithos.task_update.await_args.kwargs
    assert update_kwargs["task_id"] == "task-123"
    assert update_kwargs["title"] == "New title"


@pytest.mark.asyncio
async def test_drift_body_change_pushes_description_without_marker() -> None:
    """GH body differs from Lithos task description; marker is stripped before write."""
    lithos = _stub_lithos()
    existing = _task(
        status="open",
        description="old body",
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug"],
            "github_state_snapshot": "open",
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())
    new_body = "fresh issue body\n\n<!-- lithos:task-123 -->"
    await handler(
        _event(body=new_body),
        _ctx(lithos),
    )
    lithos.task_update.assert_awaited_once()
    kwargs = lithos.task_update.await_args.kwargs
    assert kwargs["description"] == "fresh issue body"
    # Marker MUST NOT leak into the Lithos task description.
    assert "<!-- lithos" not in kwargs["description"]


@pytest.mark.asyncio
async def test_drift_label_added_mirrors_to_tags_and_snapshot() -> None:
    """GH adds a label → Lithos tag added, github_labels snapshot bumped."""
    lithos = _stub_lithos()
    existing = _task(
        status="open",
        tags=("bug", GITHUB_ISSUE_TAG),
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug"],
            "github_state_snapshot": "open",
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())
    await handler(
        _event(body="b\n<!-- lithos:task-123 -->", labels=["bug", "needs-info"]),
        _ctx(lithos),
    )
    lithos.task_update.assert_awaited_once()
    kwargs = lithos.task_update.await_args.kwargs
    assert "needs-info" in kwargs["tags"]
    assert "bug" in kwargs["tags"]
    assert GITHUB_ISSUE_TAG in kwargs["tags"]
    assert kwargs["metadata"]["github_labels"] == ["bug", "needs-info"]


@pytest.mark.asyncio
async def test_drift_label_removed_drops_tag_but_preserves_operator_tags() -> None:
    """GH removes a label; operator-added Lithos tags survive."""
    lithos = _stub_lithos()
    existing = _task(
        status="open",
        # "operator-added-tag" was never in any GH snapshot — must survive.
        tags=("bug", "ui", "operator-added-tag", GITHUB_ISSUE_TAG),
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug", "ui"],
            "github_state_snapshot": "open",
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())
    # GH dropped "ui".
    await handler(
        _event(body="b\n<!-- lithos:task-123 -->", labels=["bug"]),
        _ctx(lithos),
    )
    lithos.task_update.assert_awaited_once()
    kwargs = lithos.task_update.await_args.kwargs
    new_tags = set(kwargs["tags"])
    assert "ui" not in new_tags
    assert "bug" in new_tags
    assert "operator-added-tag" in new_tags
    assert GITHUB_ISSUE_TAG in new_tags
    assert kwargs["metadata"]["github_labels"] == ["bug"]


@pytest.mark.asyncio
async def test_drift_no_changes_skips_task_update() -> None:
    """Steady-state poll: nothing changed → zero task_update calls."""
    lithos = _stub_lithos()
    existing = _task(
        status="open",
        title="Test issue",
        description="issue body",
        tags=("bug", GITHUB_ISSUE_TAG),
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug"],
            "github_state_snapshot": "open",
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())
    await handler(
        _event(body="issue body\n\n<!-- lithos:task-123 -->"),
        _ctx(lithos),
    )
    lithos.task_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_drift_combined_changes_one_task_update_call() -> None:
    """Efficiency: title + body + labels all changed → single task_update."""
    lithos = _stub_lithos()
    existing = _task(
        status="open",
        title="old",
        description="old body",
        tags=("bug", GITHUB_ISSUE_TAG),
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug"],
            "github_state_snapshot": "open",
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())
    await handler(
        _event(
            title="new",
            body="new body\n<!-- lithos:task-123 -->",
            labels=["bug", "needs-info"],
        ),
        _ctx(lithos),
    )
    assert lithos.task_update.await_count == 1
    kwargs = lithos.task_update.await_args.kwargs
    assert kwargs["title"] == "new"
    assert kwargs["description"] == "new body"
    assert "needs-info" in kwargs["tags"]
    assert kwargs["metadata"]["github_labels"] == ["bug", "needs-info"]


# ── Slice 7.2: state-snapshot tracking + reopen finding ───────────────


@pytest.mark.asyncio
async def test_open_to_closed_writes_snapshot_and_mirrors_close() -> None:
    """GH closes an open task; state_snapshot transitions to 'closed' and the
    close mirror still fires in the same poll."""
    lithos = _stub_lithos()
    existing = _task(
        status="open",
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug"],
            "github_state_snapshot": "open",
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())
    await handler(
        _event(
            body="issue body\n\n<!-- lithos:task-123 -->",
            state="closed",
            state_reason="completed",
        ),
        _ctx(lithos),
    )
    # Snapshot drift bumped → task_update with snapshot.
    lithos.task_update.assert_awaited_once()
    kwargs = lithos.task_update.await_args.kwargs
    assert kwargs["metadata"]["github_state_snapshot"] == "closed"
    # Close mirror fired in the same poll.
    lithos.task_complete.assert_awaited_once_with(
        task_id="task-123", agent="lithos-loom-agent"
    )


@pytest.mark.asyncio
async def test_reopen_after_close_posts_finding_once() -> None:
    """closed→open on a completed task posts the finding via finding_post.

    PRD #75: signal the operator a closed-then-reopened condition.
    Note (soak 2026-05-30): snapshot dedup via metadata.github_state_snapshot
    no longer works on terminal tasks because Lithos #303 makes task_update
    reject them. Drift sync is now skipped for terminal tasks entirely,
    so the snapshot stays at its prior value. Result: the reopen finding
    can re-fire on every subsequent poll while the GH issue stays open;
    the test ``test_reopen_with_snapshot_already_open_does_not_repost``
    still covers the open-task case via the snapshot path. Re-enable
    snapshot-on-terminal once #303 lands.
    """
    lithos = _stub_lithos()
    existing = _task(
        status="completed",
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug"],
            "github_state_snapshot": "closed",
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())
    await handler(
        _event(
            body="issue body\n\n<!-- lithos:task-123 -->",
            state="open",
        ),
        _ctx(lithos),
    )
    lithos.finding_post.assert_awaited_once()
    finding_kwargs = lithos.finding_post.await_args.kwargs
    assert finding_kwargs["task_id"] == "task-123"
    assert "[ReopenRequested]" in finding_kwargs["summary"]
    # Drift sync (and therefore the snapshot bump) is skipped on terminal
    # tasks; this is the Lithos #303 trade-off.
    lithos.task_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_reopen_with_snapshot_already_open_does_not_repost() -> None:
    """Second poll after a reopen: snapshot already 'open' → no duplicate finding."""
    lithos = _stub_lithos()
    existing = _task(
        status="completed",
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug"],
            "github_state_snapshot": "open",
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())
    await handler(
        _event(body="b\n\n<!-- lithos:task-123 -->", state="open"),
        _ctx(lithos),
    )
    lithos.finding_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_reopen_finding_failure_propagates_and_skips_snapshot_update() -> None:
    """PR-review finding 1 (round 3, 2026-05-30): finding_post failure
    in the reopen branch now propagates so the dispatcher freezes the
    cursor. Because the handler exits early on the raise, drift sync
    never runs and github_state_snapshot stays at its prior value —
    the next poll's closed-to-open guard re-fires.
    """
    lithos = _stub_lithos()
    existing = _task(
        status="completed",
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug"],
            "github_state_snapshot": "closed",
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    lithos.finding_post = AsyncMock(
        side_effect=LithosClientError("transport_error", "MCP outage")
    )
    handler = make_handler(_stub_github())

    with pytest.raises(LithosClientError):
        await handler(
            _event(body="b\n<!-- lithos:task-123 -->", state="open"),
            _ctx(lithos),
        )
    lithos.finding_post.assert_awaited_once()
    # Drift sync never ran → no task_update fired, snapshot untouched.
    lithos.task_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_reopen_on_legacy_task_without_snapshot_fires_finding() -> None:
    """A 7.1-era task has no github_state_snapshot yet. Treat missing as 'unknown'
    and fire the finding on first detection of completed+gh.open."""
    lithos = _stub_lithos()
    existing = _task(
        status="completed",
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug"],
            # NB: no github_state_snapshot key.
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())
    await handler(
        _event(body="b\n<!-- lithos:task-123 -->", state="open"),
        _ctx(lithos),
    )
    lithos.finding_post.assert_awaited_once()


@pytest.mark.asyncio
async def test_safe_call_swallows_task_not_found_without_freezing_cursor() -> None:
    """Soak observation (2026-05-30): Lithos's task_update returns
    ``task_not_found`` for terminal tasks (upstream #303). The drift
    path skips terminal tasks entirely now, but the swallow stays as
    a defence — if any other ``_safe_call`` site (close mirror on a
    terminal task that just became terminal between fetch and write,
    etc.) hits this race, the cursor must still advance rather than
    freeze. Exercise the swallow via the close-mirror branch where
    the task transitions to terminal between ``task_get`` and the
    inflight ``task_complete``.
    """
    lithos = _stub_lithos()
    # Task fetched as open but Lithos has since terminalised it; the
    # task_complete call comes back with task_not_found.
    existing = _task(task_id="task-x", status="open")
    lithos.task_get = AsyncMock(return_value=existing)
    lithos.task_complete = AsyncMock(
        side_effect=LithosClientError("task_not_found", "Task task-x not found")
    )
    handler = make_handler(_stub_github())

    # Must NOT raise — cursor must advance for the watcher's dispatcher.
    await handler(
        _event(
            body="b\n<!-- lithos:task-x -->",
            state="closed",
            state_reason="completed",
        ),
        _ctx(lithos),
    )
    lithos.task_complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_drift_on_terminal_task_is_skipped() -> None:
    """Soak 2026-05-30: Lithos #303 (task_update rejects terminal tasks).
    A poll that fetches a closed GH issue paired with a terminal Lithos
    task used to attempt drift sync every cycle and log [Friction] for
    the rejection. Now skipped entirely on terminal tasks. Reopen
    finding still fires (uses finding_post, not task_update) so the
    operator still gets the signal even while task_update is locked
    out upstream.
    """
    lithos = _stub_lithos()
    existing = _task(
        status="completed",
        title="old title",
        description="old body",
        tags=("bug", GITHUB_ISSUE_TAG),
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug"],
            "github_state_snapshot": "closed",
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())
    await handler(
        _event(
            body="new body\n<!-- lithos:task-123 -->",
            title="renamed",
            labels=["bug", "needs-info"],
            state="open",
        ),
        _ctx(lithos),
    )
    # Reopen finding still fires (separate MCP endpoint, accepts terminal tasks).
    lithos.finding_post.assert_awaited_once()
    # Drift sync skipped entirely — no task_update attempt at all.
    lithos.task_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_initialises_state_snapshot_in_metadata() -> None:
    """7.2 task-create now stamps github_state_snapshot=<issue.state> at birth."""
    lithos = _stub_lithos()
    handler = make_handler(_stub_github())
    await handler(_event(), _ctx(lithos))
    kwargs = lithos.task_create.await_args.kwargs
    assert kwargs["metadata"]["github_state_snapshot"] == "open"


@pytest.mark.asyncio
async def test_drift_runs_before_close_in_same_poll() -> None:
    """Title rename + GH close arriving together: both drift and close fire."""
    lithos = _stub_lithos()
    existing = _task(
        status="open",
        title="old title",
        metadata={
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_labels": ["bug"],
            "github_state_snapshot": "open",
        },
    )
    lithos.task_get = AsyncMock(return_value=existing)
    handler = make_handler(_stub_github())
    await handler(
        _event(
            body="b\n\n<!-- lithos:task-123 -->",
            title="new title",
            state="closed",
            state_reason="completed",
        ),
        _ctx(lithos),
    )
    # Drift applied: title pushed.
    update_kwargs = lithos.task_update.await_args.kwargs
    assert update_kwargs["title"] == "new title"
    assert update_kwargs["metadata"]["github_state_snapshot"] == "closed"
    # Close also fired.
    lithos.task_complete.assert_awaited_once()
