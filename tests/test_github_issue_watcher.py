"""Tests for ``lithos_loom.sources.github_issue_watcher``.

The watcher is a polling source; we exercise it by calling its private
loops directly (``_bootstrap`` / ``_poll_one_repo`` / etc) rather than
running ``run()`` to completion. Stubs replace both the github_client
and the Lithos surface so the tests neither hit the network nor depend
on a running Lithos.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event, EventBus
from lithos_loom.cli._github_metadata import (
    GITHUB_EXCLUDE_AUTHORS_KEY,
    GITHUB_EXCLUDE_LABELS_KEY,
    GITHUB_REPOS_KEY,
    GITHUB_WATCH_KEY,
)
from lithos_loom.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubRepoNotFoundError,
    Issue,
)
from lithos_loom.lithos_client import Note, NoteSummary, WriteResult
from lithos_loom.sources.github_issue_watcher import (
    GITHUB_ISSUE_EVENT_TYPE,
    GitHubIssueWatcher,
    WatchedRepo,
    format_cursors,
    parse_cursors,
    parse_stuck,
)

# ── Cursor doc format ─────────────────────────────────────────────────


def test_format_then_parse_round_trips() -> None:
    cursors = {
        "agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        "agent-lore/lithos": datetime(2026, 5, 28, 11, 30, 0, tzinfo=UTC),
    }
    body = format_cursors(cursors)
    parsed = parse_cursors(body)
    assert parsed == cursors


def test_parse_cursors_handles_empty_body() -> None:
    assert parse_cursors("") == {}


def test_parse_cursors_skips_comment_and_blank_lines() -> None:
    body = (
        "# header\n"
        "Daemon-owned coordination doc.\n"
        "\n"
        "agent-lore/lithos-loom 2026-05-29T12:00:00+00:00\n"
    )
    assert parse_cursors(body) == {
        "agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    }


def test_parse_cursors_ignores_unparseable_lines() -> None:
    body = (
        "valid/repo 2026-05-29T12:00:00Z\n"
        "noslashtimestamp invalid\n"
        "owner/name not-a-timestamp\n"
    )
    assert parse_cursors(body) == {
        "valid/repo": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    }


def test_parse_cursors_accepts_z_suffix() -> None:
    assert parse_cursors("owner/name 2026-05-29T12:00:00Z") == {
        "owner/name": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    }


# ── Test plumbing ─────────────────────────────────────────────────────


def _summary(
    *,
    slug: str,
    repo: str | None = None,
    repos: tuple[str, ...] | None = None,
    watching: bool,
    exclude_labels: tuple[str, ...] = (),
    exclude_authors: tuple[str, ...] = (),
) -> NoteSummary:
    """Build a project-context ``NoteSummary`` carrying github-watcher
    config in metadata. Pass ``repo`` for the single-repo case or
    ``repos`` for a project tracking several."""
    if repos is not None:
        repo_list = list(repos)
    elif repo is not None:
        repo_list = [repo]
    else:
        repo_list = []
    metadata: dict[str, Any] = {GITHUB_WATCH_KEY: watching}
    if repo_list:
        metadata[GITHUB_REPOS_KEY] = repo_list
    if exclude_labels:
        metadata[GITHUB_EXCLUDE_LABELS_KEY] = list(exclude_labels)
    if exclude_authors:
        metadata[GITHUB_EXCLUDE_AUTHORS_KEY] = list(exclude_authors)
    return NoteSummary(
        id=f"doc-{slug}",
        title=slug.title(),
        version=1,
        updated_at=datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        tags=("project-context",),
        status="active",
        note_type="concept",
        path=f"projects/{slug}/{slug}-project-context.md",
        slug=slug,
        metadata=metadata,
    )


def _make_issue(
    *,
    number: int = 1,
    repo: str = "agent-lore/lithos-loom",
    state: str = "open",
    state_reason: str | None = None,
    updated_at: datetime | None = None,
) -> Issue:
    return Issue(
        repo=repo,
        number=number,
        title=f"Issue {number}",
        body="body",
        state=state,
        state_reason=state_reason,
        labels=("bug",),
        author="alice",
        updated_at=updated_at or datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        html_url=f"https://github.com/{repo}/issues/{number}",
    )


def _fake_github_client() -> Any:
    """An AsyncMock shaped like the GitHubClient surface the watcher uses."""
    gh = AsyncMock()
    gh.list_issues_since = AsyncMock(return_value=[])
    return gh


def _fake_lithos_client(
    *,
    note_list_return: list[NoteSummary] | None = None,
    note_read_return: Note | None = None,
    write_result: WriteResult | None = None,
) -> Any:
    client = AsyncMock()
    client.note_list = AsyncMock(return_value=note_list_return or [])
    client.note_read = AsyncMock(return_value=note_read_return)
    client.note_write = AsyncMock(
        return_value=write_result or WriteResult(status="updated")
    )
    return client


def _make_watcher(
    *,
    github: Any,
    lithos: Any,
    bus: EventBus | None = None,
    dispatch: Any = None,
) -> GitHubIssueWatcher:
    return GitHubIssueWatcher(
        github=cast(GitHubClient, github),
        lithos=lithos,
        bus=bus or EventBus(),
        poll_interval_seconds=60,
        coord_doc_path="projects/_lithos-loom-internal/github-watcher-state.md",
        agent_id="test-agent",
        dispatch=dispatch,
    )


async def _drain(bus: EventBus, queue_size: int = 64) -> list[Event]:
    """Subscribe broadly and drain whatever's queued (testing util)."""
    sub = bus.subscribe(
        event_types=(GITHUB_ISSUE_EVENT_TYPE,),
        queue_size=queue_size,
    )
    out: list[Event] = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


# ── _refresh_watch_list ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_watch_list_picks_up_watched_projects() -> None:
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="lithos-loom", repo="agent-lore/lithos-loom", watching=True),
            _summary(slug="lithos", repo="agent-lore/lithos", watching=True),
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._refresh_watch_list()
    assert watcher._watch_list == {
        "lithos-loom": WatchedRepo(repos=("agent-lore/lithos-loom",)),
        "lithos": WatchedRepo(repos=("agent-lore/lithos",)),
    }
    # The watch-enabled metadata filter flows into the query.
    call = lithos.note_list.await_args
    assert call.kwargs["path_prefix"] == "projects/"
    assert call.kwargs["metadata_match"] == {GITHUB_WATCH_KEY: True}


@pytest.mark.asyncio
async def test_refresh_watch_list_skips_projects_without_repos() -> None:
    """Operator drift: an enabled doc with no github_repos is logged + skipped."""
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="lithos-loom", repo=None, watching=True),
            _summary(slug="lithos", repo="agent-lore/lithos", watching=True),
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._refresh_watch_list()
    assert watcher._watch_list == {"lithos": WatchedRepo(repos=("agent-lore/lithos",))}


@pytest.mark.asyncio
async def test_refresh_watch_list_maps_multiple_repos_per_project() -> None:
    """A project may track several repos; all land in one WatchedRepo and
    each is polled independently."""
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(
                slug="kindred-code",
                repos=("kindred/web", "kindred/api", "kindred/infra"),
                watching=True,
            ),
        ]
    )
    issue = _make_issue(number=7)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[issue])
    watcher = _make_watcher(github=github, lithos=lithos)
    await watcher._refresh_watch_list()
    assert watcher._watch_list == {
        "kindred-code": WatchedRepo(
            repos=("kindred/web", "kindred/api", "kindred/infra")
        )
    }
    # The poll cycle fans out to one fetch per repo.
    await watcher._poll_all_repos()
    polled = {call.args[0] for call in github.list_issues_since.await_args_list}
    assert polled == {"kindred/web", "kindred/api", "kindred/infra"}


@pytest.mark.asyncio
async def test_refresh_resets_cursor_when_exclude_filter_changes() -> None:
    """PR-review finding 5 (round 3, 2026-05-30): when the operator
    relaxes a ``github_exclude_labels`` entry, the watcher must drop the
    repo cursor so previously-skipped issues re-surface. Otherwise the
    cursor sits past their ``updated_at`` and the next poll won't see
    them until someone edits them on GitHub.
    """
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(
                slug="lithos-loom",
                repo="agent-lore/lithos-loom",
                watching=True,
                exclude_labels=("automated",),
            )
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._refresh_watch_list()
    # Watcher polled for a while; cursor is set.
    watcher._cursors["agent-lore/lithos-loom"] = datetime(2026, 5, 29, tzinfo=UTC)

    # Operator removes the exclude tag.
    lithos.note_list = AsyncMock(
        return_value=[
            _summary(
                slug="lithos-loom",
                repo="agent-lore/lithos-loom",
                watching=True,
                exclude_labels=(),
            )
        ]
    )
    await watcher._refresh_watch_list()

    # Cursor reset — next poll bootstrap-walks open issues so the
    # previously-excluded ones surface.
    assert "agent-lore/lithos-loom" not in watcher._cursors


@pytest.mark.asyncio
async def test_refresh_resets_cursor_when_repo_unwatched_and_rewatched() -> None:
    """Removing the watch enrolment and re-adding it later must not
    silently resume from the stale cursor — operator might have meant
    a clean re-bootstrap."""
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="lithos-loom", repo="agent-lore/lithos-loom", watching=True)
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._refresh_watch_list()
    watcher._cursors["agent-lore/lithos-loom"] = datetime(2026, 5, 29, tzinfo=UTC)

    # Disable watching.
    lithos.note_list = AsyncMock(return_value=[])
    await watcher._refresh_watch_list()
    assert "agent-lore/lithos-loom" not in watcher._cursors


@pytest.mark.asyncio
async def test_refresh_adding_sibling_repo_keeps_existing_cursor() -> None:
    """Adding a second repo to a project must NOT reset the cursor of the
    repo it already tracks — only the newly-added repo bootstraps."""
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="kindred-code", repos=("kindred/web",), watching=True)
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._refresh_watch_list()
    watcher._cursors["kindred/web"] = datetime(2026, 5, 29, tzinfo=UTC)

    # Operator adds a sibling repo to the same project.
    lithos.note_list = AsyncMock(
        return_value=[
            _summary(
                slug="kindred-code",
                repos=("kindred/web", "kindred/api"),
                watching=True,
            )
        ]
    )
    await watcher._refresh_watch_list()

    # Existing repo's cursor is untouched; the new repo has none yet.
    assert watcher._cursors["kindred/web"] == datetime(2026, 5, 29, tzinfo=UTC)
    assert "kindred/api" not in watcher._cursors


@pytest.mark.asyncio
async def test_refresh_watch_list_preserves_state_on_transport_failure() -> None:
    """Refresh failure shouldn't blank the watch list operators rely on."""
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="lithos-loom", repo="agent-lore/lithos-loom", watching=True),
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._refresh_watch_list()
    assert watcher._watch_list == {
        "lithos-loom": WatchedRepo(repos=("agent-lore/lithos-loom",))
    }
    # Second call raises transport error.
    lithos.note_list.side_effect = OSError("connection refused")
    await watcher._refresh_watch_list()
    # State preserved.
    assert watcher._watch_list == {
        "lithos-loom": WatchedRepo(repos=("agent-lore/lithos-loom",))
    }


# ── _load_cursors_from_coord_doc ──────────────────────────────────────


@pytest.mark.asyncio
async def test_load_cursors_missing_doc_treats_as_first_run() -> None:
    lithos = _fake_lithos_client(note_read_return=None)
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._load_cursors_from_coord_doc()
    assert watcher._cursors == {}
    assert watcher._coord_doc_id is None


@pytest.mark.asyncio
async def test_load_cursors_parses_existing_doc() -> None:
    body = format_cursors(
        {"agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)}
    )
    note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body=body,
        version=7,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(note_read_return=note)
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    await watcher._load_cursors_from_coord_doc()
    assert watcher._cursors == {
        "agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    }
    assert watcher._coord_doc_id == "coord-id"
    assert watcher._coord_doc_version == 7


# ── _poll_one_repo ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_one_repo_publishes_issue_events() -> None:
    bus = EventBus()
    sub = bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    issue = _make_issue(number=42)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[issue])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    watcher._watch_list = {
        "lithos-loom": WatchedRepo(repos=("agent-lore/lithos-loom",))
    }

    await watcher._poll_one_repo(slug="lithos-loom", repo="agent-lore/lithos-loom")

    assert sub.queue.qsize() == 1
    event = sub.queue.get_nowait()
    assert event.type == GITHUB_ISSUE_EVENT_TYPE
    assert event.payload["slug"] == "lithos-loom"
    assert event.payload["repo"] == "agent-lore/lithos-loom"
    assert event.payload["number"] == 42
    # Cursor sits exactly at the boundary issue's updated_at; the +1s
    # nudge that used to live here was removed because it silently
    # dropped same-second sibling updates (PR-review finding 3,
    # 2026-05-30). Idempotent replay is the safer tradeoff.
    assert watcher._cursors["agent-lore/lithos-loom"] == issue.updated_at


@pytest.mark.asyncio
async def test_poll_one_repo_bootstrap_uses_state_open() -> None:
    """Regression for PR-review finding (round 3): without a cursor,
    bootstrap must list open issues only — using state=all means the
    paginated listing leads with the oldest closed history and the
    watcher spends multiple poll cycles burning through historic
    closures before reaching live open issues, breaking PRD US-56.
    """
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    # No cursor for the repo → bootstrap path.

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    call = github.list_issues_since.await_args
    assert call is not None
    assert call.kwargs["since"] is None
    assert call.kwargs["state"] == "open"


@pytest.mark.asyncio
async def test_poll_one_repo_incremental_uses_state_all() -> None:
    """With a cursor present, the poll must use state=all so state
    transitions (open→closed) on previously-seen issues surface."""
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    watcher._cursors["agent-lore/lithos-loom"] = datetime(2026, 5, 29, tzinfo=UTC)

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    call = github.list_issues_since.await_args
    assert call is not None
    assert call.kwargs["state"] == "all"


@pytest.mark.asyncio
async def test_poll_one_repo_surfaces_closed_issue_state_to_handler() -> None:
    """Regression for PR-review finding 1: the source was hard-coded to
    state="open", so close events never reached the subscription handler
    and the GH→Lithos close mirror was effectively unimplemented.

    The watcher must surface state="closed" issues (with their
    state_reason) so the handler can drive task_complete / task_cancel.
    Incremental poll path (cursor present), which uses state="all".
    """
    bus = EventBus()
    sub = bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    closed = _make_issue(number=99, state="closed", state_reason="completed")
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[closed])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    # Cursor present → incremental (state="all") path, which is where
    # closes naturally surface.
    watcher._cursors["agent-lore/lithos-loom"] = datetime(2026, 5, 29, tzinfo=UTC)

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    assert sub.queue.qsize() == 1
    event = sub.queue.get_nowait()
    assert event.payload["state"] == "closed"
    assert event.payload["state_reason"] == "completed"


@pytest.mark.asyncio
async def test_poll_one_repo_advances_cursor_to_latest_when_multiple_issues() -> None:
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    early = _make_issue(
        number=1, updated_at=datetime(2026, 5, 29, 10, 0, 0, tzinfo=UTC)
    )
    late = _make_issue(number=2, updated_at=datetime(2026, 5, 29, 13, 0, 0, tzinfo=UTC))
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[early, late])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")
    # PR-review finding 3 (2026-05-30): cursor is exactly max(updated_at).
    # The earlier +1s nudge silently dropped any *other* issue updated
    # within the same wall second; correctness beats one extra idempotent
    # task_list call.
    assert watcher._cursors["agent-lore/lithos-loom"] == late.updated_at


@pytest.mark.asyncio
async def test_poll_one_repo_holds_cursor_when_dispatch_fails_mid_batch() -> None:
    """PR-review finding 1 (2026-05-30): with the bus path, a queue-full
    drop or a handler exception silently advanced the cursor past
    issues that were never reconciled. With an injected inline
    dispatcher the watcher walks issues in updated_at-asc order and
    holds the cursor at the last successfully dispatched issue's
    timestamp; the failed issue's updated_at is re-fetched next poll.
    """
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    first = _make_issue(
        number=1, updated_at=datetime(2026, 5, 29, 10, 0, 0, tzinfo=UTC)
    )
    second = _make_issue(
        number=2, updated_at=datetime(2026, 5, 29, 11, 0, 0, tzinfo=UTC)
    )
    third = _make_issue(
        number=3, updated_at=datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    )
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[first, second, third])

    dispatched: list[int] = []

    async def flaky_dispatch(event: Any) -> None:
        n = event.payload["number"]
        dispatched.append(n)
        if n == 2:
            raise RuntimeError("Lithos went away")

    watcher = _make_watcher(
        github=github,
        lithos=_fake_lithos_client(),
        bus=bus,
        dispatch=flaky_dispatch,
    )

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    # Issue 1 dispatched, issue 2 failed → loop stopped; issue 3 never tried.
    assert dispatched == [1, 2]
    # Cursor sits at the latest successful issue (1), not the failed one
    # (2) and not the latest seen (3) — next poll re-fetches 2 onward.
    assert watcher._cursors["agent-lore/lithos-loom"] == first.updated_at


@pytest.mark.asyncio
async def test_poll_one_repo_does_not_advance_cursor_when_first_issue_fails() -> None:
    """First-issue dispatch failure: cursor stays at its prior value
    (or absent) so the next poll re-fetches the same boundary.
    """
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    issue = _make_issue(
        number=1, updated_at=datetime(2026, 5, 29, 10, 0, 0, tzinfo=UTC)
    )
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[issue])

    async def failing_dispatch(_: Any) -> None:
        raise RuntimeError("Lithos went away")

    watcher = _make_watcher(
        github=github,
        lithos=_fake_lithos_client(),
        bus=bus,
        dispatch=failing_dispatch,
    )
    prior = datetime(2026, 5, 1, tzinfo=UTC)
    watcher._cursors["agent-lore/lithos-loom"] = prior

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    assert watcher._cursors["agent-lore/lithos-loom"] == prior


@pytest.mark.asyncio
async def test_poll_one_repo_404_drops_only_that_repo() -> None:
    """A 404 on one repo of a multi-repo project must drop only that
    repo (and its cursor) — the project's other repos keep being
    polled."""
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(side_effect=GitHubRepoNotFoundError("gone"))
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client())
    watcher._watch_list = {
        "kindred-code": WatchedRepo(repos=("kindred/web", "kindred/gone"))
    }
    watcher._cursors["kindred/gone"] = datetime(2026, 5, 29, tzinfo=UTC)
    watcher._cursors["kindred/web"] = datetime(2026, 5, 28, tzinfo=UTC)

    await watcher._poll_one_repo(slug="kindred-code", repo="kindred/gone")

    # Only the 404 repo is dropped; the sibling and its cursor survive.
    assert watcher._watch_list == {"kindred-code": WatchedRepo(repos=("kindred/web",))}
    assert "kindred/gone" not in watcher._cursors
    assert watcher._cursors["kindred/web"] == datetime(2026, 5, 28, tzinfo=UTC)


@pytest.mark.asyncio
async def test_poll_one_repo_404_on_last_repo_drops_slug() -> None:
    """When the 404 repo was the project's only repo, the slug is
    dropped entirely."""
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(side_effect=GitHubRepoNotFoundError("gone"))
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client())
    watcher._watch_list = {"solo": WatchedRepo(repos=("owner/only",))}

    await watcher._poll_one_repo(slug="solo", repo="owner/only")

    assert watcher._watch_list == {}


@pytest.mark.asyncio
async def test_stuck_issue_retried_by_number_next_poll() -> None:
    """PR-review finding 2 (round 4, 2026-05-30): an issue that failed
    dispatch during bootstrap (cursor None) and then closes on GH
    before the next poll would disappear from the next state="open"
    walk. The watcher now re-fetches stuck issues by number via
    ``get_issue`` so the close-before-retry race no longer strands the
    linked Lithos task.
    """
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    open_issue = _make_issue(
        number=42, updated_at=datetime(2026, 5, 29, 10, 0, 0, tzinfo=UTC)
    )
    closed_issue = _make_issue(
        number=42,
        state="closed",
        state_reason="completed",
        updated_at=datetime(2026, 5, 29, 11, 0, 0, tzinfo=UTC),
    )
    github = _fake_github_client()
    # First poll fetches open, dispatch fails, issue gets stuck.
    # Second poll's retry-by-number sees the closed state.
    github.list_issues_since = AsyncMock(side_effect=[[open_issue], []])
    github.get_issue = AsyncMock(return_value=closed_issue)

    attempt_count = 0

    async def flaky_dispatch(event: Any) -> None:
        nonlocal attempt_count
        attempt_count += 1
        # First call (during initial bootstrap) raises.
        # Second call (the by-number retry) succeeds.
        if attempt_count == 1:
            raise RuntimeError("transient")

    watcher = _make_watcher(
        github=github,
        lithos=_fake_lithos_client(),
        bus=bus,
        dispatch=flaky_dispatch,
    )

    # First poll: bootstrap, fails, issue 42 is stuck.
    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")
    assert 42 in watcher._stuck_issues.get("agent-lore/lithos-loom", set())

    # Second poll: get_issue returns closed state, dispatch succeeds,
    # stuck set drains.
    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")
    github.get_issue.assert_awaited_with("agent-lore/lithos-loom", 42)
    assert watcher._stuck_issues.get("agent-lore/lithos-loom", set()) == set()


@pytest.mark.asyncio
async def test_stuck_issue_dropped_when_gh_returns_404() -> None:
    """Operator deleted the issue between polls. get_issue returns None;
    the stuck entry drops without further retry."""
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[])
    github.get_issue = AsyncMock(return_value=None)

    async def dispatch_ok(_: Any) -> None:
        return None

    watcher = _make_watcher(
        github=github,
        lithos=_fake_lithos_client(),
        bus=bus,
        dispatch=dispatch_ok,
    )
    watcher._stuck_issues["agent-lore/lithos-loom"] = {42}

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    assert "agent-lore/lithos-loom" not in watcher._stuck_issues


@pytest.mark.asyncio
async def test_stuck_issue_auth_error_does_not_drop_entry() -> None:
    """PR-review finding 2 (round 5, 2026-05-30): an auth failure on
    get_issue used to drop the stuck entry as if it were permanent.
    Credentials can be rotated later — the entry must stay so the
    eventual recovery picks it up. Only None (issue genuinely deleted
    on GH) or a successful dispatch retires the entry."""
    from lithos_loom.github_client import GitHubAuthError

    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[])
    github.get_issue = AsyncMock(side_effect=GitHubAuthError("403 denied"))

    async def dispatch_ok(_: Any) -> None:
        return None

    watcher = _make_watcher(
        github=github,
        lithos=_fake_lithos_client(),
        bus=bus,
        dispatch=dispatch_ok,
    )
    watcher._stuck_issues["agent-lore/lithos-loom"] = {42}

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    # Stuck entry preserved despite the auth error — operator might
    # repair credentials and the next poll picks it up.
    assert 42 in watcher._stuck_issues["agent-lore/lithos-loom"]


@pytest.mark.asyncio
async def test_stuck_issues_persist_and_reload_through_coord_doc() -> None:
    """PR-review finding 3 (round 5, 2026-05-30): the stuck-issue set
    rides on the coord doc so daemon restart preserves repair records.
    Without persistence, an issue stuck between an incomplete
    task_create + marker write and the next retry can be lost when
    the daemon restarts."""
    body = format_cursors(
        {"owner/x": datetime(2026, 5, 29, tzinfo=UTC)},
        stuck={"owner/x": {42, 99}, "owner/y": {7}},
    )
    assert "stuck:owner/x#42" in body
    assert "stuck:owner/x#99" in body
    assert "stuck:owner/y#7" in body
    # And it round-trips through the parser.
    assert parse_stuck(body) == {
        "owner/x": {42, 99},
        "owner/y": {7},
    }
    # Cursors are still parseable too — stuck rows are ignored by parse_cursors.
    assert parse_cursors(body) == {"owner/x": datetime(2026, 5, 29, tzinfo=UTC)}


@pytest.mark.asyncio
async def test_stuck_issue_still_failing_defers_new_fetch() -> None:
    """If a stuck retry still fails, the watcher skips the regular fetch
    so we don't accumulate fresh stuck entries on top of the unresolved
    ones."""
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[])
    github.get_issue = AsyncMock(
        return_value=_make_issue(
            number=42, updated_at=datetime(2026, 5, 29, tzinfo=UTC)
        )
    )

    async def still_failing(_: Any) -> None:
        raise RuntimeError("still down")

    watcher = _make_watcher(
        github=github,
        lithos=_fake_lithos_client(),
        bus=bus,
        dispatch=still_failing,
    )
    watcher._stuck_issues["agent-lore/lithos-loom"] = {42}

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    # 42 stays in the stuck set; regular fetch was skipped this poll.
    assert 42 in watcher._stuck_issues["agent-lore/lithos-loom"]
    github.list_issues_since.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_one_repo_boundary_replay_is_accepted() -> None:
    """The cursor is held at the boundary timestamp rather than nudged
    past it. PR-review finding 3 (2026-05-30): the earlier +1s nudge
    avoided a single idempotent re-fetch but dropped same-second sibling
    updates outright. Same-second drops are a correctness failure for an
    inbound mirror; idempotent replay is not. The handler short-circuits
    on the marker → open-task path so the cost is at most one extra
    Lithos round-trip per repo per poll.
    """
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    boundary = _make_issue(
        number=42, updated_at=datetime(2026, 5, 29, 19, 7, 35, tzinfo=UTC)
    )
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[boundary])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    watcher._cursors["agent-lore/lithos-loom"] = boundary.updated_at

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")

    # Cursor stays at the boundary — same-second sibling updates still
    # get pulled on the next poll.
    assert watcher._cursors["agent-lore/lithos-loom"] == boundary.updated_at


@pytest.mark.asyncio
async def test_poll_one_repo_uses_existing_cursor_as_since_param() -> None:
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(return_value=[])
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    prior = datetime(2026, 5, 29, 8, 0, 0, tzinfo=UTC)
    watcher._cursors["agent-lore/lithos-loom"] = prior

    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")
    call = github.list_issues_since.await_args
    assert call is not None
    assert call.kwargs["since"] == prior


@pytest.mark.asyncio
async def test_poll_one_repo_drops_repo_on_404() -> None:
    """D49: an unmapped/missing repo drops + logs a [Friction] line."""
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(
        side_effect=GitHubRepoNotFoundError("missing/repo")
    )
    bus = EventBus()
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    watcher._watch_list = {
        "ghost": WatchedRepo(repos=("missing/repo",)),
        "real": WatchedRepo(repos=("agent-lore/lithos-loom",)),
    }
    watcher._cursors["missing/repo"] = datetime(2026, 5, 28, tzinfo=UTC)

    await watcher._poll_one_repo(slug="ghost", repo="missing/repo")

    assert "ghost" not in watcher._watch_list
    assert "missing/repo" not in watcher._cursors
    # Sibling project untouched.
    assert "real" in watcher._watch_list


@pytest.mark.asyncio
async def test_poll_one_repo_swallows_auth_error() -> None:
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(
        side_effect=GitHubAuthError("401 Bad credentials")
    )
    bus = EventBus()
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)

    # Should not raise.
    await watcher._poll_one_repo(slug="x", repo="agent-lore/lithos-loom")
    # No events published.
    events = await _drain(bus)
    assert events == []


# ── _persist_cursors ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_cursors_writes_coord_doc_via_cas() -> None:
    lithos = _fake_lithos_client(
        write_result=WriteResult(
            status="updated",
            note=Note(
                id="coord-id",
                title="GitHub Watcher State",
                body="ignored",
                version=8,
                updated_at=None,
                tags=(),
                status="active",
                note_type="concept",
                path="projects/_lithos-loom-internal/github-watcher-state.md",
                slug="_lithos-loom-internal",
            ),
        )
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._coord_doc_id = "coord-id"
    watcher._coord_doc_version = 7
    watcher._cursors = {
        "agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    }

    await watcher._persist_cursors()

    call = lithos.note_write.await_args
    assert call.kwargs["id"] == "coord-id"
    assert call.kwargs["expected_version"] == 7
    assert "agent-lore/lithos-loom 2026-05-29T12:00:00+00:00" in call.kwargs["content"]
    # Version map advanced to what the write returned.
    assert watcher._coord_doc_version == 8


@pytest.mark.asyncio
async def test_persist_cursors_merges_pending_advances_on_version_conflict() -> None:
    """Regression for PR-review finding 3: a single version_conflict
    used to overwrite ``_cursors`` from the remote and return, dropping
    every cursor advance the current poll observed. The fix merges our
    pending cursors back over the remote view (latest wins per repo),
    then retries the write so the merged cursors actually persist.
    """
    # Remote coord doc holds an older cursor for repo A and an unrelated
    # cursor for repo B (concurrent writer landed for B).
    older_a = datetime(2026, 5, 28, tzinfo=UTC)
    other_b = datetime(2026, 5, 29, 8, 0, 0, tzinfo=UTC)
    remote_body = format_cursors({"owner/a": older_a, "owner/b": other_b})
    remote_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body=remote_body,
        version=9,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    # Our just-observed advance for A is later than remote's A; we hold
    # no opinion on B.
    fresher_a = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)

    lithos = _fake_lithos_client(note_read_return=remote_note)
    # First write: conflict. Second write: success.
    final_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=10,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos.note_write = AsyncMock(
        side_effect=[
            WriteResult(status="version_conflict", current_version=9),
            WriteResult(status="updated", note=final_note),
        ]
    )

    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._coord_doc_id = "coord-id"
    watcher._coord_doc_version = 7
    watcher._cursors = {"owner/a": fresher_a}

    await watcher._persist_cursors()

    # Second write happened (so cursors actually landed in Lithos).
    assert lithos.note_write.await_count == 2
    second = lithos.note_write.await_args_list[1].kwargs
    # Used the fresh version from the conflict response.
    assert second["expected_version"] == 9
    # Merge: our advance for A wins, remote's B is preserved.
    body_written = second["content"]
    assert f"owner/a {fresher_a.isoformat()}" in body_written
    assert f"owner/b {other_b.isoformat()}" in body_written
    # In-memory cursors reflect the merge.
    assert watcher._cursors == {"owner/a": fresher_a, "owner/b": other_b}
    assert watcher._coord_doc_version == 10


@pytest.mark.asyncio
async def test_persist_cursors_keeps_stuck_deletions_through_version_conflict() -> None:
    """PR-review finding 3 (round 6, 2026-05-30): a stuck row drained
    locally must stay deleted even when a CAS conflict reloads the
    remote stuck-set that still carries it. Without the per-number
    tombstone, the union-merge re-adds the row from the remote and
    the next write resurrects it.
    """
    T1 = datetime(2026, 5, 29, tzinfo=UTC)
    # Remote coord doc still has stuck entry that we already drained locally.
    remote_body = format_cursors({"owner/x": T1}, stuck={"owner/x": {42, 99}})
    remote_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body=remote_body,
        version=9,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    final_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=10,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(note_read_return=remote_note)
    lithos.note_write = AsyncMock(
        side_effect=[
            WriteResult(status="version_conflict", current_version=9),
            WriteResult(status="updated", note=final_note),
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._coord_doc_id = "coord-id"
    watcher._coord_doc_version = 7
    watcher._cursors = {"owner/x": T1}
    watcher._last_persisted_cursors = {"owner/x": T1}
    # We had {42, 99} stuck persisted; we just drained #42 locally,
    # leaving #99. Remote still carries both.
    watcher._stuck_issues = {"owner/x": {99}}
    watcher._last_persisted_stuck = {"owner/x": {42, 99}}

    await watcher._persist_cursors()

    body_written = lithos.note_write.await_args_list[1].kwargs["content"]
    # #42 is gone from the persisted body — the local drain survived
    # the CAS conflict.
    assert "stuck:owner/x#42" not in body_written
    # #99 is still there.
    assert "stuck:owner/x#99" in body_written
    # In-memory matches.
    assert watcher._stuck_issues == {"owner/x": {99}}


@pytest.mark.asyncio
async def test_persist_cursors_keeps_deletions_through_version_conflict() -> None:
    """PR-review finding 1 (round 5, 2026-05-30): a cursor we intend to
    delete must not silently come back when a version_conflict triggers
    reload-then-merge. Without tracking deletion tombstones, the reload
    re-populates ``_cursors`` from the remote (which still contains the
    row we wanted gone) and the next write persists the stale row.

    Scenario: in-memory has dropped repo X (operator disabled watching).
    Remote coord doc still has X→T1. The persist conflicts, reloads X
    back, merges pending (empty) — without the fix, X resurrects.
    """
    T1 = datetime(2026, 5, 28, tzinfo=UTC)
    remote_body = format_cursors({"owner/x": T1})
    remote_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body=remote_body,
        version=9,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    final_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=10,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(note_read_return=remote_note)
    lithos.note_write = AsyncMock(
        side_effect=[
            WriteResult(status="version_conflict", current_version=9),
            WriteResult(status="updated", note=final_note),
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._coord_doc_id = "coord-id"
    watcher._coord_doc_version = 7
    # Operator just disabled watching for X — in-memory is empty, but the
    # _last_persisted snapshot still carries X (it was persisted earlier).
    watcher._cursors = {}
    watcher._last_persisted_cursors = {"owner/x": T1}

    await watcher._persist_cursors()

    # Two writes: first conflicted, second succeeded with X *gone*.
    assert lithos.note_write.await_count == 2
    body_written = lithos.note_write.await_args_list[1].kwargs["content"]
    assert "owner/x" not in body_written
    # In-memory state confirms the deletion stuck.
    assert "owner/x" not in watcher._cursors


@pytest.mark.asyncio
async def test_persist_cursors_gives_up_after_max_cas_attempts() -> None:
    """Three back-to-back conflicts surface a warning and bail without
    spinning forever; the next poll will retry."""
    remote_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=9,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(note_read_return=remote_note)
    lithos.note_write = AsyncMock(
        return_value=WriteResult(status="version_conflict", current_version=9)
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._coord_doc_id = "coord-id"
    watcher._coord_doc_version = 7
    watcher._cursors = {"owner/a": datetime(2026, 5, 29, tzinfo=UTC)}

    await watcher._persist_cursors()

    # Exhausted at _MAX_COORD_DOC_CAS_ATTEMPTS=3 attempts, returns cleanly.
    assert lithos.note_write.await_count == 3


@pytest.mark.asyncio
async def test_persist_cursors_creates_doc_when_no_id_yet() -> None:
    """First-run path: no _coord_doc_id → write with path= instead of id=."""
    lithos = _fake_lithos_client(
        write_result=WriteResult(
            status="created",
            note=Note(
                id="new-id",
                title="GitHub Watcher State",
                body="",
                version=1,
                updated_at=None,
                tags=(),
                status="active",
                note_type="concept",
                path="projects/_lithos-loom-internal/github-watcher-state.md",
                slug="_lithos-loom-internal",
            ),
        )
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._cursors = {"x/y": datetime(2026, 5, 29, tzinfo=UTC)}

    await watcher._persist_cursors()

    call = lithos.note_write.await_args
    expected_path = "projects/_lithos-loom-internal/github-watcher-state.md"
    assert call.kwargs.get("id") is None
    assert call.kwargs["path"] == expected_path
    assert watcher._coord_doc_id == "new-id"
    assert watcher._coord_doc_version == 1


@pytest.mark.asyncio
async def test_persist_cursors_is_noop_when_unchanged_since_last_write() -> None:
    """Soak 2026-05-29: the watcher was re-writing the coord doc every
    poll regardless of whether any cursor advanced — Lithos version
    crept up minute by minute and fired two SSE note.updated events per
    minute for no benefit. After a successful write, a follow-up persist
    with the same cursor map must skip the write entirely.
    """
    written_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=2,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(
        write_result=WriteResult(status="updated", note=written_note)
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._coord_doc_id = "coord-id"
    watcher._coord_doc_version = 1
    watcher._cursors = {"x/y": datetime(2026, 5, 29, tzinfo=UTC)}

    # First persist writes once.
    await watcher._persist_cursors()
    assert lithos.note_write.await_count == 1

    # Second persist with the same cursor map skips the write entirely
    # — no Lithos round-trip, no version bump.
    await watcher._persist_cursors()
    assert lithos.note_write.await_count == 1


@pytest.mark.asyncio
async def test_persist_cursors_writes_empty_map_when_slug_removed() -> None:
    """PR-review finding 1 (round 4, 2026-05-30): when the last watched
    slug is disabled, the in-memory cursor map empties — but the coord
    doc still holds the prior cursor rows. Without persisting the
    empty map, a daemon restart re-loads the stale rows; a subsequent
    re-enable resumes from the stale timestamp and can miss issues
    created during the disabled window.
    """
    written_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=3,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(
        write_result=WriteResult(status="updated", note=written_note)
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._coord_doc_id = "coord-id"
    watcher._coord_doc_version = 2
    # Coord doc had a cursor; the watch list was just cleared, dropping
    # the in-memory cursor too.
    watcher._last_persisted_cursors = {"x/y": datetime(2026, 5, 29, tzinfo=UTC)}
    watcher._cursors = {}

    await watcher._persist_cursors()
    # Coord doc rewritten with the empty cursor map — stale row is gone.
    assert lithos.note_write.await_count == 1
    write_kwargs = lithos.note_write.await_args.kwargs
    assert "x/y" not in write_kwargs["content"]


@pytest.mark.asyncio
async def test_persist_cursors_writes_again_when_cursor_advances() -> None:
    """After the no-op short-circuit lands, a subsequent cursor advance
    must still trigger a write — otherwise the watcher would silently
    stop persisting after the first poll."""
    note_v2 = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=2,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    note_v3 = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body="",
        version=3,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(
        write_result=WriteResult(status="updated", note=note_v2)
    )
    lithos.note_write = AsyncMock(
        side_effect=[
            WriteResult(status="updated", note=note_v2),
            WriteResult(status="updated", note=note_v3),
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._coord_doc_id = "coord-id"
    watcher._coord_doc_version = 1
    watcher._cursors = {"x/y": datetime(2026, 5, 29, 10, tzinfo=UTC)}

    await watcher._persist_cursors()
    assert lithos.note_write.await_count == 1

    # Cursor advances → next persist actually writes.
    watcher._cursors["x/y"] = datetime(2026, 5, 29, 11, tzinfo=UTC)
    await watcher._persist_cursors()
    assert lithos.note_write.await_count == 2


@pytest.mark.asyncio
async def test_persist_cursors_skips_retry_when_conflict_resolves_to_unchanged() -> (
    None
):
    """PR-review finding (round 2 on PR #64): the no-op short-circuit
    was at function entry, OUTSIDE the CAS loop. On version_conflict
    the watcher re-reads the remote, merges, then ``continue``s back to
    the top of ``while True`` — bypassing the entry guard. If the
    remote already held the same (or newer) cursors than the watcher
    wanted to write, the merge produced no change, but the retry
    iteration wrote anyway and bumped the coord-doc version. The
    in-loop check at the top of every iteration catches this.
    """
    cursor = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    remote_body = format_cursors({"agent-lore/lithos-loom": cursor})
    remote_note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body=remote_body,
        version=9,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(note_read_return=remote_note)
    # First write: version_conflict. If the bug returns, a second write
    # would hit this side_effect list and pass.
    lithos.note_write = AsyncMock(
        side_effect=[
            WriteResult(status="version_conflict", current_version=9),
            WriteResult(status="updated", note=remote_note),
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)
    watcher._coord_doc_id = "coord-id"
    watcher._coord_doc_version = 7
    # Entry guard would not fire: empty _last_persisted_cursors != our
    # cursor map. Only the in-loop check after the merge can save us.
    watcher._cursors = {"agent-lore/lithos-loom": cursor}

    await watcher._persist_cursors()

    # Exactly one write: the conflict-then-merge collapsed our pending
    # advance into "no change vs remote", and the retry was skipped.
    assert lithos.note_write.await_count == 1


@pytest.mark.asyncio
async def test_load_cursors_marks_them_as_already_persisted() -> None:
    """A fresh load from the coord doc means the remote already holds
    what we just read — the first poll-cycle's persist must not write
    those cursors back unchanged."""
    body = format_cursors(
        {"agent-lore/lithos-loom": datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)}
    )
    note = Note(
        id="coord-id",
        title="GitHub Watcher State",
        body=body,
        version=7,
        updated_at=None,
        tags=(),
        status="active",
        note_type="concept",
        path="projects/_lithos-loom-internal/github-watcher-state.md",
        slug="_lithos-loom-internal",
    )
    lithos = _fake_lithos_client(note_read_return=note)
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos)

    await watcher._load_cursors_from_coord_doc()
    # Immediate persist must be a no-op — what we'd write equals what's
    # already on disk.
    await watcher._persist_cursors()
    lithos.note_write.assert_not_called()


# ── End-to-end: bootstrap + one poll cycle ────────────────────────────


@pytest.mark.asyncio
async def test_bootstrap_loads_watch_list_and_subscribes_bus() -> None:
    bus = EventBus()
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="x", repo="agent-lore/x", watching=True),
        ],
        note_read_return=None,
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos, bus=bus)
    await watcher._bootstrap()
    assert watcher._watch_list == {"x": WatchedRepo(repos=("agent-lore/x",))}
    assert watcher._coord_doc_subscription is not None
    # Subscribed to lithos.note.* events.
    assert watcher._coord_doc_subscription.event_types == frozenset(
        {"lithos.note.created", "lithos.note.updated"}
    )


_WATCHER_LOGGER = "lithos_loom.sources.github_issue_watcher"


@pytest.mark.asyncio
async def test_bootstrap_logs_watching_count_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression for soak-time review: an operator with `enabled = true`
    and any watched project needs to see "watching N repo(s)" once at
    INFO so the daemon's state is unambiguous at startup."""
    import logging as _logging

    bus = EventBus()
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="x", repo="agent-lore/x", watching=True),
            _summary(slug="y", repo="agent-lore/y", watching=True),
        ],
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos, bus=bus)
    with caplog.at_level(_logging.INFO, logger=_WATCHER_LOGGER):
        await watcher._bootstrap()
    assert any("watching 2 repo(s)" in record.message for record in caplog.records), (
        caplog.text
    )


@pytest.mark.asyncio
async def test_bootstrap_logs_empty_watch_list_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: empty watch list at startup must surface at INFO, with
    actionable guidance — otherwise the "enabled but nothing tagged"
    state reads identically to a stuck daemon (silent for every poll
    cycle that follows)."""
    import logging as _logging

    bus = EventBus()
    lithos = _fake_lithos_client(note_list_return=[])
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos, bus=bus)
    with caplog.at_level(_logging.INFO, logger=_WATCHER_LOGGER):
        await watcher._bootstrap()
    assert any(
        "no watched repos configured" in record.message for record in caplog.records
    ), caplog.text
    # And the message names the actionable CLI command.
    assert any("add-github-repo" in record.message for record in caplog.records), (
        caplog.text
    )


@pytest.mark.asyncio
async def test_refresh_loop_reacts_to_project_doc_changes() -> None:
    """Publishing a lithos.note.updated for a project path triggers refresh."""
    bus = EventBus()
    lithos = _fake_lithos_client(
        note_list_return=[
            _summary(slug="x", repo="agent-lore/x", watching=True),
        ]
    )
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos, bus=bus)
    await watcher._bootstrap()
    # Two refresh calls so far: one at bootstrap.
    initial_count = lithos.note_list.await_count

    # Publish a relevant event.
    await bus.publish(
        Event(
            type="lithos.note.updated",
            timestamp=datetime(2026, 5, 29, tzinfo=UTC),
            payload={"id": "doc-1", "path": "projects/y/y-project-context.md"},
        )
    )
    # Drain one event by running the refresh loop until it processes one.
    import asyncio

    task = asyncio.create_task(watcher._refresh_loop())
    # Yield until the loop processes the queued event.
    for _ in range(10):
        await asyncio.sleep(0)
        if lithos.note_list.await_count > initial_count:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lithos.note_list.await_count > initial_count


@pytest.mark.asyncio
async def test_refresh_loop_ignores_unrelated_events() -> None:
    bus = EventBus()
    lithos = _fake_lithos_client()
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos, bus=bus)
    await watcher._bootstrap()
    initial_count = lithos.note_list.await_count

    # Non-projects path → no refresh.
    await bus.publish(
        Event(
            type="lithos.note.updated",
            timestamp=datetime(2026, 5, 29, tzinfo=UTC),
            payload={"id": "doc-1", "path": "notes/unrelated.md"},
        )
    )
    import asyncio

    task = asyncio.create_task(watcher._refresh_loop())
    # Give it a few ticks. If it triggers, the count would change.
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lithos.note_list.await_count == initial_count


@pytest.mark.asyncio
async def test_refresh_loop_ignores_coord_doc_writes() -> None:
    """Self-write protection: an event for the coord doc itself doesn't loop."""
    bus = EventBus()
    coord_path = "projects/_lithos-loom-internal/github-watcher-state.md"
    lithos = _fake_lithos_client()
    watcher = _make_watcher(github=_fake_github_client(), lithos=lithos, bus=bus)
    await watcher._bootstrap()
    initial_count = lithos.note_list.await_count

    await bus.publish(
        Event(
            type="lithos.note.updated",
            timestamp=datetime(2026, 5, 29, tzinfo=UTC),
            payload={"id": "coord", "path": coord_path},
        )
    )
    import asyncio

    task = asyncio.create_task(watcher._refresh_loop())
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lithos.note_list.await_count == initial_count


@pytest.mark.asyncio
async def test_poll_all_repos_iterates_watch_list() -> None:
    bus = EventBus()
    sub = bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()

    def fake_list(
        repo: str, *, since: datetime | None, state: str = "all"
    ) -> list[Issue]:
        return [_make_issue(number=1, repo=repo)]

    github.list_issues_since = AsyncMock(side_effect=fake_list)
    watcher = _make_watcher(github=github, lithos=_fake_lithos_client(), bus=bus)
    watcher._watch_list = {
        "a": WatchedRepo(repos=("owner/a",)),
        "b": WatchedRepo(repos=("owner/b",)),
    }

    await watcher._poll_all_repos()

    assert github.list_issues_since.await_count == 2
    assert sub.queue.qsize() == 2


@pytest.mark.asyncio
async def test_poll_loop_persists_cursors_after_pass() -> None:
    """After a polling pass with new issues, the coord doc gets written."""
    bus = EventBus()
    bus.subscribe(event_types=(GITHUB_ISSUE_EVENT_TYPE,), queue_size=16)
    github = _fake_github_client()
    github.list_issues_since = AsyncMock(
        return_value=[_make_issue(number=1, repo="owner/a")]
    )
    lithos = _fake_lithos_client(
        write_result=WriteResult(
            status="created",
            note=Note(
                id="new",
                title="GitHub Watcher State",
                body="",
                version=1,
                updated_at=None,
                tags=(),
                status="active",
                note_type="concept",
                path="projects/_lithos-loom-internal/github-watcher-state.md",
                slug="_lithos-loom-internal",
            ),
        )
    )

    # Make _sleep raise after the first pass so we exit the loop.
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise StopAsyncIteration

    watcher = _make_watcher(github=github, lithos=lithos, bus=bus)
    watcher._sleep = fake_sleep
    watcher._watch_list = {"a": WatchedRepo(repos=("owner/a",))}

    with pytest.raises(StopAsyncIteration):
        await watcher._poll_loop()

    # Coord doc was persisted after the pass.
    assert lithos.note_write.await_count == 1
    # The cursor was set, and the doc body reflects it.
    body = lithos.note_write.await_args.kwargs["content"]
    assert "owner/a" in body


@pytest.mark.asyncio
async def test_poll_loop_skips_cursor_write_when_no_cursors() -> None:
    """First poll with empty watch list — nothing to persist, no write."""
    bus = EventBus()
    github = _fake_github_client()
    lithos = _fake_lithos_client()

    async def fake_sleep(seconds: float) -> None:
        raise StopAsyncIteration

    watcher = _make_watcher(github=github, lithos=lithos, bus=bus)
    watcher._sleep = fake_sleep

    with pytest.raises(StopAsyncIteration):
        await watcher._poll_loop()

    lithos.note_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_loop_backs_off_on_exception() -> None:
    """A poll-cycle exception triggers exponential backoff, not source death."""
    bus = EventBus()
    github = _fake_github_client()
    lithos = _fake_lithos_client()
    # Force a crash inside the poll pass.
    lithos.note_write.side_effect = RuntimeError("boom")
    watcher = _make_watcher(github=github, lithos=lithos, bus=bus)
    watcher._watch_list = {"a": WatchedRepo(repos=("owner/a",))}
    watcher._cursors = {"owner/a": datetime(2026, 5, 29, tzinfo=UTC)}

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        # Stop after the first backoff sleep.
        if len(sleep_calls) >= 1:
            raise StopAsyncIteration

    watcher._sleep = fake_sleep

    with pytest.raises(StopAsyncIteration):
        await watcher._poll_loop()

    # The backoff sleep ran (1.0s by default).
    assert len(sleep_calls) >= 1
    assert sleep_calls[0] == pytest.approx(1.0)


# ── Edge: coord doc subscription queue size ───────────────────────────


def test_event_type_constant_is_namespaced() -> None:
    """``github.issue.seen`` is the bus contract; subscription handler binds to it."""
    assert GITHUB_ISSUE_EVENT_TYPE == "github.issue.seen"


def test_cursor_format_handles_future_timestamps() -> None:
    """No special-casing for issues from the future (clock skew) — just round-trip."""
    future = datetime(2030, 1, 1, tzinfo=UTC) + timedelta(seconds=1)
    assert parse_cursors(format_cursors({"x/y": future})) == {"x/y": future}
