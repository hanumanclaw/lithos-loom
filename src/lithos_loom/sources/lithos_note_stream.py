"""LithosNoteStream — push-based source consuming Lithos's /events SSE for
``note.{created,updated,deleted}``.

A thin sibling of :class:`~lithos_loom.sources.lithos_event_stream.LithosEventStream`,
note-shaped. The reasoning for keeping these as separate classes rather
than parameterising one:

- Half of ``LithosEventStream``'s body is task-domain plumbing —
  ``_known_tasks`` enrichment cache, ``_bootstrap_resolved`` for the
  the TTL-lingering window, ``_with_terminal_status`` override,
  ``task_list`` refresh on every SSE event. None of that applies to
  notes (no terminal state, no claims, no resolved-window replay,
  enrichment is the subscription's job because filtering is too).
- Parameterising the existing class via strategy injection produces
  4+ callback knobs (bootstrap, enrich, publish-namespace,
  entity-id-key) on top of the shared reconnect loop — more code
  than the duplication, and more error surface (any per-domain bug
  would have to be fixed in two strategy implementations rather than
  in the smaller per-domain class).

The note source is therefore:

1. **Connect.** Open ``<events_url>?types=note.created,note.updated,note.deleted``.
2. **Bootstrap (first attempt only).** Call
   ``note_list(path_prefix="projects/", tags=["project-context"])`` and
   republish each result as ``lithos.note.created`` so the projection
   subscription re-projects vault files on daemon restart. Other
   doctypes (PRDs, ADRs) would slot in here later but ship out of
   scope for v1.
3. **Stream.** Translate each SSE frame to ``lithos.note.X`` with the
   raw ``{id, title, path}`` payload. The projection subscription
   calls ``note_read(id=...)`` itself to get the full body — keeping
   enrichment in the subscription (rather than the source) means the
   filter (path prefix + tag) runs once at the subscription boundary
   instead of being hardcoded into the source.
4. **Reconnect.** Same exponential-backoff + ``Last-Event-ID`` pattern
   as ``LithosEventStream`` — copy-pasted shape, not shared via
   inheritance, because the per-event shape divergence above is what
   makes the abstraction more painful than the duplication.

Wire two instances of ``LithosNoteStream`` only when more doctypes
appear — for v1 a single instance subscribed to all three note event
types is sufficient.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol

import httpx
from httpx_sse import aconnect_sse

from lithos_loom.bus import Event, EventBus
from lithos_loom.cursor_store import CursorStore
from lithos_loom.lithos_client import NoteSummary

__all__ = ["LithosNoteStream", "NoteStreamClient"]

logger = logging.getLogger(__name__)


_HANDLED_NOTE_EVENT_TYPES = (
    "note.created",
    "note.updated",
    "note.deleted",
)
"""Lithos-side note event types we subscribe to. Sent server-side as
``?types=``. Currently covers only project-context lifecycle; future
doc types would extend this tuple and the bootstrap query."""


class NoteStreamClient(Protocol):
    """Minimum surface the note-stream source depends on.

    Only ``note_list`` is required for bootstrap. SSE events publish
    with their raw payload — the subscription handler reaches back
    for the full body via ``note_read`` if it decides to project
    the doc, so the source does NOT depend on ``note_read``.
    """

    async def note_list(
        self,
        *,
        path_prefix: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
    ) -> list[NoteSummary]: ...


def _default_httpx_timeout() -> httpx.Timeout:
    """Mirrors :func:`lithos_event_stream._default_httpx_timeout` —
    read timeout disabled because Lithos sends keepalive comments
    on an otherwise-quiet SSE stream."""
    return httpx.Timeout(connect=10.0, read=None, write=10.0, pool=5.0)


@dataclass
class LithosNoteStream:
    client: NoteStreamClient
    bus: EventBus
    events_url: str
    reconnect_backoff_seconds: float = 1.0
    max_reconnect_backoff_seconds: float = 30.0
    bootstrap_path_prefix: str = "projects/"
    """Path prefix passed to ``note_list`` at bootstrap. ``"projects/"``
    targets the project-context projection. A future "pull-all-KB-docs"
    subscription could spawn a separate instance with ``""``."""
    bootstrap_tags: tuple[str, ...] = ("project-context",)
    """Tag filter forwarded to ``note_list`` at bootstrap. Pinned to
    ``"project-context"`` so the bootstrap pulls only the docs the
    projection actually projects (the subscription would filter the rest
    out anyway — bootstrap just saves the round-trip)."""
    bootstrap_limit: int = 100
    """Cap on bootstrap enumeration. Lithos's default page size is
    50; 100 comfortably covers the user's ~20-project working set."""
    cursor_store: CursorStore | None = None
    """Optional :class:`~lithos_loom.cursor_store.CursorStore` for
    persisting ``Last-Event-ID`` across daemon restarts. Same contract
    as :attr:`LithosEventStream.cursor_store`."""
    cursor_name: str = "note-events"
    """Key under which this stream's cursor is stored."""
    # Injection points for tests — same shape as LithosEventStream.
    _aconnect_sse: Any = field(default=aconnect_sse)
    _httpx_client_factory: Any = field(default=httpx.AsyncClient)
    _httpx_timeout: httpx.Timeout = field(default_factory=_default_httpx_timeout)
    _now_provider: Any = field(default=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self._last_event_id: str | None = (
            self.cursor_store.get(self.cursor_name)
            if self.cursor_store is not None
            else None
        )
        self._bootstrapped: bool = False
        # Same role as LithosEventStream's counter — drives the
        # backoff-reset decision when an attempt produces events.
        self._events_this_attempt: int = 0

    async def run(self) -> None:
        """Connect, bootstrap-once, then stream forever. Cancellable.

        Bootstrap sits inside the reconnect/backoff loop (same shape
        as LithosEventStream.run), so a transient ``note_list`` failure
        at startup retries with backoff instead of killing the source
        task silently.
        """
        backoff = self.reconnect_backoff_seconds
        while True:
            try:
                events_seen = await self._stream_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                events_seen = self._events_this_attempt
                # See LithosEventStream.run() for the WARNING-vs-exception
                # rationale: reconnect is the *expected* path during a
                # Lithos restart, full tracebacks every backoff cycle
                # bury the recovery timeline.
                logger.warning(
                    "LithosNoteStream: error; retrying after %.3fs: %s: %s",
                    backoff,
                    type(exc).__name__,
                    exc,
                )
            if events_seen > 0:
                backoff = self.reconnect_backoff_seconds
            await asyncio.sleep(backoff)
            if events_seen == 0:
                backoff = min(backoff * 2, self.max_reconnect_backoff_seconds)

    # ── bootstrap ────────────────────────────────────────────────────

    async def _bootstrap(self) -> int:
        """Snapshot project-context docs and publish them as
        ``lithos.note.created`` events.

        Required by the projection subscription's restart-recovery
        contract: without bootstrap, vault files would only update
        when Lithos pushed a live event, so cold restart would
        silently miss any doc that hadn't changed since the last
        SSE drain.

        Returns the number of bootstrap events published.
        ``self._bootstrapped`` flips to True only after the snapshot
        completes, so a partial-snapshot failure re-bootstraps on
        next reconnect — the projection subscription absorbs the
        duplicate ``created`` events as no-ops (same content → no
        re-write).
        """
        notes = await self.client.note_list(
            path_prefix=self.bootstrap_path_prefix,
            tags=list(self.bootstrap_tags) or None,
            limit=self.bootstrap_limit,
        )
        logger.info(
            "LithosNoteStream: bootstrapping snapshot of %d note(s) "
            "(path_prefix=%r, tags=%s)",
            len(notes),
            self.bootstrap_path_prefix,
            list(self.bootstrap_tags),
        )
        published = 0
        for note in notes:
            await self._publish(
                "lithos.note.created",
                {"id": note.id, "title": note.title, "path": note.path},
            )
            published += 1
        self._bootstrapped = True
        return published

    # ── streaming ────────────────────────────────────────────────────

    async def _stream_once(self) -> int:
        """Connect, bootstrap-if-needed inside the SSE context, then drain.

        Same subscribe-before-snapshot contract as LithosEventStream:
        opening the SSE causes the server to start buffering for
        this subscription; we take the bootstrap snapshot inside
        that context so any change between snapshot-and-drain still
        arrives via the buffered feed. Duplicate ``created`` events
        are absorbed by the projection (content hash compare).
        """
        self._events_this_attempt = 0
        headers: dict[str, str] = {}
        if self._last_event_id is not None:
            headers["Last-Event-ID"] = self._last_event_id
        params = {"types": ",".join(_HANDLED_NOTE_EVENT_TYPES)}

        bootstrap_this_attempt = not self._bootstrapped or self._last_event_id is None

        logger.info(
            "LithosNoteStream: connecting to %s (Last-Event-ID=%s, bootstrap=%s)",
            self.events_url,
            self._last_event_id or "<none>",
            bootstrap_this_attempt,
        )

        async with AsyncExitStack() as stack:
            http_client = await stack.enter_async_context(
                self._httpx_client_factory(timeout=self._httpx_timeout)
            )
            event_source = await stack.enter_async_context(
                self._aconnect_sse(
                    http_client,
                    "GET",
                    self.events_url,
                    headers=headers,
                    params=params,
                )
            )
            if bootstrap_this_attempt:
                self._events_this_attempt += await self._bootstrap()
            async for sse in event_source.aiter_sse():
                published = await self._handle_sse_event(sse)
                if sse.id:
                    self._last_event_id = sse.id
                    if self.cursor_store is not None:
                        self.cursor_store.save(self.cursor_name, sse.id)
                if published:
                    self._events_this_attempt += 1
        return self._events_this_attempt

    # ── per-event handling ───────────────────────────────────────────

    async def _handle_sse_event(self, sse: Any) -> bool:
        """Process one SSE frame. Returns True iff a bus event was published.

        Unlike LithosEventStream's task path, no enrichment happens
        here — the subscription handler does the ``note_read`` itself
        because filtering (path prefix + tag) happens at the
        subscription boundary too. Keeping enrichment in the
        subscription means the source has zero domain knowledge: it
        just translates SSE frames into bus events.
        """
        sse_id = getattr(sse, "id", "") or "<none>"
        event_type = getattr(sse, "event", "") or ""
        if event_type not in _HANDLED_NOTE_EVENT_TYPES:
            logger.debug(
                "LithosNoteStream: ignoring non-note event id=%s type=%r",
                sse_id,
                event_type,
            )
            return False

        try:
            data = json.loads(sse.data) if sse.data else {}
        except json.JSONDecodeError:
            logger.warning(
                "LithosNoteStream: malformed JSON in SSE id=%s type=%s; skipping",
                sse_id,
                event_type,
            )
            return False

        note_id = data.get("id")
        if not isinstance(note_id, str) or not note_id:
            logger.warning(
                "LithosNoteStream: SSE id=%s type=%s missing 'id'; skipping",
                sse_id,
                event_type,
            )
            return False

        # ``note.deleted`` requires ``path`` at the source boundary.
        # Created/updated subscribers recover the full doc shape via
        # ``note_read(id=...)``, but a deleted note is gone by the time
        # the event arrives — the projection's delete handler needs the
        # original path to know which ``_lithos/projects/<slug>/...``
        # file to remove. Without path, the subscriber would either
        # leak the local file or fall back to a slow scan; failing
        # closed at the source is cleaner.
        if event_type == "note.deleted":
            path = data.get("path")
            if not isinstance(path, str) or not path:
                logger.warning(
                    "LithosNoteStream: note.deleted SSE id=%s note=%s "
                    "missing 'path'; skipping (delete is non-actionable "
                    "without the original path — local file would be "
                    "stranded)",
                    sse_id,
                    note_id,
                )
                return False

        logger.debug(
            "LithosNoteStream: received SSE id=%s type=%s note=%s",
            sse_id,
            event_type,
            note_id,
        )

        loom_type = f"lithos.{event_type}"
        await self._publish(loom_type, data)
        return True

    # ── bus publish ──────────────────────────────────────────────────

    async def _publish(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """Publish a note event onto the bus.

        Payload shape mirrors the Lithos-side event payload
        (``{id, title, path}`` for created/updated; ``{id, path}`` for
        deleted). The subscription handler decides whether to enrich
        and project. Wrapped in ``MappingProxyType`` so the
        subscription can't accidentally mutate the payload that other
        subscribers may also receive.
        """
        event = Event(
            type=event_type,
            timestamp=self._now_provider(),
            payload=MappingProxyType(dict(payload)),
        )
        await self.bus.publish(event)
        logger.info(
            "LithosNoteStream: published %s for %s",
            event_type,
            payload.get("id", "<no-id>"),
        )
