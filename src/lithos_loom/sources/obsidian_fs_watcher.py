"""ObsidianFsWatcher — polling source for vault edits to ``_lithos/tasks.md``.

Watches a single projected file, polls its SHA-256 at
``poll_interval_seconds``, parses per-task ``[ ]/[x]/[-]`` markers,
compares against the projection's last known emission via
:class:`~lithos_loom.sync_state.ProjectionSyncState`, and publishes
``obsidian.task.status_changed`` events for tasks whose marker flipped
under user editing.

Why polling instead of ``watchdog``:

* Single file, human-scale edit cadence, 250ms latency budget.
* Polling is fully asyncio-native (``watchdog`` uses an OS-notify
  thread that we'd have to bridge to the event loop).
* No new runtime dependency.
* Deterministic tests — ``poll_once()`` is callable directly without
  installing OS-level fs handlers.

Self-write suppression has two layers, cheapest-first:

1. **Unchanged hash.** ``current_hash == self._last_seen_hash`` →
   no edits since last poll → return without parsing.
2. **Projection self-write.** ``current_hash ==
   self.sync_state.last_written_hash`` → the projection committed this
   exact content → update ``_last_seen_hash`` and return without
   emitting. The projection updates ``sync_state.last_written_hash``
   *before* committing the atomic rename (see
   :meth:`ProjectionSyncState.record_projection_write`), so any poll
   that sees the new file always sees the matching coordination
   state.
3. **Per-task suppression.** When the file changed AND it wasn't a
   self-write, parse the lines and emit
   ``obsidian.task.status_changed`` for each task whose parsed marker
   differs from ``sync_state.task_status_markers[task_id]``. Tasks
   the projection has never written (``projection_marker is None``)
   are ignored — they are new lines inserted by the capture-macro.

Event payload shape::

    {
        "task_id": "abc123",
        "prior": "[ ]",
        "new":   "[x]",
    }

``prior`` and ``new`` are the literal three-character checkbox forms
the projection emitted / the user typed, not their interpreted status
strings. Downstream subscriptions own the mapping
(``[x]`` → complete, ``[-]`` → cancel, ``[/]`` / ``[>]`` → no-op,
``[x]/[-]`` → ``[ ]`` → reopen request).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from lithos_loom.bus import Event, EventBus
from lithos_loom.sync_state import ProjectionSyncState

__all__ = ["EMOJI_TO_PRIORITY", "ObsidianFsWatcher", "VALID_STATUS_MARKERS"]

logger = logging.getLogger(__name__)


VALID_STATUS_MARKERS: frozenset[str] = frozenset({"[ ]", "[x]", "[-]", "[/]", "[>]"})
"""Checkbox markers the watcher recognises.

``[ ]`` open · ``[x]`` completed · ``[-]`` cancelled · ``[/]`` in
progress · ``[>]`` rescheduled. The watcher emits events for any flip
among these; the status-transition subscription decides which ones map
to Lithos calls."""


EMOJI_TO_PRIORITY: dict[str, str] = {
    "🔺": "highest",
    "⏫": "high",
    "🔼": "medium",
    "🔽": "low",
    "⏬": "lowest",
}
"""Priority emoji → enum string, inverse of the projection's
``PRIORITY_EMOJI``. The two tables are pinned to agree by an anti-drift
test in ``tests/test_obsidian_fs_watcher.py``
(``test_priority_emoji_table_matches_projection_table``); if either
changes, that test fails loudly. Public (no underscore) so the
anti-drift test can import it without reaching into private internals."""


# `- [<m>] ...` where <m> is exactly one character (single-char markers
# are the projected line shape; the regex deliberately rejects multi-char
# weirdness rather than guessing).
_LINE_RE = re.compile(r"^- \[(?P<marker>.)\] ")
_TASK_ID_RE = re.compile(r"🆔 lithos:(?P<task_id>[A-Za-z0-9_-]+)")
# Match any of the five priority emoji. The projection renders the emoji
# in the trailing-metadata zone *after* the 🆔 marker (see
# ``render.render_line``). The parser scopes search to the zone after
# ``id_match.end()`` so titles freely containing one of these emoji
# can't be misread as the task's priority. Mid-line emoji in titles are
# by design ignored — only trailing-zone metadata is authoritative.
_PRIORITY_EMOJI_RE = re.compile(r"(🔺|⏫|🔼|🔽|⏬)")
# Match the Tasks-plugin due-date marker: `📅 YYYY-MM-DD`. Same
# trailing-zone-only scoping as the priority regex above — a title
# like "Prepare 📅 2026-06-15 review notes" must NOT be misread
# as a due date. We match the canonical format only; anything else
# (e.g. `📅 next Friday`, `📅 2026-06-15T09:00Z`) is treated as
# "no date" so a malformed user edit doesn't bounce a garbage
# value back to Lithos. Tasks plugin itself only renders
# `YYYY-MM-DD` so the round-trip stays closed under valid inputs.
_DUE_DATE_RE = re.compile(r"📅 (\d{4}-\d{2}-\d{2})")


@dataclass
class ObsidianFsWatcher:
    """Polling-based filesystem source for the projected tasks file.

    Constructed by the ``obsidian-sync`` child with a bus + a shared
    :class:`ProjectionSyncState` instance also handed to the
    projection. ``run()`` loops forever; cancel the task to stop.
    """

    bus: EventBus
    tasks_path: Path
    sync_state: ProjectionSyncState
    poll_interval_seconds: float = 0.25
    _now_provider: Any = field(default=lambda: datetime.now(UTC))
    """Wall-clock seam for tests so emitted event timestamps are
    deterministic. Production callers leave at the default."""

    def __post_init__(self) -> None:
        # Seeded by the first poll (or by run()'s init read). Tracking
        # the last hash we processed lets the cheap unchanged-since-last-
        # poll path short-circuit before consulting sync_state or
        # parsing.
        self._last_seen_hash: bytes | None = None
        # Per-task marker memory layered on top of
        # ``sync_state.task_status_markers``. The sync_state map only
        # advances on projection writes — without local memory of what
        # we've already observed and emitted for, a user edit followed
        # by any subsequent file save (unrelated whitespace change,
        # edit to another line) would re-trigger layer 3 with the same
        # ``[ ] → [x]`` diff and re-emit the same transition. The
        # local map records the marker we last saw the user *commit* to
        # disk; emission compares against (and updates) this map so we
        # publish at most one event per actual transition. Cleared on
        # projection self-write — the projection's re-rendered file is
        # authoritative over any user edits sitting on top of it.
        self._observed_markers: dict[str, str] = {}
        # Parallel per-task memory for priority enums. Same role as
        # ``_observed_markers`` but for the
        # ``obsidian.task.priority_changed`` event family. ``None``
        # values are meaningful — "user committed a line with no
        # priority emoji" — so this is ``dict[str, str | None]``
        # rather than just ``dict[str, str]``.
        self._observed_priorities: dict[str, str | None] = {}
        # Parallel per-task memory for due dates. Same role as
        # ``_observed_priorities`` but for
        # ``obsidian.task.due_date_changed``. ``None`` is a meaningful
        # value here — "user committed a line with no 📅 marker" — so
        # the type is ``dict[str, str | None]``. The string values are
        # canonical ``YYYY-MM-DD`` strings the renderer emits / the
        # ``_DUE_DATE_RE`` regex matches.
        self._observed_dates: dict[str, str | None] = {}
        # Snapshot of ``sync_state.write_version`` from our last poll.
        # If it's advanced, the projection has committed a re-render
        # since we last looked. We use that signal to distinguish
        # genuine projection self-writes (file matches new
        # ``last_written_hash`` AND version advanced → suppress) from
        # user reverts that happen to match the projection's last
        # written content (version unchanged → real user transition,
        # let layer 3 emit). Without this, a flip-then-flip-back was
        # silently absorbed by the layer-2 hash compare.
        self._last_processed_write_version: int = 0

    async def run(self) -> None:
        """Poll forever. Cancellable.

        Seeds ``_last_seen_hash`` from
        ``sync_state.last_written_hash`` — i.e. what the projection
        believes is on disk — rather than re-reading disk directly.
        That closes a small startup-race window: if a user edited the
        file in the gap between projection-seed and watcher-start,
        seeding from current disk content would silently swallow that
        edit (initial hash matches the user's edited content, no
        emit). Seeding from sync_state means the first poll sees the
        user's edit as a real change and emits the expected event.
        """
        self._last_seen_hash = self.sync_state.last_written_hash
        self._last_processed_write_version = self.sync_state.write_version
        logger.info(
            "ObsidianFsWatcher: watching %s (poll=%.3fs, seeded_hash=%s, "
            "seeded_write_version=%d)",
            self.tasks_path,
            self.poll_interval_seconds,
            "<none>" if self._last_seen_hash is None else "<seeded>",
            self._last_processed_write_version,
        )
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "ObsidianFsWatcher: poll failed for %s; continuing",
                    self.tasks_path,
                )
            await asyncio.sleep(self.poll_interval_seconds)

    async def poll_once(self) -> int:
        """Read the file once, emit events for user-driven status or
        priority flips.

        Returns the total count of bus events published this poll
        (``obsidian.task.status_changed`` + ``obsidian.task.priority_changed``).
        Zero when the file is unchanged, matches the projection's
        last write, or contains only no-op flips (markers unchanged,
        unknown task ids, line not parseable).

        The file is read exactly once per call — the bytes are hashed
        for the layer-1/2 short-circuits and reused as text for
        layer-3 parsing. Reading twice would open a small TOCTOU
        window where the parsed content might disagree with the
        recorded hash on a rapidly-edited file.

        A single user save can produce BOTH a status_changed AND a
        priority_changed event for the same task (e.g. tick + change
        priority emoji in one edit). They fire as independent bus
        events; the two corresponding handlers run independently.
        """
        raw = _read_file(self.tasks_path)
        current_hash = hashlib.sha256(raw).digest() if raw is not None else None

        # Layer 1: nothing changed since last poll. Single hash compare,
        # no file re-read or parsing. The cheap steady-state path.
        if current_hash == self._last_seen_hash:
            return 0

        # Layer 2: a genuine projection self-write — projection
        # committed a re-render since our last poll AND the file
        # currently matches that write. Distinguishing on the version
        # counter (not just the hash) is what makes the flip-then-
        # flip-back case work: if the user reverts to projection-known
        # content without the projection writing in between, the
        # version is unchanged and we fall through to layer 3 so the
        # real transition is emitted.
        projection_wrote_since_last_poll = (
            self.sync_state.write_version > self._last_processed_write_version
        )
        if (
            projection_wrote_since_last_poll
            and current_hash is not None
            and current_hash == self.sync_state.last_written_hash
        ):
            logger.debug(
                "ObsidianFsWatcher: %s changed to projection-known content; "
                "suppressing self-write (write_version=%d)",
                self.tasks_path,
                self.sync_state.write_version,
            )
            # The projection's re-rendered file is authoritative over
            # any user edits we'd previously observed — drop them so
            # the next real user edit measures against the projection's
            # fresh view, not a stale user marker. Status, priority,
            # AND due-date observed-maps clear together.
            self._observed_markers.clear()
            self._observed_priorities.clear()
            self._observed_dates.clear()
            self._last_seen_hash = current_hash
            self._last_processed_write_version = self.sync_state.write_version
            return 0

        # If the projection wrote but the file doesn't match (user
        # raced in between with their own edit, or two projection
        # writes coalesced and we missed one), still drop _observed —
        # the projection's view is the new baseline — and update the
        # version cursor so we don't keep re-clearing on every poll.
        if projection_wrote_since_last_poll:
            self._observed_markers.clear()
            self._observed_priorities.clear()
            self._observed_dates.clear()
            self._last_processed_write_version = self.sync_state.write_version

        # Layer 3: real user edit. Parse + per-task transition detection.
        # The "prior" is the marker the user last committed to disk for
        # this task — falling back to the projection's view when we've
        # never seen a user edit for it. Emitting against this layered
        # baseline gives transition semantics: re-saving the same file
        # content (whitespace change, edit to a sibling line) does NOT
        # re-trigger an already-emitted transition.
        published = 0
        # Reuse the bytes we already read for hashing — avoids the
        # second disk read and the TOCTOU window it would open.
        # ``raw is None`` means missing/unreadable; nothing to parse.
        text = raw.decode("utf-8", errors="replace") if raw is not None else ""
        for task_id, marker, priority, due_date in _parse_line_markers(text):
            # ─── status diff ──────────────────────────────────────────
            prior_status = self._observed_markers.get(
                task_id, self.sync_state.task_status_markers.get(task_id)
            )
            if prior_status is None:
                # Task not in the projection's last-known render and
                # we haven't observed it before. Stale line, or a
                # capture-macro line. Suppress — we only diff
                # projection-known tasks.
                continue
            if marker != prior_status:
                await self._publish_status_change(task_id, prior_status, marker)
                self._observed_markers[task_id] = marker
                published += 1

            # ─── priority diff ────────────────────────────────────────
            # Only diff priority for projection-known tasks (same
            # ``prior_status is None`` gate above). ``None`` is a
            # meaningful value here — "no priority emoji on the
            # line" — so we use a ``KeyError``-style check via
            # explicit membership rather than ``get(..., default)``,
            # which would conflate "task absent" with "task has
            # ``priority=None``".
            if task_id in self._observed_priorities:
                prior_priority = self._observed_priorities[task_id]
            else:
                prior_priority = self.sync_state.task_priority_markers.get(task_id)
            if priority != prior_priority:
                await self._publish_priority_change(task_id, prior_priority, priority)
                self._observed_priorities[task_id] = priority
                published += 1

            # ─── due-date diff ────────────────────────────────────────
            # Same shape as the priority diff above. ``None`` is
            # meaningful ("no 📅 marker on the line"), so we use
            # explicit membership for the observed-map lookup. The
            # baseline fallback is ``sync_state.task_due_date_markers``
            # which the projection populates on each flush.
            if task_id in self._observed_dates:
                prior_due = self._observed_dates[task_id]
            else:
                prior_due = self.sync_state.task_due_date_markers.get(task_id)
            if due_date != prior_due:
                await self._publish_due_date_change(task_id, prior_due, due_date)
                self._observed_dates[task_id] = due_date
                published += 1

        self._last_seen_hash = current_hash
        return published

    async def _publish_status_change(self, task_id: str, prior: str, new: str) -> None:
        event = Event(
            type="obsidian.task.status_changed",
            timestamp=self._now_provider(),
            payload=MappingProxyType({"task_id": task_id, "prior": prior, "new": new}),
        )
        await self.bus.publish(event)
        logger.info(
            "ObsidianFsWatcher: published obsidian.task.status_changed task=%s %s→%s",
            task_id,
            prior,
            new,
        )

    async def _publish_priority_change(
        self, task_id: str, prior: str | None, new: str | None
    ) -> None:
        """Publish ``obsidian.task.priority_changed``.

        ``prior`` / ``new`` carry the canonical priority enum strings
        (``"highest"``, ``"high"``, ``"medium"``, ``"low"``,
        ``"lowest"``) or ``None`` for "no priority". The handler
        consumes enums; the emoji-to-enum translation is the
        watcher's job (see :data:`EMOJI_TO_PRIORITY`).
        """
        event = Event(
            type="obsidian.task.priority_changed",
            timestamp=self._now_provider(),
            payload=MappingProxyType({"task_id": task_id, "prior": prior, "new": new}),
        )
        await self.bus.publish(event)
        logger.info(
            "ObsidianFsWatcher: published obsidian.task.priority_changed task=%s %s→%s",
            task_id,
            prior,
            new,
        )

    async def _publish_due_date_change(
        self, task_id: str, prior: str | None, new: str | None
    ) -> None:
        """Publish ``obsidian.task.due_date_changed``.

        ``prior`` / ``new`` carry the canonical ``YYYY-MM-DD`` strings
        the renderer emits / the ``_DUE_DATE_RE`` regex matches — or
        ``None`` for "no 📅 marker on the line". The handler pushes
        the change back to Lithos as
        ``task_update(metadata={"scheduled_for": new})``.
        """
        event = Event(
            type="obsidian.task.due_date_changed",
            timestamp=self._now_provider(),
            payload=MappingProxyType({"task_id": task_id, "prior": prior, "new": new}),
        )
        await self.bus.publish(event)
        logger.info(
            "ObsidianFsWatcher: published obsidian.task.due_date_changed task=%s %s→%s",
            task_id,
            prior,
            new,
        )


# ── helpers ────────────────────────────────────────────────────────────


def _read_file(path: Path) -> bytes | None:
    """Read ``path``'s current contents, or ``None`` when absent /
    unreadable.

    Returning raw bytes (not a hash or decoded text) lets the caller
    reuse the same bytes for both hashing and parsing without a
    second disk read — eliminates the TOCTOU window where the parsed
    content could disagree with the recorded hash on a rapidly-edited
    file.
    """
    try:
        return path.read_bytes()
    except (FileNotFoundError, OSError):
        return None


def _parse_line_markers(
    text: str,
) -> Iterator[tuple[str, str, str | None, str | None]]:
    """Yield ``(task_id, status_marker, priority_enum_or_none,
    due_date_or_none)`` quadruples for every parseable task line.

    Takes already-decoded text rather than a path so the caller (see
    :meth:`ObsidianFsWatcher.poll_once`) can hash and parse the same
    bytes it read in one pass.

    Format expected (matches the projection's renderer):

        - [<m>] <title> 🆔 lithos:<id> ... [<prio>] [📅 <date>]

    where ``<prio>`` is one of the five priority emoji (or absent)
    and ``<date>`` is the YYYY-MM-DD form Tasks plugin renders. The
    priority emoji is mapped to its canonical enum string via
    :data:`EMOJI_TO_PRIORITY`; the due date is yielded verbatim as a
    string (no parse / no validation here — that's the handler's
    job). Both default to ``None`` when absent.

    The 🆔 marker is the only fixed anchor in the line, and the
    renderer always writes priority / date in the trailing-metadata
    zone *after* it. Priority and due-date regexes are scoped to
    that zone (``line[id_match.end():]``) so a title containing
    one of the priority emoji or a date-shaped string can't be
    misread as task metadata — invariant: title text may freely
    contain any character, only trailing metadata is authoritative.

    Lines that don't start with ``- [<m>] `` (header comments, blank
    lines, free-text) are skipped silently. A matching prefix without
    a ``🆔 lithos:<id>`` marker is also skipped — it's a task-shaped
    line the projection didn't write.

    Unknown checkbox markers (anything outside :data:`VALID_STATUS_MARKERS`)
    are skipped with a debug log; the user typed something we don't
    recognise, treat as no-op rather than emit a confusing event.
    """
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m is None:
            continue
        marker = f"[{m.group('marker')}]"
        if marker not in VALID_STATUS_MARKERS:
            logger.debug(
                "ObsidianFsWatcher: unknown checkbox marker %r on line %r; skipping",
                marker,
                line,
            )
            continue
        id_match = _TASK_ID_RE.search(line)
        if id_match is None:
            continue
        # Trailing metadata only — title text is whatever precedes
        # ``🆔 lithos:<id>`` and is explicitly out of scope for
        # priority / date parsing. See module-level regex comments.
        metadata_zone = line[id_match.end() :]
        prio_match = _PRIORITY_EMOJI_RE.search(metadata_zone)
        priority = EMOJI_TO_PRIORITY[prio_match.group(1)] if prio_match else None
        due_match = _DUE_DATE_RE.search(metadata_zone)
        due_date = due_match.group(1) if due_match else None
        yield id_match.group("task_id"), marker, priority, due_date
