"""Tests for ``lithos_loom.sources.lithos_note_stream`` (Slice 4 US28).

The note stream is a parallel-but-thinner sibling of LithosEventStream
(task source). It owns SSE plumbing only; enrichment lives in the
projection subscription (per D26).

Tests inject a fake ``LithosClient`` and a fake ``aconnect_sse`` to
exercise the source logic without an HTTP round trip — same shape as
``test_lithos_event_stream.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from typing import Any

import pytest

from lithos_loom.bus import EventBus, Subscription
from lithos_loom.lithos_client import NoteSummary
from lithos_loom.sources.lithos_note_stream import LithosNoteStream

# ── Test helpers ────────────────────────────────────────────────────────


def _summary(
    id_: str,
    *,
    title: str = "t",
    path: str = "",
    tags: tuple[str, ...] = ("project-context",),
    version: int = 1,
) -> NoteSummary:
    return NoteSummary(
        id=id_,
        title=title,
        version=version,
        updated_at=None,
        tags=tags,
        status="active",
        note_type="concept",
        path=path or f"projects/{id_}/context.md",
        slug=id_,
    )


class _FakeSse:
    def __init__(self, *, event: str, data: dict[str, Any], id: str = "") -> None:
        self.event = event
        self.data = json.dumps(data)
        self.id = id


class _FakeEventSource:
    def __init__(self, script: Iterable[_FakeSse | Exception]) -> None:
        self._script = list(script)

    async def aiter_sse(self) -> AsyncIterator[_FakeSse]:
        for item in self._script:
            if isinstance(item, Exception):
                raise item
            yield item


class _FakeAconnect:
    """Async-CM stand-in for ``aconnect_sse`` — same shape as in
    test_lithos_event_stream.py."""

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
    """Fake ``LithosClient`` exposing only ``note_list``.

    The note stream calls ``note_list`` once at bootstrap; re-bootstraps
    on reconnect when no Last-Event-ID has been drained. ``responses``
    is the scripted return per call (or an Exception to raise).
    """

    def __init__(
        self,
        *,
        responses: list[list[NoteSummary] | Exception] | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    async def note_list(
        self,
        *,
        path_prefix: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
    ) -> list[NoteSummary]:
        self.calls.append(
            {
                "path_prefix": path_prefix,
                "tags": list(tags) if tags else None,
                "limit": limit,
            }
        )
        if not self._responses:
            return []
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return list(nxt)


def _drain(sub: Subscription) -> list[tuple[str, dict[str, Any]]]:
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
    now: datetime | None = None,
) -> LithosNoteStream:
    kwargs: dict[str, Any] = {
        "client": client,
        "bus": bus,
        "events_url": "http://lithos.test/events",
        "reconnect_backoff_seconds": reconnect_backoff_seconds,
        "max_reconnect_backoff_seconds": max_reconnect_backoff_seconds,
        "_aconnect_sse": aconnect,
    }
    if now is not None:
        kwargs["_now_provider"] = lambda: now
    return LithosNoteStream(**kwargs)


async def _run_once(stream: LithosNoteStream, timeout: float = 0.5) -> None:
    """Run ``stream.run()`` until the scripted aconnect exhausts and
    the source loops back to sleep, then cancel."""
    task = asyncio.create_task(stream.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
        await asyncio.wait_for(task, timeout=timeout)


# ── Bootstrap ───────────────────────────────────────────────────────────


async def test_bootstrap_emits_created_per_note() -> None:
    """Cold start: snapshot via note_list → one lithos.note.created per doc."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.note.created"])
    client = _FakeClient(
        responses=[
            [_summary("doc-1"), _summary("doc-2")],
        ]
    )
    aconnect = _FakeAconnect([[]])  # connect succeeds; no SSE events
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream)

    events = _drain(listener)
    assert len(events) == 2
    assert all(t == "lithos.note.created" for t, _ in events)
    assert events[0][1]["id"] == "doc-1"
    assert events[1][1]["id"] == "doc-2"


async def test_bootstrap_payload_carries_id_title_path() -> None:
    """The bootstrap-emitted event payload matches the SSE event
    shape (``{id, title, path}``) so the projection subscription
    can't tell the difference between bootstrap and live created
    events."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.note.created"])
    client = _FakeClient(
        responses=[[_summary("doc-1", title="Loom", path="projects/loom/context.md")]]
    )
    aconnect = _FakeAconnect([[]])
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream)

    [(event_type, payload)] = _drain(listener)
    assert event_type == "lithos.note.created"
    assert payload["id"] == "doc-1"
    assert payload["title"] == "Loom"
    assert payload["path"] == "projects/loom/context.md"


async def test_bootstrap_uses_configured_path_prefix_and_tags() -> None:
    """The defaults (``projects/`` + ``project-context``) are
    forwarded to ``note_list`` so bootstrap pulls only the docs the
    projection actually projects — no wasted round-trip.

    The source re-bootstraps on every reconnect when no Last-Event-ID
    has been drained (covered separately by
    ``test_stream_re_bootstraps_when_no_event_id_drained``); we just
    assert the FIRST call's shape here."""
    bus = EventBus()
    client = _FakeClient(responses=[[]])
    aconnect = _FakeAconnect([[]])
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream)

    assert client.calls, "bootstrap should have invoked note_list at least once"
    first = client.calls[0]
    assert first["path_prefix"] == "projects/"
    assert first["tags"] == ["project-context"]


async def test_bootstrap_empty_result_does_not_publish() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.note.created"])
    client = _FakeClient(responses=[[]])
    aconnect = _FakeAconnect([[]])
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream)

    assert _drain(listener) == []


# ── Streaming SSE → bus ───────────────────────────────────────────────


async def test_stream_translates_sse_event_to_loom_namespace() -> None:
    """A ``note.updated`` SSE frame becomes a ``lithos.note.updated``
    bus event with the raw payload (no enrichment in source)."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.note.updated"])
    client = _FakeClient(responses=[[]])  # empty bootstrap
    aconnect = _FakeAconnect(
        [
            [
                _FakeSse(
                    event="note.updated",
                    data={
                        "id": "doc-1",
                        "title": "Loom",
                        "path": "projects/loom/context.md",
                    },
                    id="evt-1",
                ),
            ],
        ]
    )
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream)

    [(event_type, payload)] = _drain(listener)
    assert event_type == "lithos.note.updated"
    assert payload == {
        "id": "doc-1",
        "title": "Loom",
        "path": "projects/loom/context.md",
    }


async def test_stream_publishes_deleted_event() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.note.deleted"])
    client = _FakeClient(responses=[[]])
    aconnect = _FakeAconnect(
        [
            [
                _FakeSse(
                    event="note.deleted",
                    data={"id": "doc-1", "path": "projects/loom/context.md"},
                    id="evt-1",
                ),
            ]
        ]
    )
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream)

    [(event_type, payload)] = _drain(listener)
    assert event_type == "lithos.note.deleted"
    assert payload["id"] == "doc-1"


async def test_stream_drops_deleted_event_missing_path() -> None:
    """``note.deleted`` requires ``path`` at the source boundary
    because the projection's delete handler can't recover via
    ``note_read`` (the note is gone) and would strand the local
    file. A malformed/partial frame must be logged + dropped, not
    forwarded onto the bus where the delete becomes non-actionable.

    Created/updated tolerate missing-path because the subscriber
    can enrich via ``note_read(id=...)``; deleted is asymmetric
    because there's nothing to enrich. Failing closed at the
    source is cleaner than failing open downstream.
    """
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.note.deleted"])
    client = _FakeClient(responses=[[]])
    aconnect = _FakeAconnect(
        [
            [
                _FakeSse(
                    event="note.deleted",
                    data={"id": "doc-1"},  # no path
                    id="evt-1",
                ),
            ]
        ]
    )
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream)

    assert _drain(listener) == []


async def test_stream_drops_deleted_event_with_empty_path() -> None:
    """Same requirement as the missing-path case — an empty string
    is just as non-actionable for the delete handler."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.note.deleted"])
    client = _FakeClient(responses=[[]])
    aconnect = _FakeAconnect(
        [
            [
                _FakeSse(
                    event="note.deleted",
                    data={"id": "doc-1", "path": ""},
                    id="evt-1",
                ),
            ]
        ]
    )
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream)

    assert _drain(listener) == []


async def test_stream_publishes_created_without_path() -> None:
    """Inverse-symmetry check: created/updated DO tolerate a
    missing path because the subscriber can recover via
    ``note_read(id=...)``. Pins the asymmetry against accidental
    over-tightening of the source boundary."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.note.created"])
    client = _FakeClient(responses=[[]])
    aconnect = _FakeAconnect(
        [
            [
                _FakeSse(
                    event="note.created",
                    data={"id": "doc-1", "title": "x"},  # no path
                    id="evt-1",
                ),
            ]
        ]
    )
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream)

    [(event_type, payload)] = _drain(listener)
    assert event_type == "lithos.note.created"
    assert payload["id"] == "doc-1"


async def test_stream_subscribes_only_to_note_event_types() -> None:
    """The server-side ``?types=`` filter must list exactly the three
    note event types — anything else would either widen scope (task
    events delivered to the note stream) or narrow it (missed
    events). Lock the contract."""
    bus = EventBus()
    client = _FakeClient(responses=[[]])
    aconnect = _FakeAconnect([[]])
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream)

    assert aconnect.calls, "expected at least one connect attempt"
    # Re-bootstrap on reconnect produces multiple calls when run for
    # ~50ms with an empty stream; the per-attempt params shape is the
    # invariant we lock.
    for call in aconnect.calls:
        assert call["params"]["types"] == "note.created,note.updated,note.deleted", (
            call["params"]
        )


async def test_stream_ignores_non_note_event_types() -> None:
    """Belt-and-braces — even if the server leaks a task event onto a
    note subscription, the source filters it out at the boundary."""
    bus = EventBus()
    note_listener = bus.subscribe(event_types=["lithos.note.created"])
    task_listener = bus.subscribe(event_types=["lithos.task.created"])
    client = _FakeClient(responses=[[]])
    aconnect = _FakeAconnect(
        [
            [
                _FakeSse(
                    event="task.created",
                    data={"task_id": "t1"},
                    id="evt-1",
                ),
            ]
        ]
    )
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream)

    assert _drain(note_listener) == []
    assert _drain(task_listener) == []


async def test_stream_skips_malformed_json() -> None:
    """A frame with non-JSON data is logged + dropped — never publishes
    a degenerate event."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.note.created"])
    client = _FakeClient(responses=[[]])

    bad = _FakeSse(event="note.created", data={"id": "doc-1"}, id="evt-1")
    bad.data = "{not json"  # type: ignore[assignment]
    aconnect = _FakeAconnect([[bad]])
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream)

    assert _drain(listener) == []


async def test_stream_skips_event_without_id() -> None:
    """Missing ``id`` in the payload means there's no entity to act
    on; drop with a warning."""
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.note.created"])
    client = _FakeClient(responses=[[]])
    aconnect = _FakeAconnect(
        [[_FakeSse(event="note.created", data={"title": "x"}, id="evt-1")]]
    )
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream)

    assert _drain(listener) == []


# ── Reconnect + Last-Event-ID ─────────────────────────────────────────


async def test_stream_reconnects_with_last_event_id_after_transient_error() -> None:
    """After draining a frame and a connection drop, the next connect
    attempt sends ``Last-Event-ID`` for replay; bootstrap is NOT
    re-run because we have a resume cursor."""
    bus = EventBus()
    # No listener subscription needed — we assert on aconnect/client
    # call shape, not on bus delivery.
    client = _FakeClient(responses=[[_summary("boot-1")]])
    aconnect = _FakeAconnect(
        [
            [
                _FakeSse(
                    event="note.created",
                    data={
                        "id": "live-1",
                        "title": "x",
                        "path": "projects/x/context.md",
                    },
                    id="evt-7",
                ),
                RuntimeError("simulated drop"),
            ],
            [
                _FakeSse(
                    event="note.created",
                    data={
                        "id": "live-2",
                        "title": "y",
                        "path": "projects/y/context.md",
                    },
                    id="evt-8",
                ),
            ],
        ]
    )
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream, timeout=1.0)

    # Second connect attempt must include Last-Event-ID from the
    # last drained frame (evt-7).
    assert len(aconnect.calls) >= 2
    assert aconnect.calls[1]["headers"].get("Last-Event-ID") == "evt-7"
    # Bootstrap ran only once (first connect); second connect skipped it.
    assert len(client.calls) == 1


async def test_stream_re_bootstraps_when_no_event_id_drained() -> None:
    """If the first connection drops before any SSE event with an id
    is drained, we have no resume cursor — re-bootstrap on the next
    attempt rather than silently losing whatever was buffered on
    the dead subscription. The exact count depends on how many
    reconnect cycles fit in the test window; we just assert
    bootstrap ran more than once."""
    bus = EventBus()
    # Plenty of empty responses so the source can re-bootstrap freely.
    client = _FakeClient(responses=[[] for _ in range(10)])
    aconnect = _FakeAconnect(
        [
            RuntimeError("connection dropped immediately"),
            [],  # second attempt: empty stream
            [],  # subsequent reconnects also empty
            [],
        ]
    )
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    await _run_once(stream, timeout=1.0)

    # Bootstrap ran at least twice — once on first attempt (failed
    # connect), again on the second attempt because no Last-Event-ID
    # was drained from the first.
    assert len(client.calls) >= 2


# ── Cancellation ──────────────────────────────────────────────────────


async def test_stream_cancellable_during_event_iteration() -> None:
    bus = EventBus()
    client = _FakeClient(responses=[[]])

    async def _hang() -> AsyncIterator[_FakeSse]:
        await asyncio.sleep(10)
        yield _FakeSse(event="note.created", data={"id": "x"})

    class _HangingEventSource:
        async def aiter_sse(self) -> AsyncIterator[_FakeSse]:
            async for sse in _hang():
                yield sse

    class _HangingCtx:
        async def __aenter__(self) -> _HangingEventSource:
            return _HangingEventSource()

        async def __aexit__(self, *exc: Any) -> None:
            return None

    def _hanging_aconnect(*args: Any, **kwargs: Any) -> _HangingCtx:
        return _HangingCtx()

    stream = LithosNoteStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        reconnect_backoff_seconds=0.001,
        max_reconnect_backoff_seconds=0.01,
        _aconnect_sse=_hanging_aconnect,
    )

    task = asyncio.create_task(stream.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── Reconnect-loop noise (soak regression 2026-05-24) ──────────────────


async def test_reconnect_logs_warning_without_traceback_on_transient_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mirrors the LithosEventStream regression: a transient server
    disconnect must log at WARNING with a one-line message, not at
    ERROR with a full traceback. See LithosEventStream's matching test
    for the rationale (soak 2026-05-24 — Lithos restart firing a
    multi-page trace every backoff cycle)."""
    import logging

    bus = EventBus()
    client = _FakeClient(responses=[[]])
    # First connection raises during _stream_once → triggers except clause.
    aconnect = _FakeAconnect([RuntimeError("server disconnected")])
    stream = _stream(client=client, bus=bus, aconnect=aconnect)

    source_logger = "lithos_loom.sources.lithos_note_stream"
    with caplog.at_level(logging.DEBUG, logger=source_logger):
        task = asyncio.create_task(stream.run())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    retry_records = [r for r in caplog.records if "retrying after" in r.getMessage()]
    assert retry_records, "expected at least one 'retrying after' log"
    for r in retry_records:
        assert r.levelno == logging.WARNING, (
            f"reconnect log must be WARNING not {logging.getLevelName(r.levelno)}"
        )
        assert r.exc_info is None, (
            "reconnect WARNING must not carry exc_info (renders traceback)"
        )
        msg = r.getMessage()
        assert "RuntimeError" in msg and "server disconnected" in msg, (
            f"WARNING must include exception type + message inline; got: {msg!r}"
        )


# ── Wall clock provider ───────────────────────────────────────────────


async def test_publish_uses_now_provider_for_event_timestamp() -> None:
    """``_now_provider`` is the wall-clock seam — published event
    timestamps must come from it (not from real ``datetime.now``) so
    tests are deterministic."""
    pinned = datetime(2026, 5, 24, 14, 30, tzinfo=UTC)
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.note.created"])
    client = _FakeClient(responses=[[_summary("doc-1")]])
    aconnect = _FakeAconnect([[]])
    stream = _stream(client=client, bus=bus, aconnect=aconnect, now=pinned)

    await _run_once(stream)

    [ev] = list(listener.queue._queue)  # type: ignore[attr-defined]
    assert ev.timestamp == pinned


# ── Cursor persistence (Last-Event-ID across restarts) ─────────────────


async def test_cursor_store_persists_last_event_id(tmp_path: Any) -> None:
    """When a CursorStore is wired, the note stream saves the cursor after
    each SSE event drain and a fresh instance on the same store recovers it."""
    from lithos_loom.cursor_store import CursorStore

    store_path = tmp_path / "sse_cursors.json"
    store = CursorStore(store_path)

    bus = EventBus()
    bus.subscribe(event_types=["lithos.note.created"])
    client = _FakeClient(responses=[[]])
    aconnect = _FakeAconnect(
        connections=[
            [
                _FakeSse(
                    event="note.created",
                    data={"id": "n1", "title": "Note 1", "path": "projects/n1/ctx.md"},
                    id="evt-77",
                ),
            ],
        ]
    )
    source = LithosNoteStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        reconnect_backoff_seconds=0.001,
        max_reconnect_backoff_seconds=0.01,
        _aconnect_sse=aconnect,
        cursor_store=store,
        cursor_name="note-events",
    )

    await _run_once(source)

    # Persisted.
    assert store.get("note-events") == "evt-77"

    # Survives reload.
    store2 = CursorStore(store_path)
    assert store2.get("note-events") == "evt-77"


async def test_cursor_store_loaded_at_construction(tmp_path: Any) -> None:
    """A note stream constructed with a pre-populated CursorStore sends
    Last-Event-ID on its first connect."""
    from lithos_loom.cursor_store import CursorStore

    store_path = tmp_path / "sse_cursors.json"
    store = CursorStore(store_path)
    store.save("note-events", "evt-55")

    bus = EventBus()
    client = _FakeClient(responses=[[]])
    aconnect = _FakeAconnect(connections=[[]])

    source = LithosNoteStream(
        client=client,
        bus=bus,
        events_url="http://lithos.test/events",
        reconnect_backoff_seconds=0.001,
        max_reconnect_backoff_seconds=0.01,
        _aconnect_sse=aconnect,
        cursor_store=store,
        cursor_name="note-events",
    )

    await _run_once(source)

    # First connect should carry the persisted Last-Event-ID.
    assert aconnect.calls[0]["headers"].get("Last-Event-ID") == "evt-55"
