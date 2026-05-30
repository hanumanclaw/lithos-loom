"""Tests for ``lithos_loom.subscriptions._github_issue_push`` (Slice 7.2).

The push handler consumes Lithos task state-change events and mirrors
them into the linked GitHub issue:

- ``lithos.task.completed`` / ``cancelled`` → close GH with state_reason
- ``lithos.task.updated`` → if title changed, PATCH GH title

Tests use stubbed GitHub clients and dict event payloads matching the
shape produced by :func:`lithos_loom.sources.lithos_event_stream._event_payload`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event
from lithos_loom.github_client import GitHubAuthError, GitHubError, Issue
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._github_issue_push import EVENT_TYPES, make_handler

# ── Builders ──────────────────────────────────────────────────────────


def _ctx(lithos: Any = None) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos,
        logger=logging.getLogger("test-github-issue-push"),
        agent_id="lithos-loom-agent",
    )


def _payload(
    *,
    task_id: str = "task-123",
    title: str = "Test issue",
    status: str = "completed",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if metadata is None:
        metadata = {
            "github_issue_url": "https://github.com/agent-lore/lithos-loom/issues/42",
            "github_issue_number": 42,
            "project": "lithos-loom",
        }
    return {
        "id": task_id,
        "title": title,
        "status": status,
        "tags": ["bug"],
        "metadata": metadata,
        "claims": [],
        "resolved_at": None,
    }


def _event(event_type: str, payload: dict[str, Any]) -> Event:
    return Event(
        type=event_type,
        timestamp=datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        payload=payload,
    )


def _issue(
    *,
    state: str = "open",
    state_reason: str | None = None,
    title: str = "Test issue",
    repo: str = "agent-lore/lithos-loom",
    number: int = 42,
) -> Issue:
    return Issue(
        repo=repo,
        number=number,
        title=title,
        body="b",
        state=state,
        state_reason=state_reason,
        labels=(),
        author="alice",
        updated_at=datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        html_url=f"https://github.com/{repo}/issues/{number}",
    )


def _stub_github(*, issue: Issue | None = None) -> AsyncMock:
    gh = AsyncMock()
    gh.get_issue = AsyncMock(return_value=issue if issue is not None else _issue())
    gh.update_issue_fields = AsyncMock()
    return gh


# ── Close mirror branch ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completed_event_closes_gh_issue_as_completed() -> None:
    github = _stub_github(issue=_issue(state="open"))
    handler = make_handler(github)
    await handler(_event("lithos.task.completed", _payload(status="completed")), _ctx())
    github.update_issue_fields.assert_awaited_once_with(
        "agent-lore/lithos-loom", 42, state="closed", state_reason="completed"
    )


@pytest.mark.asyncio
async def test_cancelled_event_closes_gh_issue_as_not_planned() -> None:
    github = _stub_github(issue=_issue(state="open"))
    handler = make_handler(github)
    await handler(_event("lithos.task.cancelled", _payload(status="cancelled")), _ctx())
    github.update_issue_fields.assert_awaited_once_with(
        "agent-lore/lithos-loom", 42, state="closed", state_reason="not_planned"
    )


@pytest.mark.asyncio
async def test_completed_event_skips_when_gh_already_closed_as_completed() -> None:
    """Idempotency: GH→Lithos path may have already closed the issue."""
    github = _stub_github(issue=_issue(state="closed", state_reason="completed"))
    handler = make_handler(github)
    await handler(_event("lithos.task.completed", _payload()), _ctx())
    github.update_issue_fields.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_event_repatches_when_gh_closed_with_wrong_reason() -> None:
    """GH closed as not_planned but Lithos task is completed → re-close.

    Rare but possible: the operator closed on GH first as not_planned,
    then later ticked [x] in Obsidian (which routes through
    task_complete). The two state_reasons should converge.
    """
    github = _stub_github(issue=_issue(state="closed", state_reason="not_planned"))
    handler = make_handler(github)
    await handler(_event("lithos.task.completed", _payload()), _ctx())
    github.update_issue_fields.assert_awaited_once_with(
        "agent-lore/lithos-loom", 42, state="closed", state_reason="completed"
    )


@pytest.mark.asyncio
async def test_completed_event_without_github_metadata_is_skipped() -> None:
    """Most Lithos tasks aren't linked to a GH issue; the handler is a no-op
    for the common case and must not spam the log at INFO."""
    github = _stub_github()
    handler = make_handler(github)
    payload = _payload(metadata={"project": "some-other-project"})
    await handler(_event("lithos.task.completed", payload), _ctx())
    github.get_issue.assert_not_awaited()
    github.update_issue_fields.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_event_skips_when_gh_issue_deleted() -> None:
    """Operator deleted the GH issue → get_issue returns None → no PATCH."""
    github = _stub_github()
    github.get_issue = AsyncMock(return_value=None)
    handler = make_handler(github)
    await handler(_event("lithos.task.completed", _payload()), _ctx())
    github.update_issue_fields.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_mirror_swallows_permanent_gh_errors() -> None:
    """Permanent GitHub errors (auth, repo not found) are logged + dropped —
    no point retrying a permission denial."""
    github = _stub_github(issue=_issue(state="open"))
    github.update_issue_fields = AsyncMock(side_effect=GitHubAuthError("403"))
    handler = make_handler(github)
    # Must not raise on permanent errors.
    await handler(_event("lithos.task.completed", _payload()), _ctx())


@pytest.mark.asyncio
async def test_close_mirror_propagates_transient_gh_errors() -> None:
    """PR-review finding 3 (round 3, 2026-05-30): transient errors
    (5xx, network blips) must propagate so the consumer loop can retry
    with backoff. The previous code swallowed every GitHubError
    indiscriminately, turning a Cloudflare hiccup into a permanently
    lost close-mirror event.
    """
    github = _stub_github(issue=_issue(state="open"))
    github.update_issue_fields = AsyncMock(
        side_effect=GitHubError("500 internal server error")
    )
    handler = make_handler(github)
    with pytest.raises(GitHubError):
        await handler(_event("lithos.task.completed", _payload()), _ctx())


@pytest.mark.asyncio
async def test_close_mirror_propagates_transient_get_issue_failure() -> None:
    """get_issue failing with a transient error also propagates."""
    github = _stub_github()
    github.get_issue = AsyncMock(side_effect=GitHubError("502 bad gateway"))
    handler = make_handler(github)
    with pytest.raises(GitHubError):
        await handler(_event("lithos.task.completed", _payload()), _ctx())


# ── Title sync branch ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_updated_event_with_renamed_title_patches_gh() -> None:
    github = _stub_github(issue=_issue(title="old title"))
    handler = make_handler(github)
    payload = _payload(title="new title", status="open")
    await handler(_event("lithos.task.updated", payload), _ctx())
    github.update_issue_fields.assert_awaited_once_with(
        "agent-lore/lithos-loom", 42, title="new title"
    )


@pytest.mark.asyncio
async def test_updated_event_with_unchanged_title_skips_patch() -> None:
    """Steady-state efficiency: title.unchanged → no PATCH."""
    github = _stub_github(issue=_issue(title="same title"))
    handler = make_handler(github)
    payload = _payload(title="same title", status="open")
    await handler(_event("lithos.task.updated", payload), _ctx())
    github.update_issue_fields.assert_not_awaited()


@pytest.mark.asyncio
async def test_updated_event_without_github_metadata_is_skipped() -> None:
    github = _stub_github()
    handler = make_handler(github)
    payload = _payload(title="renamed", metadata={"project": "unlinked"})
    await handler(_event("lithos.task.updated", payload), _ctx())
    github.get_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_updated_event_handles_gh_404_gracefully() -> None:
    """Issue deleted operator-side → no PATCH, no exception."""
    github = _stub_github()
    github.get_issue = AsyncMock(return_value=None)
    handler = make_handler(github)
    await handler(_event("lithos.task.updated", _payload(title="renamed")), _ctx())
    github.update_issue_fields.assert_not_awaited()


# ── Robustness ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_event_type_is_ignored() -> None:
    github = _stub_github()
    handler = make_handler(github)
    await handler(
        Event(
            type="some.other.event",
            timestamp=datetime(2026, 5, 29, tzinfo=UTC),
            payload=_payload(),
        ),
        _ctx(),
    )
    github.get_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_url_in_metadata_is_skipped() -> None:
    """Non-github.com URL in metadata → defensive skip."""
    github = _stub_github()
    handler = make_handler(github)
    payload = _payload(metadata={"github_issue_url": "https://example.com/issues/42"})
    await handler(_event("lithos.task.completed", payload), _ctx())
    github.get_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_types_constant_exposed() -> None:
    """The child wires its bus subscription against EVENT_TYPES."""
    assert "lithos.task.created" in EVENT_TYPES
    assert "lithos.task.completed" in EVENT_TYPES
    assert "lithos.task.cancelled" in EVENT_TYPES
    assert "lithos.task.updated" in EVENT_TYPES


@pytest.mark.asyncio
async def test_created_event_with_title_drift_patches_gh() -> None:
    """PR-review finding 4 (round 3, 2026-05-30): bootstrap replays the
    open-task snapshot as ``lithos.task.created``. If a Lithos title was
    renamed while the watcher was down, that rename only surfaces on
    restart via this event type — the push handler must mirror it to
    GH so the next inbound poll doesn't overwrite the Lithos rename.
    """
    github = _stub_github(issue=_issue(title="old title"))
    handler = make_handler(github)
    payload = _payload(title="new title", status="open")
    await handler(_event("lithos.task.created", payload), _ctx())
    github.update_issue_fields.assert_awaited_once_with(
        "agent-lore/lithos-loom", 42, title="new title"
    )


@pytest.mark.asyncio
async def test_created_event_with_no_title_drift_is_noop() -> None:
    """Steady-state bootstrap replay: open task title matches GH → no PATCH."""
    github = _stub_github(issue=_issue(title="same title"))
    handler = make_handler(github)
    payload = _payload(title="same title", status="open")
    await handler(_event("lithos.task.created", payload), _ctx())
    github.update_issue_fields.assert_not_awaited()
