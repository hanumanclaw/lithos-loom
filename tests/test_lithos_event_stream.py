"""Tests for ``lithos_loom.sources.lithos_event_stream`` (issue #8).

The event-stream source replaces the polling LithosPoller. It consumes
Lithos's ``GET /events`` SSE endpoint, enriches each slim event payload
via ``task_status``, and publishes the same ``lithos.task.*`` events
RouteRunner already consumes (so the source swap is invisible
downstream).

Tests inject a fake ``LithosClient`` and a fake ``aconnect_sse`` so the
source logic is exercised without an HTTP round trip — see
``test_lithos_client.py`` for the real client.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lithos_loom.bus import EventBus, Subscription
from lithos_loom.lithos_client import Task
from lithos_loom.sources.lithos_event_stream import LithosEventStream

# ── Test helpers ────────────────────────────────────────────────────────


def _task(
    id_: str,
    *,
    status: str = "open",
    tags: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
    claims: tuple[Mapping[str, Any], ...] = (),
    title: str = "t",
) -> Task:
    return Task(
        id=id_,
        title=title,
        status=status,
        tags=tags,
        metadata=metadata or {},
        claims=claims,
    )


class _FakeSse:
    """Minimal stand-in for ``httpx_sse.ServerSentEvent``."""

    def __init__(self, *, event: str, data: dict[str, Any], id: str = "") -> None:
        self.event = event
        self.data = json.dumps(data)
        self.id = id


class _FakeEventSource:
    """Yields a pre-scripted iterable of SSE events, optionally raising."""

    def __init__(self, script: Iterable[_FakeSse | Exception]) -> None:
        self._script = list(script)

    async def aiter_sse(self) -> AsyncIterator[_FakeSse]:
        for item in self._script:
            if isinstance(item, Exception):
                raise item
            yield item


class _FakeAconnect:
    """Async-context-manager stand-in for ``httpx_sse.aconnect_sse``.

    Records every call with the kwargs it was invoked with (so tests can
    assert on ``Last-Event-ID`` header behaviour across reconnects) and
    dequeues the next pre-scripted EventSource from ``connections``.
    Entries can be either a list of events (success) or an Exception
    (raised when entering the context).
    """

    def __init__(
        self, connections: list[list[_FakeSse | Exception] | Exception]
    ) -> None:
        self._connections = list(connections)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, client: Any, method: str, url: str, **kwargs: Any
    ) -> _FakeAconnect._Ctx:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(kwargs.get("headers") or {}),
                "params": dict(kwargs.get("params") or {}),
            }
        )
        if not self._connections:
            return _FakeAconnect._Ctx(events=[])
        nxt = self._connections.pop(0)
        if isinstance(nxt, Exception):
            return _FakeAconnect._Ctx(error=nxt)
        return _FakeAconnect._Ctx(events=nxt)

    class _Ctx:
        def __init__(
            self,
            *,
            events: list[_FakeSse | Exception] | None = None,
            error: Exception | None = None,
        ) -> None:
            self._events = events
            self._error = error

        async def __aenter__(self) -> _FakeEventSource:
            if self._error is not None:
                raise self._error
            return _FakeEventSource(self._events or [])

        async def __aexit__(self, *exc: Any) -> None:
            return None


class _FakeClient:
    """Fake ``LithosClient`` with scripted ``task_list`` responses.

    For ``status="open"`` the first call returns ``bootstrap``;
    subsequent open calls (used by ``_enrich`` to refresh the cache on
    cache miss) consume ``refresh_responses`` if provided, else return
    ``bootstrap`` again. A ``RuntimeError`` (or any exception) in
    ``refresh_responses`` is raised instead of returned.

    For ``status="completed"`` / ``status="cancelled"`` (used by
    ``_bootstrap_resolved`` when ``bootstrap_resolved_window`` is set),
    the call returns the corresponding ``bootstrap_*`` list, or empty.
    """

    def __init__(
        self,
        *,
        bootstrap: list[Task] | None = None,
        bootstrap_completed: list[Task] | None = None,
        bootstrap_cancelled: list[Task] | None = None,
        refresh_responses: list[list[Task] | Exception] | None = None,
    ) -> None:
        self._bootstrap = list(bootstrap or [])
        self._bootstrap_completed = list(bootstrap_completed or [])
        self._bootstrap_cancelled = list(bootstrap_cancelled or [])
        self._refresh_responses = list(refresh_responses or [])
        self._first_open_call = True
        self.task_list_calls: list[dict[str, Any]] = []

    async def task_list(
        self,
        *,
        status: str | None = None,
        with_claims: bool = False,
        resolved_since: datetime | None = None,
    ) -> list[Task]:
        self.task_list_calls.append(
            {
                "status": status,
                "with_claims": with_claims,
                "resolved_since": resolved_since,
            }
        )
        if status == "completed":
            return list(self._bootstrap_completed)
        if status == "cancelled":
            return list(self._bootstrap_cancelled)
        # status == "open" or None
        if self._first_open_call:
            self._first_open_call = False
            return list(self._bootstrap)
        if self._refresh_responses:
            nxt = self._refresh_responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return list(nxt)
        # No script set for refresh — return the bootstrap as a stable
        # fallback so well-known tasks remain enrichable.
        return list(self._bootstrap)


def _drain(sub: Subscription) -> list[tuple[str, dict[str, Any]]]:
    """Drain a subscription queue to (event_type, payload) tuples."""
    out: list[tuple[str, dict[str, Any]]] = []
    while not sub.queue.empty():
        ev = sub.queue.get_nowait()
        out.append((ev.type, dict(ev.payload)))
    return out


def _stream(
    *,
    client: _FakeClient,
    bus: EventBus,
    aconnect: _FakeAconnect,
    reconnect_backoff_seconds: float = 0.001,
    max_reconnect_backoff_seconds: float = 0.01,
    bootstrap_resolved_window: timedelta | None = None,
    now: datetime | None = None,
) -> LithosEventStream:
    """Build a stream with the fake aconnect injected.

    Pass ``now`` to pin the wall-clock seam used by
    ``_bootstrap_resolved`` for deterministic boundary-day assertions.
    """
    kwargs: dict[str, Any] = {
        "client": client,
        "bus": bus,
        "events_url": "http://lithos.test/events",
        "reconnect_backoff_seconds": reconnect_backoff_seconds,
        "max_reconnect_backoff_seconds": max_reconnect_backoff_seconds,
        "bootstrap_resolved_window": bootstrap_resolved_window,
        "_aconnect_sse": aconnect,
    }
    if now is not None:
        kwargs["_now_provider"] = lambda: now
    return LithosEventStream(**kwargs)


# ── Bootstrap ───────────────────────────────────────────────────────────


async def test_bootstrap_emits_created_per_open_task() -> None:
    """Cold start: snapshot via task_list → one lithos.task.created per task."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    client = _FakeClient(
        bootstrap=[_task("a"), _task("b"), _task("c")],
    )
    aconnect = _FakeAconnect(connections=[[]])  # immediate clean EOF on stream
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    types = [t for t, _ in _drain(listener)]
    # The first attempt's bootstrap publishes one created per snapshot
    # task. Reconnects that drain no SSE event (Last-Event-ID still None)
    # re-bootstrap by design — RouteRunner dedup absorbs the duplicates.
    # We only assert on the first attempt's snapshot here.
    assert types[:3] == [
        "lithos.task.created",
        "lithos.task.created",
        "lithos.task.created",
    ]
    assert client.task_list_calls
    assert client.task_list_calls[0] == {
        "status": "open",
        "with_claims": True,
        "resolved_since": None,
    }


async def test_bootstrap_payload_matches_poller_shape() -> None:
    """Bootstrap-emitted events carry the full Task payload shape.

    Same six keys the poller publishes (id, title, status, tags,
    metadata, claims) so the RouteRunner contract is preserved across
    the source swap.
    """
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    client = _FakeClient(
        bootstrap=[
            _task(
                "abc",
                tags=("trigger:test",),
                metadata={"depends_on": ["x"]},
                title="bootstrap task",
            )
        ],
    )
    aconnect = _FakeAconnect(connections=[[]])
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    drained = _drain(listener)
    # Re-bootstrap on cursorless reconnect can publish the same task
    # more than once; we only assert on the first emission's shape.
    assert drained, "bootstrap published nothing"
    _, payload = drained[0]
    assert payload == {
        "id": "abc",
        "title": "bootstrap task",
        "status": "open",
        "tags": ["trigger:test"],
        "metadata": {"depends_on": ["x"]},
        "claims": [],
        "resolved_at": None,
    }


# ── Stream translation + enrichment ─────────────────────────────────────


async def test_stream_translates_sse_event_type_to_loom_namespace() -> None:
    """Lithos's task.released SSE event → Loom's lithos.task.released bus event."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.released"])
    # Unknown task; enrichment refresh sees it via task_list.
    client = _FakeClient(
        bootstrap=[],
        refresh_responses=[[_task("r1", tags=("trigger:t",))]],
    )
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.released", data={"task_id": "r1"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    types = [t for t, _ in _drain(listener)]
    assert types == ["lithos.task.released"]


async def test_stream_enriches_payload_via_task_list_for_unknown_task() -> None:
    """SSE event for a task not seen at bootstrap → refresh via task_list.

    Regression for Copilot review #4: the previous impl used
    task_status which drops tags + metadata, so streamed events would
    publish with empty tags and never match a RouteRunner. task_list
    returns the full Task shape.
    """
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    full = _task(
        "t1",
        status="open",
        tags=("trigger:x",),
        metadata={"depends_on": []},
        title="enriched",
    )
    # bootstrap is empty; the refresh after the SSE event yields the task.
    client = _FakeClient(bootstrap=[], refresh_responses=[[full]])
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.created", data={"task_id": "t1"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    drained = _drain(listener)
    assert len(drained) == 1
    _, payload = drained[0]
    assert payload["id"] == "t1"
    assert payload["title"] == "enriched"
    assert payload["status"] == "open"
    assert payload["tags"] == ["trigger:x"]
    assert payload["metadata"] == {"depends_on": []}
    # First task_list call is bootstrap; second is the enrichment refresh.
    assert len(client.task_list_calls) == 2


async def test_stream_force_refreshes_on_task_updated_even_for_cached_task() -> None:
    """lithos#283: task.updated event payload is minimal ({task_id}
    only) and the cached Task is stale by definition for that event,
    so _enrich must always refresh — not return the cached value."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.updated"])
    # Bootstrap puts a stale snapshot in the cache.
    stale = _task("t1", status="open", tags=("trigger:x",), title="OLD title")
    fresh = _task("t1", status="open", tags=("trigger:x",), title="NEW title")
    client = _FakeClient(
        bootstrap=[stale],
        refresh_responses=[[fresh]],
    )
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.updated", data={"task_id": "t1"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    drained = _drain(listener)
    assert drained, "task.updated was not published"
    _, payload = drained[0]
    # The fresh title made it through — the refresh ran despite cache hit.
    assert payload["title"] == "NEW title", (
        f"task.updated must force-refresh; saw stale payload {payload!r}"
    )
    # Two task_list calls: bootstrap + force refresh.
    assert len(client.task_list_calls) == 2, (
        f"expected bootstrap + force refresh, got {client.task_list_calls}"
    )


async def test_stream_publishes_lithos_task_updated_for_task_updated_sse() -> None:
    """Smoke: a Lithos task.updated SSE arrives → loom publishes
    lithos.task.updated on the bus with the enriched payload."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.updated"])
    fresh = _task("u1", status="open", tags=("trigger:x",), title="refreshed")
    client = _FakeClient(bootstrap=[], refresh_responses=[[fresh]])
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.updated", data={"task_id": "u1"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    types = [t for t, _ in _drain(listener)]
    assert types == ["lithos.task.updated"]


async def test_stream_skips_task_updated_for_task_no_longer_open() -> None:
    """task.updated for a task that's not in the refreshed open list
    (deleted, or moved to terminal in the same window) is skipped — same
    fallback as the deleted-task race for created/claimed/released."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.updated"])
    # Refresh returns no tasks at all (the updated task is no longer open).
    client = _FakeClient(bootstrap=[], refresh_responses=[[]])
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.updated", data={"task_id": "gone"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _drain(listener) == []


async def test_stream_uses_cached_task_for_known_task_without_refresh() -> None:
    """Subsequent SSE events for tasks already in cache reuse the cached
    full-shape Task — no extra task_list refresh.

    This keeps the per-event cost down to one MCP call (the bootstrap)
    for the steady-state case where events arrive for tasks already
    known.
    """
    bus = EventBus()
    # Subscribe to both so we see the bootstrap-created event too.
    listener = bus.subscribe(
        event_types=["lithos.task.created", "lithos.task.released"]
    )
    full = _task("t1", status="open", tags=("trigger:x",), title="known")
    client = _FakeClient(bootstrap=[full])
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.released", data={"task_id": "t1"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    drained = _drain(listener)
    # Bootstrap created event + released event.
    assert len(drained) == 2
    assert drained[0][0] == "lithos.task.created"
    assert drained[1][0] == "lithos.task.released"
    assert drained[1][1]["tags"] == ["trigger:x"]
    # Only bootstrap task_list — no extra refresh for the known task.
    assert len(client.task_list_calls) == 1


async def test_stream_propagates_task_list_errors_so_event_is_replayed() -> None:
    """Regression for Copilot review #3: a transient task_list failure
    during enrichment must NOT acknowledge the SSE event. The error
    should propagate so the reconnect loop replays the same event with
    the unchanged Last-Event-ID.
    """
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    full = _task("t1", tags=("trigger:x",))
    # First refresh raises (transient); second succeeds with the task.
    client = _FakeClient(
        bootstrap=[],
        refresh_responses=[RuntimeError("transient blip"), [full]],
    )
    aconnect = _FakeAconnect(
        connections=[
            # First connection: yields one event, then raises (mirroring
            # what happens when _enrich raises mid-iteration).
            [_FakeSse(event="task.created", data={"task_id": "t1"}, id="evt-1")],
            # Second connection: replay yields the same event again.
            [_FakeSse(event="task.created", data={"task_id": "t1"}, id="evt-1")],
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The event was eventually published (second attempt succeeded).
    drained = _drain(listener)
    assert any(t == "lithos.task.created" for t, _ in drained)
    # Reconnect happened with no Last-Event-ID advance — the first
    # connect set Last-Event-ID="evt-1" only AFTER the publish, which
    # we never reached. So second connect must NOT carry it.
    assert "Last-Event-ID" not in aconnect.calls[1]["headers"]


async def test_stream_uses_cached_snapshot_for_terminal_event_on_known_task() -> None:
    """For terminal events on a task we knew during bootstrap, use cached snapshot.

    Lithos's task_list(status="open") won't return a task that's just
    completed, so the refresh path can't enrich. But because the task
    was open at bootstrap time, the cached entry carries the tags +
    metadata we need to route on. The SSE event's terminal status
    overrides the cached "open" so subscribers see the canonical state.
    """
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.completed"])
    known = _task("done", tags=("trigger:t",), title="finished task")
    # Bootstrap returns the known task (initially open). Refresh sees an
    # empty list (the task has since left the open set).
    client = _FakeClient(bootstrap=[known], refresh_responses=[[]])
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.completed", data={"task_id": "done"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    drained = _drain(listener)
    assert len(drained) == 1
    _, payload = drained[0]
    assert payload["id"] == "done"
    assert payload["title"] == "finished task"
    assert payload["tags"] == ["trigger:t"]
    # Status overridden to completed even though snapshot had "open" —
    # the SSE event carries the canonical terminal state.
    assert payload["status"] == "completed"


async def test_stream_skips_unknown_task_when_not_in_bootstrap_or_refresh() -> None:
    """SSE event for a task absent from cache AND refresh result is skipped."""
    bus = EventBus()
    listener = bus.subscribe(
        event_types=[
            "lithos.task.created",
            "lithos.task.updated",
            "lithos.task.claimed",
            "lithos.task.released",
            "lithos.task.completed",
            "lithos.task.cancelled",
        ]
    )
    # Empty bootstrap + empty refresh → enrichment yields nothing.
    client = _FakeClient(bootstrap=[], refresh_responses=[[]])
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.created", data={"task_id": "ghost"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _drain(listener) == []


async def test_stream_ignores_non_task_event_types() -> None:
    """Source filters by ``?types=task.*`` server-side; if a stray event leaks
    through (e.g., upstream config drift), we drop it locally rather than
    crash on the unknown shape."""
    bus = EventBus()
    listener = bus.subscribe(
        event_types=[
            "lithos.task.created",
            "lithos.task.updated",
            "lithos.task.claimed",
            "lithos.task.released",
            "lithos.task.completed",
            "lithos.task.cancelled",
        ]
    )
    client = _FakeClient(bootstrap=[])
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="note.created", data={"note_id": "n1"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _drain(listener) == []
    # And no enrichment was attempted for the non-task event (only the
    # bootstrap task_list, no follow-up refresh).
    assert len(client.task_list_calls) == 1


# ── Reconnect + replay ──────────────────────────────────────────────────


async def test_stream_reconnects_with_last_event_id_after_transient_error() -> None:
    """On disconnect, the next connect carries Last-Event-ID for ring-buffer replay."""
    bus = EventBus()
    bus.subscribe(event_types=["lithos.task.created"])  # passive consumer
    client = _FakeClient(
        bootstrap=[],
        # Two enrichment refreshes — one per SSE event.
        refresh_responses=[[_task("t1")], [_task("t2")]],
    )
    aconnect = _FakeAconnect(
        connections=[
            # First connection: one event then drops.
            [
                _FakeSse(event="task.created", data={"task_id": "t1"}, id="evt-1"),
                ConnectionError("simulated mid-stream drop"),
            ],
            # Second connection: another event, then immediate clean EOF.
            [_FakeSse(event="task.created", data={"task_id": "t2"}, id="evt-2")],
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(aconnect.calls) >= 2
    # First connect: no Last-Event-ID header.
    assert "Last-Event-ID" not in aconnect.calls[0]["headers"]
    # Second connect: Last-Event-ID set from the last successfully-processed event.
    assert aconnect.calls[1]["headers"].get("Last-Event-ID") == "evt-1"


async def test_stream_reconnect_backoff_grows_then_caps() -> None:
    """Repeated connection failures back off exponentially up to the cap.

    Captures the sleep durations the stream requests so we can assert the
    sequence without burning real wall-clock time. Polls deterministically
    until the expected number of backoff calls have been recorded rather
    than racing a fixed-duration timer (the latter was flaky on slow CI
    runners — see PR #28's first CI run).
    """
    sleep_calls: list[float] = []
    original_sleep = asyncio.sleep
    target_calls = 5

    # Awaitable that fires when the 5th backoff sleep has been recorded.
    # Set inside _record_sleep so the driver can wait deterministically
    # for the loop to make progress instead of guessing at a wall-clock
    # window.
    target_reached = asyncio.Event()

    async def _record_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        if len(sleep_calls) >= target_calls:
            target_reached.set()
        # Use a tiny real sleep so the loop keeps making progress.
        await original_sleep(0)

    bus = EventBus()
    client = _FakeClient(bootstrap=[])
    aconnect = _FakeAconnect(
        connections=[
            ConnectionError("boom 1"),
            ConnectionError("boom 2"),
            ConnectionError("boom 3"),
            ConnectionError("boom 4"),
            ConnectionError("boom 5"),
            [],  # finally a clean connection that yields nothing
        ]
    )
    source = LithosEventStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        reconnect_backoff_seconds=1.0,
        max_reconnect_backoff_seconds=4.0,
        _aconnect_sse=aconnect,
    )

    # Patch the module-level asyncio.sleep that the stream uses for backoff.
    import lithos_loom.sources.lithos_event_stream as mod

    mod_sleep_orig = mod.asyncio.sleep
    mod.asyncio.sleep = _record_sleep  # type: ignore[assignment]
    try:
        task = asyncio.create_task(source.run())
        # Wait until the loop has recorded the target number of backoff
        # calls. The outer 2.0s wall-clock cap is just a safety net —
        # locally this completes in <10ms.
        await asyncio.wait_for(target_reached.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        mod.asyncio.sleep = mod_sleep_orig  # type: ignore[assignment]

    # Doubling sequence starting at 1.0, capped at 4.0: 1, 2, 4, 4, 4.
    assert sleep_calls[:target_calls] == [1.0, 2.0, 4.0, 4.0, 4.0]


async def test_stream_cancellable_during_event_iteration() -> None:
    """``task.cancel()`` on a stream sitting in aiter_sse exits via CancelledError."""

    class _BlockingEventSource:
        async def aiter_sse(self) -> AsyncIterator[_FakeSse]:
            await asyncio.sleep(3600)  # park; cancellation should unwind
            if False:  # pragma: no cover — keeps mypy/yield-typing happy
                yield  # type: ignore[unreachable]

    class _BlockingAconnect:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, *args: Any, **kwargs: Any) -> _BlockingAconnect._Ctx:
            self.calls += 1
            return _BlockingAconnect._Ctx()

        class _Ctx:
            async def __aenter__(self) -> _BlockingEventSource:
                return _BlockingEventSource()

            async def __aexit__(self, *exc: Any) -> None:
                return None

    bus = EventBus()
    client = _FakeClient(bootstrap=[])
    aconnect = _BlockingAconnect()
    source = LithosEventStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        _aconnect_sse=aconnect,
    )

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert aconnect.calls == 1


# ── Wire-level argument contract ────────────────────────────────────────


async def test_stream_subscribes_only_to_task_event_types() -> None:
    """The source filters server-side via ``?types=task.*`` (saves bandwidth + CPU)."""
    bus = EventBus()
    client = _FakeClient(bootstrap=[])
    aconnect = _FakeAconnect(connections=[[]])
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    types_filter = aconnect.calls[0]["params"].get("types", "")
    parts = set(types_filter.split(","))
    assert parts == {
        "task.created",
        "task.updated",
        "task.claimed",
        "task.released",
        "task.completed",
        "task.cancelled",
    }
    assert aconnect.calls[0]["url"] == "http://lithos.test/events"
    assert aconnect.calls[0]["method"] == "GET"


# ── Operator-visibility logging ─────────────────────────────────────────


async def test_stream_logs_info_per_published_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each bus publish emits one INFO log naming the event type + task id.

    Operator visibility regression: without this, the source is silent
    on the success path and the operator can't tell whether the SSE
    channel is actually delivering events.
    """
    import logging

    bus = EventBus()
    bus.subscribe(event_types=["lithos.task.created"])  # passive
    client = _FakeClient(
        bootstrap=[],
        refresh_responses=[[_task("abc-123")]],
    )
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.created", data={"task_id": "abc-123"}, id="e1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    source_logger = "lithos_loom.sources.lithos_event_stream"
    with caplog.at_level(logging.INFO, logger=source_logger):
        task = asyncio.create_task(source.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    publish_logs = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "published" in r.getMessage()
    ]
    assert publish_logs, "expected at least one INFO 'published' log"
    msg = publish_logs[0].getMessage()
    assert "lithos.task.created" in msg
    assert "abc-123" in msg


# ── Reconnect-loop noise (soak regression 2026-05-24) ──────────────────


async def test_reconnect_logs_warning_without_traceback_on_transient_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient server disconnect must log at WARNING with a one-line
    message — NOT at ERROR with a full traceback.

    Soak regression: ``LithosEventStream.run`` used ``logger.exception``,
    which dumps a multi-page traceback on every retry. During a Lithos
    restart that fires every ``backoff`` seconds for the duration of
    the outage and buries the actual reconnect timeline. The retry is
    the *expected* path; reserve ERROR / traceback for genuinely
    unexpected failures.
    """
    import logging

    bus = EventBus()
    client = _FakeClient(bootstrap=[])
    # First connection raises mid-bootstrap → triggers the except clause.
    aconnect = _FakeAconnect(connections=[RuntimeError("server disconnected")])
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    source_logger = "lithos_loom.sources.lithos_event_stream"
    with caplog.at_level(logging.DEBUG, logger=source_logger):
        task = asyncio.create_task(source.run())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    retry_records = [r for r in caplog.records if "retrying after" in r.getMessage()]
    assert retry_records, "expected at least one 'retrying after' log"
    for r in retry_records:
        assert r.levelno == logging.WARNING, (
            f"reconnect log must be WARNING not {logging.getLevelName(r.levelno)} — "
            f"full tracebacks every retry overwhelm operator logs during outage"
        )
        # exc_info is what would trigger the traceback render in
        # logger.exception — must NOT be set on the WARNING path.
        assert r.exc_info is None, (
            "reconnect WARNING must not carry exc_info (would render full "
            "traceback under the default formatter, defeating the noise fix)"
        )
        msg = r.getMessage()
        assert "RuntimeError" in msg and "server disconnected" in msg, (
            f"WARNING must include exception type + message inline so the "
            f"operator can still see what broke; got: {msg!r}"
        )


# ── httpx timeout for SSE streaming ─────────────────────────────────────


def test_default_httpx_timeout_disables_read_timeout() -> None:
    """Regression for Copilot review #5: httpx's default 5s read timeout
    is shorter than Lithos's 15s keepalive interval, so an idle stream
    would disconnect every 5s and back off, missing events. The source
    must use a timeout with read disabled (or longer than the keepalive).
    """
    import httpx

    from lithos_loom.sources.lithos_event_stream import _default_httpx_timeout

    timeout = _default_httpx_timeout()
    assert isinstance(timeout, httpx.Timeout)
    # Read timeout disabled (None) → httpx never fires a read-timeout error
    # on an idle SSE stream.
    assert timeout.read is None
    # Connect/write/pool still have sensible bounds.
    assert timeout.connect is not None and timeout.connect > 0


# ── Bootstrap / SSE ordering (issues #13, #14) ──────────────────────────


async def test_sse_connect_happens_before_bootstrap_snapshot() -> None:
    """Race-window regression for #13: aconnect_sse must enter BEFORE
    task_list runs. Otherwise a state change in the snapshot-to-connect
    gap is invisible to both paths (not in snapshot, no Last-Event-ID
    yet to replay)."""
    bus = EventBus()
    bus.subscribe(event_types=["lithos.task.created"])
    client = _FakeClient(bootstrap=[_task("t1")])
    aconnect = _FakeAconnect(connections=[[]])

    # Capture how many aconnect calls had been made at the moment
    # task_list ran. If the new ordering holds, this is 1 (SSE already
    # opened); under the old ordering it would be 0.
    snapshot_state: list[int] = []
    original_task_list = client.task_list

    async def _capturing(
        *, status: str | None = None, with_claims: bool = False
    ) -> list[Task]:
        snapshot_state.append(len(aconnect.calls))
        return await original_task_list(status=status, with_claims=with_claims)

    client.task_list = _capturing  # type: ignore[assignment]
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert snapshot_state, "task_list was never called"
    assert snapshot_state[0] == 1, (
        f"expected SSE connect (len=1) before snapshot, got {snapshot_state[0]}"
    )


async def test_bootstrap_failure_triggers_reconnect_not_silent_death() -> None:
    """Regression for #14: a transient task_list failure during bootstrap
    must NOT escape run() and kill the source. The retry loop should
    back off and re-bootstrap until it succeeds."""

    class _FlakyBootstrapClient:
        """First snapshot raises; subsequent snapshots return one task."""

        def __init__(self) -> None:
            self.task_list_calls: list[dict[str, Any]] = []
            self._raised = False

        async def task_list(
            self,
            *,
            status: str | None = None,
            with_claims: bool = False,
            resolved_since: datetime | None = None,
        ) -> list[Task]:
            self.task_list_calls.append(
                {
                    "status": status,
                    "with_claims": with_claims,
                    "resolved_since": resolved_since,
                }
            )
            if not self._raised:
                self._raised = True
                raise RuntimeError("startup blip")
            return [_task("recovered", tags=("trigger:x",))]

    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    client = _FlakyBootstrapClient()
    # Both connect attempts succeed at the SSE layer; bootstrap is what
    # raises on attempt one. Second attempt's stream is empty + clean.
    aconnect = _FakeAconnect(connections=[[], []])
    source = LithosEventStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        reconnect_backoff_seconds=0.001,
        max_reconnect_backoff_seconds=0.01,
        _aconnect_sse=aconnect,
    )

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.1)
    assert not task.done(), "run() exited unexpectedly — silent death not fixed"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Bootstrap was retried after the first failure.
    assert len(client.task_list_calls) >= 2
    # Recovered task eventually published from the retried bootstrap.
    drained = _drain(listener)
    assert any(payload["id"] == "recovered" for _, payload in drained), (
        f"expected 'recovered' task to be published; got {drained}"
    )


async def test_bootstrap_skipped_on_reconnect_when_last_event_id_present() -> None:
    """Bootstrap is skipped on reconnect ONLY when we have a
    ``Last-Event-ID`` to replay from. Otherwise the dead subscription's
    buffered events would be lost. Here the first connect drains an SSE
    event (advancing the cursor) before dropping, so the reconnect can
    safely skip bootstrap and rely on server-side replay."""
    bus = EventBus()
    bus.subscribe(
        event_types=["lithos.task.created", "lithos.task.claimed"],
    )
    # Bootstrap returns one task; the SSE then delivers a claimed event
    # for it (advancing Last-Event-ID to "evt-1") before the connection
    # drops. Reconnect must skip bootstrap because the cursor is set.
    client = _FakeClient(bootstrap=[_task("a", tags=("trigger:x",))])
    aconnect = _FakeAconnect(
        connections=[
            [
                _FakeSse(event="task.claimed", data={"task_id": "a"}, id="evt-1"),
                ConnectionError("drop after first event"),
            ],
            [],
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Snapshot exactly once; the reconnect skipped bootstrap because
    # Last-Event-ID was set.
    snapshot_calls = [c for c in client.task_list_calls if c["with_claims"] is True]
    assert len(snapshot_calls) == 1, (
        f"expected 1 bootstrap snapshot, got {len(snapshot_calls)}: "
        f"{client.task_list_calls}"
    )
    # And the reconnect actually happened with the cursor.
    assert len(aconnect.calls) >= 2
    assert aconnect.calls[1]["headers"].get("Last-Event-ID") == "evt-1"


async def test_bootstrap_re_runs_on_reconnect_when_no_event_id_drained() -> None:
    """Regression: if bootstrap succeeds but the connection drops before
    any SSE event with an id is drained, the next attempt has neither a
    fresh snapshot nor a resume cursor. We MUST re-bootstrap so that
    events buffered on the lost subscription aren't silently dropped.
    ``RouteRunner._processed_tasks`` absorbs the resulting duplicates."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    client = _FakeClient(bootstrap=[_task("a", tags=("trigger:x",))])
    # First connect: bootstrap succeeds, then immediate drop with no
    # SSE event drained → Last-Event-ID stays None.
    # Second connect: clean empty stream so the test can shut down.
    aconnect = _FakeAconnect(
        connections=[
            [ConnectionError("drop before any event drained")],
            [],
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Bootstrap re-ran on the reconnect because there was no cursor.
    snapshot_calls = [c for c in client.task_list_calls if c["with_claims"] is True]
    assert len(snapshot_calls) >= 2, (
        f"expected re-bootstrap on cursor-less reconnect, got "
        f"{len(snapshot_calls)} snapshot calls: {client.task_list_calls}"
    )
    # And the reconnect went out with NO Last-Event-ID header (since
    # none was ever drained), confirming the "cursor missing" path.
    assert "Last-Event-ID" not in aconnect.calls[1]["headers"]
    # Both bootstraps published the same task → at least two created
    # events on the bus (RouteRunner dedup absorbs at the subscriber
    # level; the source's job is just to publish what it sees).
    created = [
        payload for evt, payload in _drain(listener) if evt == "lithos.task.created"
    ]
    assert len(created) >= 2, f"expected duplicate created events, got {created}"
    assert all(p["id"] == "a" for p in created)


async def test_bootstrap_events_count_toward_backoff_reset() -> None:
    """Events published during bootstrap count as progress: if bootstrap
    publishes N events and the stream then immediately errors, the next
    sleep must use the BASE backoff (not a doubled one). Otherwise a
    flaky SSE channel would let backoff ratchet to max while bootstrap
    keeps succeeding."""
    sleep_calls: list[float] = []
    original_sleep = asyncio.sleep

    async def _record_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        await original_sleep(0)

    bus = EventBus()
    bus.subscribe(event_types=["lithos.task.created"])
    # Bootstrap publishes 2 events. First connect: stream raises
    # immediately after bootstrap. Following connects: clean and empty.
    client = _FakeClient(bootstrap=[_task("a"), _task("b")])
    aconnect = _FakeAconnect(
        connections=[
            [ConnectionError("immediate drop")],
            [],
            [],
        ]
    )
    source = LithosEventStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        reconnect_backoff_seconds=1.0,
        max_reconnect_backoff_seconds=4.0,
        _aconnect_sse=aconnect,
    )

    import lithos_loom.sources.lithos_event_stream as mod

    mod_sleep_orig = mod.asyncio.sleep
    mod.asyncio.sleep = _record_sleep  # type: ignore[assignment]
    try:
        task = asyncio.create_task(source.run())
        await original_sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        mod.asyncio.sleep = mod_sleep_orig  # type: ignore[assignment]

    # First iteration: bootstrap published 2 events → events_this_attempt=2
    # even though the stream then raised. Backoff stays at the base.
    assert sleep_calls, "no sleeps captured"
    assert sleep_calls[0] == 1.0, (
        f"expected base backoff after bootstrap progress; got {sleep_calls[0]}"
    )


async def test_filtered_sse_frames_do_not_count_toward_backoff_reset() -> None:
    """Frames we filter (non-task event type) do NOT count as published
    events. A stream delivering only noise should be allowed to grow
    backoff rather than spin at the base interval.

    Regression for Copilot review on #15: ``_events_this_attempt`` was
    previously incremented per SSE frame, not per actual bus publish."""
    sleep_calls: list[float] = []
    original_sleep = asyncio.sleep

    async def _record_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        await original_sleep(0)

    bus = EventBus()
    bus.subscribe(event_types=["lithos.task.created"])
    # Empty bootstrap (0 publishes) + a non-task SSE frame (filtered
    # → 0 publishes) then immediate clean EOF. After this attempt
    # ends, events_this_attempt should be 0 and backoff should DOUBLE.
    client = _FakeClient(bootstrap=[])
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="note.created", data={"note_id": "n1"}, id="evt-1")],
            [],
            [],
        ]
    )
    source = LithosEventStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        reconnect_backoff_seconds=1.0,
        max_reconnect_backoff_seconds=4.0,
        _aconnect_sse=aconnect,
    )

    import lithos_loom.sources.lithos_event_stream as mod

    mod_sleep_orig = mod.asyncio.sleep
    mod.asyncio.sleep = _record_sleep  # type: ignore[assignment]
    try:
        task = asyncio.create_task(source.run())
        await original_sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        mod.asyncio.sleep = mod_sleep_orig  # type: ignore[assignment]

    # First iteration produced ZERO bus publishes (bootstrap was empty,
    # the SSE frame was a filtered non-task event). Backoff doubles
    # rather than resetting.
    assert sleep_calls, "no sleeps captured"
    assert sleep_calls[:2] == [1.0, 2.0], (
        f"expected doubling backoff with no publishes; got {sleep_calls[:2]}"
    )


async def test_buffered_sse_events_during_bootstrap_drain_after() -> None:
    """End-to-end #13 confidence: an SSE event that arrived during the
    snapshot window is drained after bootstrap. The source publishes
    BOTH the bootstrap event AND the buffered SSE event for the same
    task id; RouteRunner's _processed_tasks absorbs the duplicate."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created", "lithos.task.claimed"])
    bootstrap_task = _task("t1", tags=("trigger:x",), title="bootstrapped")
    client = _FakeClient(bootstrap=[bootstrap_task])
    # SSE delivers a task.claimed for the same task — simulates a state
    # change that occurred during the bootstrap window and was buffered
    # server-side.
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.claimed", data={"task_id": "t1"}, id="evt-1")]
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    drained = _drain(listener)
    types = [t for t, _ in drained]
    # Bootstrap emits created; SSE drain then emits claimed — both reach
    # the bus, in that order.
    assert types == ["lithos.task.created", "lithos.task.claimed"], (
        f"expected created→claimed for t1; got {types}"
    )
    assert drained[1][1]["id"] == "t1"
    assert drained[1][1]["tags"] == ["trigger:x"]


async def test_stream_passes_timeout_to_httpx_client_factory() -> None:
    """The configured timeout is passed to the httpx.AsyncClient factory."""
    import httpx

    bus = EventBus()
    client = _FakeClient(bootstrap=[])
    aconnect = _FakeAconnect(connections=[[]])
    factory_calls: list[dict[str, Any]] = []

    class _SpyClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            factory_calls.append(kwargs)

        async def __aenter__(self) -> _SpyClient:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    custom_timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
    source = LithosEventStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        reconnect_backoff_seconds=0.001,
        max_reconnect_backoff_seconds=0.01,
        _aconnect_sse=aconnect,
        _httpx_client_factory=_SpyClient,
        _httpx_timeout=custom_timeout,
    )

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert factory_calls, "expected at least one client factory invocation"
    assert factory_calls[0].get("timeout") is custom_timeout


# ── Bootstrap-resolved (PR #21 review issue 1) ─────────────────────────


def _resolved_task(
    id: str,
    *,
    status: str = "completed",
    resolved_at: datetime | None = None,
) -> Task:
    return Task(
        id=id,
        title=f"task {id}",
        status=status,
        tags=(),
        metadata={},
        claims=(),
        resolved_at=resolved_at,
    )


async def test_bootstrap_resolved_publishes_completed_and_cancelled() -> None:
    """When bootstrap_resolved_window is set, the source fetches
    completed + cancelled tasks (via the server-side resolved_since
    filter — lithos#286) and publishes each as the appropriate
    terminal-event type. This is the restart-recovery path that lets
    the obsidian-projection handler rehydrate its TTL lingering
    window."""
    bus = EventBus()
    sub = bus.subscribe(
        event_types=[
            "lithos.task.created",
            "lithos.task.completed",
            "lithos.task.cancelled",
        ]
    )
    now = datetime.now(UTC)
    recent = now - timedelta(days=2)
    completed_in_window = _resolved_task("c-in", status="completed", resolved_at=recent)
    cancelled_in_window = _resolved_task("x-in", status="cancelled", resolved_at=recent)
    client = _FakeClient(
        bootstrap=[],
        bootstrap_completed=[completed_in_window],
        bootstrap_cancelled=[cancelled_in_window],
    )
    aconnect = _FakeAconnect(connections=[[]])
    source = _stream(
        client=client,
        bus=bus,
        aconnect=aconnect,
        bootstrap_resolved_window=timedelta(days=7),
    )
    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    events = _drain(sub)
    types = [t for t, _ in events]
    assert "lithos.task.completed" in types
    assert "lithos.task.cancelled" in types
    # All three list calls happened: open + completed + cancelled.
    statuses = [call["status"] for call in client.task_list_calls]
    assert statuses[:3] == ["open", "completed", "cancelled"]


async def test_bootstrap_resolved_publishes_everything_server_returns() -> None:
    """lithos#286 moved the cutoff filter server-side. Loom no longer
    double-filters — whatever the server returns is published verbatim.
    This locks in the contract: a future Lithos change to the SQL
    predicate doesn't need a parallel loom-side change."""
    bus = EventBus()
    sub = bus.subscribe(event_types=["lithos.task.completed"])
    # Two tasks the server chose to return — loom doesn't second-guess.
    client = _FakeClient(
        bootstrap=[],
        bootstrap_completed=[
            _resolved_task("a", resolved_at=datetime.now(UTC) - timedelta(days=1)),
            _resolved_task("b", resolved_at=datetime.now(UTC) - timedelta(days=2)),
        ],
    )
    aconnect = _FakeAconnect(connections=[[]])
    source = _stream(
        client=client,
        bus=bus,
        aconnect=aconnect,
        bootstrap_resolved_window=timedelta(days=7),
    )
    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    ids = sorted({payload["id"] for _, payload in _drain(sub)})
    assert ids == ["a", "b"]


async def test_bootstrap_resolved_passes_resolved_since_to_server() -> None:
    """The server-side filter only works if loom actually sends the
    cutoff. Assert that resolved_since is present on both the completed
    and cancelled fetches."""
    bus = EventBus()
    client = _FakeClient(bootstrap=[])
    aconnect = _FakeAconnect(connections=[[]])
    source = _stream(
        client=client,
        bus=bus,
        aconnect=aconnect,
        bootstrap_resolved_window=timedelta(days=7),
    )
    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    resolved_calls = [
        c for c in client.task_list_calls if c["status"] in ("completed", "cancelled")
    ]
    assert len(resolved_calls) >= 2
    for call in resolved_calls:
        assert call["resolved_since"] is not None
        assert isinstance(call["resolved_since"], datetime)
        # tz-aware — loom must not send naive datetimes (Lithos expects
        # ISO-8601 with offset; naive would render ambiguous).
        assert call["resolved_since"].tzinfo is not None


async def test_bootstrap_resolved_skipped_when_window_is_none() -> None:
    """Default (no bootstrap_resolved_window) means open-only bootstrap
    — route-runner and other source consumers don't pay for an unused
    over-fetch."""
    bus = EventBus()
    sub = bus.subscribe(
        event_types=[
            "lithos.task.created",
            "lithos.task.completed",
            "lithos.task.cancelled",
        ]
    )
    client = _FakeClient(
        bootstrap=[],
        bootstrap_completed=[_resolved_task("c1", resolved_at=datetime.now(UTC))],
    )
    aconnect = _FakeAconnect(connections=[[]])
    source = _stream(
        client=client, bus=bus, aconnect=aconnect, bootstrap_resolved_window=None
    )
    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _drain(sub) == []
    statuses = [call["status"] for call in client.task_list_calls]
    assert "completed" not in statuses
    assert "cancelled" not in statuses


async def test_bootstrap_resolved_cutoff_is_local_midnight_on_boundary_date() -> None:
    """The cutoff sent to the server is local midnight on the boundary
    date — ``today - window`` rendered at 00:00 local. This matches
    ``_evict_expired``'s ``resolved_at.astimezone().date() >= today - ttl_days``
    semantic exactly, so a task resolved at any time on the boundary
    local-date survives both the live walk and the bootstrap recovery
    (PR #21 review #2 preserved through the server-side move)."""
    bus = EventBus()
    client = _FakeClient(bootstrap=[])
    aconnect = _FakeAconnect(connections=[[]])
    # Pin "now" to 2026-05-21 18:00 UTC. With a 7-day window the
    # boundary local-date is 2026-05-14; cutoff should be local
    # midnight on that date.
    now = datetime(2026, 5, 21, 18, 0, 0, tzinfo=UTC)
    source = _stream(
        client=client,
        bus=bus,
        aconnect=aconnect,
        bootstrap_resolved_window=timedelta(days=7),
        now=now,
    )
    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    resolved_call = next(
        c for c in client.task_list_calls if c["status"] == "completed"
    )
    cutoff = resolved_call["resolved_since"]
    assert isinstance(cutoff, datetime)
    expected_boundary_date = now.astimezone().date() - timedelta(days=7)
    # Local midnight on the boundary date.
    assert cutoff.astimezone().date() == expected_boundary_date
    assert cutoff.astimezone().hour == 0
    assert cutoff.astimezone().minute == 0
    assert cutoff.astimezone().second == 0
    assert cutoff.tzinfo is not None


# ── Cursor persistence (Last-Event-ID across restarts) ─────────────────


async def test_cursor_store_persists_last_event_id(tmp_path: Any) -> None:
    """When a CursorStore is wired, the stream saves the cursor after
    each SSE event drain and a fresh stream instance on the same store
    resumes from that cursor."""
    from lithos_loom.cursor_store import CursorStore

    store_path = tmp_path / "sse_cursors.json"
    store = CursorStore(store_path)

    bus = EventBus()
    bus.subscribe(event_types=["lithos.task.created"])
    client = _FakeClient(
        bootstrap=[],
        refresh_responses=[[_task("t1")]],
    )
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.created", data={"task_id": "t1"}, id="evt-42")],
        ]
    )
    source = LithosEventStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        reconnect_backoff_seconds=0.001,
        max_reconnect_backoff_seconds=0.01,
        _aconnect_sse=aconnect,
        cursor_store=store,
        cursor_name="task-events",
    )

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The cursor was persisted.
    assert store.get("task-events") == "evt-42"

    # A new store on the same file recovers the cursor.
    store2 = CursorStore(store_path)
    assert store2.get("task-events") == "evt-42"


async def test_cursor_store_loaded_at_construction(tmp_path: Any) -> None:
    """A stream constructed with a pre-populated CursorStore sends the
    persisted Last-Event-ID on its first connect (bootstrap still runs —
    see the note below)."""
    from lithos_loom.cursor_store import CursorStore

    store_path = tmp_path / "sse_cursors.json"
    # Pre-seed a cursor.
    store = CursorStore(store_path)
    store.save("task-events", "evt-99")

    bus = EventBus()
    bus.subscribe(event_types=["lithos.task.created"])
    client = _FakeClient(bootstrap=[_task("a")])
    aconnect = _FakeAconnect(connections=[[]])

    source = LithosEventStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        reconnect_backoff_seconds=0.001,
        max_reconnect_backoff_seconds=0.01,
        _aconnect_sse=aconnect,
        cursor_store=store,
        cursor_name="task-events",
    )

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # First connect should carry the persisted Last-Event-ID.
    assert aconnect.calls[0]["headers"].get("Last-Event-ID") == "evt-99"

    # A persisted cursor does NOT skip bootstrap. The gate is
    # `not self._bootstrapped or self._last_event_id is None`, and
    # `_bootstrapped` starts False, so the first connect still bootstraps.
    # That is deliberate: the cursor resumes the SSE stream mid-ring-buffer,
    # while bootstrap covers any open tasks created while we were down — the
    # overlap is deduped downstream (RouteRunner._processed_tasks).


async def test_stream_without_cursor_store_still_works(tmp_path: Any) -> None:
    """Omitting cursor_store preserves the original in-memory-only
    behaviour — no files are written."""
    bus = EventBus()
    bus.subscribe(event_types=["lithos.task.created"])
    client = _FakeClient(
        bootstrap=[],
        refresh_responses=[[_task("t1")]],
    )
    aconnect = _FakeAconnect(
        connections=[
            [_FakeSse(event="task.created", data={"task_id": "t1"}, id="evt-1")],
        ]
    )
    source = _stream(client=client, bus=bus, aconnect=aconnect)

    task = asyncio.create_task(source.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # No cursor file should exist anywhere in tmp_path.
    assert not list(tmp_path.rglob("sse_cursors.json"))
