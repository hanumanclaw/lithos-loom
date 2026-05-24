"""Coordination state shared between the obsidian-projection writer and
the obsidian-fs-watcher source (Slice 2 US23).

The fs watcher and projection live in the same subprocess (the
``obsidian-sync`` child). The projection writes ``_lithos/tasks.md``;
the watcher polls the same file. Without coordination, every projection
write would trip the watcher and emit a spurious
``obsidian.task.status_changed`` event that the status-transition
subscription would then echo back to Lithos — the feedback loop US23
explicitly forbids.

This module is the coordination seam: a single :class:`ProjectionSyncState`
instance is constructed by the child and handed to both sides. The
projection updates it *before* committing each write; the watcher reads
it on every poll and short-circuits when the on-disk content matches the
projection's last known emission.

Three pieces of state matter:

* ``last_written_hash`` — SHA-256 of the projection's most recent
  successful write. Lets the watcher cheaply skip the parse step when
  the file content is byte-identical to what the projection just wrote
  (the common case immediately after any Lithos event).
* ``task_status_markers`` — per-task ``[ ]/[x]/[-]`` checkbox marker
  the projection most recently emitted. Lets the watcher distinguish
  user edits from projection-driven status changes on a per-task basis
  when the file content does differ (e.g. user edited an unrelated
  line, projection added a new task, etc.).
* ``task_priority_markers`` (Slice 2 US21) — per-task priority enum
  (``"highest"``/``"high"``/``"medium"``/``"low"``/``"lowest"`` or
  ``None`` for no priority) the projection most recently emitted.
  Same role for ``obsidian.task.priority_changed`` as the status map
  has for ``obsidian.task.status_changed``.

Both updates happen in :meth:`ProjectionSyncState.record_projection_write`
before the projection commits its atomic rename, so a watcher poll that
sees the new file always sees consistent state for it. Single-threaded
asyncio (no locks needed).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ProjectionSyncState"]


@dataclass
class ProjectionSyncState:
    """In-process coordination state between projection writer and fs watcher.

    Constructed by the ``obsidian-sync`` child and shared by reference
    with both the :func:`~lithos_loom.subscriptions._obsidian_projection.make_handler`
    handler and the :class:`~lithos_loom.sources.obsidian_fs_watcher.ObsidianFsWatcher`
    source. Not thread-safe; mutated only on the event loop.
    """

    last_written_hash: bytes | None = None
    """SHA-256 of the projection's most recent successful write. ``None``
    before the projection has ever written. The fs watcher compares the
    current on-disk hash against this to short-circuit the parse step
    when the file is byte-identical to the projection's last emission."""

    task_status_markers: dict[str, str] = field(default_factory=dict)
    """Per-task ``[ ]/[x]/[-]`` marker the projection most recently
    emitted, keyed by Lithos task id. The fs watcher consults this when
    deciding whether a parsed status came from itself (matches the
    marker → projection-driven, suppress) or a user edit (differs →
    real change, emit ``obsidian.task.status_changed``).

    Tasks dropped from the projection (e.g. completed-and-TTL-expired,
    no-longer-actionable) are removed from this dict so re-additions
    later don't trip on stale markers."""

    task_priority_markers: dict[str, str | None] = field(default_factory=dict)
    """Per-task priority enum (``highest``/``high``/``medium``/
    ``low``/``lowest``) or ``None`` the projection most recently
    emitted, keyed by Lithos task id. Same role for the
    ``obsidian.task.priority_changed`` event (Slice 2 US21) as
    ``task_status_markers`` plays for ``status_changed``. ``None``
    means "task is open but has no priority"; a key being absent
    means the projection has never written that task. Resolved tasks
    are not added here — the renderer drops priority on resolved
    lines, so there's no projection baseline to compare against."""

    task_due_date_markers: dict[str, str | None] = field(default_factory=dict)
    """Per-task due date (``YYYY-MM-DD`` string, matching what the
    renderer emits in the ``📅`` marker) or ``None`` the projection
    most recently emitted. Same role for the
    ``obsidian.task.due_date_changed`` event (Slice 3 round-trip) as
    ``task_status_markers`` plays for ``status_changed``. ``None``
    means "task is open but has no due date on the projected line";
    a key being absent means the projection has never written that
    task. Resolved tasks are not added here — the renderer drops
    the due date on resolved lines."""

    write_version: int = 0
    """Monotonically incremented on each ``record_projection_write``
    call. The fs watcher snapshots this on every poll; a tick since
    last poll means the projection wrote in the meantime. Lets the
    watcher distinguish two hash-identical scenarios that look the
    same to a naive ``last_written_hash`` compare: (a) the projection
    re-rendered and committed (genuine self-write — suppress, clear
    observed markers) versus (b) the user manually reverted the file
    to whatever the projection had last written (a real user
    transition that must NOT be suppressed). Without this counter the
    flip-then-flip-back case was silently dropped."""

    note_file_hashes: dict[str, bytes] = field(default_factory=dict)
    """Per-project-context-doc **full-file** hash the projection most
    recently emitted (SHA-256 of the entire rendered output —
    frontmatter + body), keyed by Lithos doc id (Slice 4).

    Two purposes:

    * Projection self-dedup: skip the write if the freshly-rendered
      file would be byte-identical to what we last wrote. Must be
      whole-file (not body-only) because US30 requires frontmatter
      fields (``lithos_version``, ``status``, ``tags``,
      ``lithos_updated_at``) to mirror Lithos — a version bump with
      unchanged body MUST still rewrite the frontmatter, otherwise
      Slice 5's optimistic-lock contract breaks.
    * Slice 5 dir-watcher self-write suppression: the watcher
      computes the on-disk hash and compares against this; a match
      means "the projection wrote these exact bytes, suppress as
      self-write."

    Body-only hash (``compute_body_hash``) is a separate concept
    used by Slice 5's body-only diff for the D28 invariant (operator
    frontmatter edits never push). It is NOT stored here — the
    dir-watcher computes it on the fly when needed."""

    note_versions: dict[str, int] = field(default_factory=dict)
    """Per-project-context-doc ``lithos_version`` the projection most
    recently wrote into vault frontmatter (Slice 4). The Slice 5
    note-push handler reads this to provide ``expected_version`` to
    ``lithos_write`` for optimistic locking. Keyed by Lithos doc id."""

    note_body_hashes: dict[str, bytes] = field(default_factory=dict)
    """Per-project-context-doc **body-only** hash (SHA-256 of the
    Markdown body, frontmatter excluded), keyed by Lithos doc id
    (Slice 5).

    Drives the dir-watcher's body-only diff (D28 invariant: frontmatter
    edits never push). The watcher computes the on-disk body hash
    every poll and compares against this baseline:

    * Match → projection wrote this body (or operator edited only the
      frontmatter); suppress, no push.
    * Mismatch combined with a whole-file hash that does NOT match
      :attr:`note_file_hashes` → real operator body edit; emit
      ``obsidian.note.modified``.

    Distinct from :attr:`note_file_hashes` because Slice 5's
    note-push round-trip rewrites frontmatter (version bump) without
    changing body, and the projection itself rewrites frontmatter
    fields like ``lithos_updated_at`` on docs the operator didn't
    touch. Whole-file hash alone would mis-classify both as user
    body edits and trigger feedback-loop pushes."""

    note_projected_paths: dict[str, Path] = field(default_factory=dict)
    """Per-project-context-doc **absolute vault path** the projection
    most recently wrote to, keyed by Lithos doc id (Slice 4).

    Required for stale-file cleanup when a note's address changes
    while the projection's view of "where to write next" diverges
    from "where the previous file lives". Three scenarios this
    enables cleanup for, all surfaced by reviewer feedback on PR #37:

    * Path migration within ``projects/``: doc moves from
      ``projects/foo/context.md`` to ``projects/bar/context.md`` —
      unlink the old file before writing the new.
    * Tag removal: doc loses ``project-context`` tag — unlink the
      stale projection (was actionable, now isn't).
    * Path moved out of ``projects/``: doc moves to e.g.
      ``observations/...`` — unlink the stale projection.

    Without the prior path stored here we'd have no way to find the
    old file from the current event payload (Lithos sends the NEW
    path, not the OLD one). Cleared by ``forget_project_context``
    on delete + on cleanup-driven-by-filter-rejection."""

    def record_projection_write(
        self,
        *,
        content_hash: bytes,
        task_status_markers: Mapping[str, str],
        task_priority_markers: Mapping[str, str | None],
        task_due_date_markers: Mapping[str, str | None],
    ) -> None:
        """Capture the post-render state the projection is about to commit.

        Called by the projection's ``_flush`` *before* it commits the
        atomic rename, so any concurrent watcher poll that sees the new
        file content also sees the matching coordination state.

        ``task_status_markers`` / ``task_priority_markers`` /
        ``task_due_date_markers`` are each copied into fresh dicts so
        subsequent mutation of the projection's render-state dicts
        cannot silently change suppression behaviour after this point.

        ``write_version`` increments unconditionally — even
        same-content overwrites bump it, so the watcher's "did
        projection write since last poll" check stays accurate. (In
        practice ``_flush`` short-circuits on hash-match before
        calling this, so the counter only advances when content
        actually changed.)
        """
        self.last_written_hash = content_hash
        self.task_status_markers = dict(task_status_markers)
        self.task_priority_markers = dict(task_priority_markers)
        self.task_due_date_markers = dict(task_due_date_markers)
        self.write_version += 1

    def record_project_context_write(
        self,
        *,
        doc_id: str,
        file_hash: bytes,
        body_hash: bytes,
        version: int,
        projected_path: Path,
    ) -> None:
        """Capture the post-render state for a single project-context
        doc the projection is about to commit (Slice 4 + Slice 5).

        Per-doc state lives in four parallel maps keyed by doc id:
        ``note_file_hashes`` (whole-file hash — used by the projection
        for self-dedup), ``note_body_hashes`` (body-only hash — used
        by Slice 5's dir-watcher to suppress self-writes without
        false-positive matches against frontmatter-only changes),
        ``note_versions`` (the version Slice 5's note-push provides
        to ``expected_version`` for optimistic locking), and
        ``note_projected_paths`` (the absolute vault path of the
        current projection — used for stale-file cleanup on path
        migration / tag-removal / out-of-projects-move).

        Called by the project-context projection per doc, before the
        atomic rename — same ordering invariant as
        :meth:`record_projection_write` so any concurrent dir-watcher
        poll that sees the new file also sees the matching
        coordination state.

        Unlike the task projection's ``write_version`` (one counter
        shared across all tasks in a single file), per-doc projection
        is naturally file-scoped — re-rendering one doc doesn't
        invalidate the dir-watcher's view of any other doc — so no
        global counter is needed here. The dir-watcher compares
        per-file hash against the per-doc entry directly.
        """
        self.note_file_hashes[doc_id] = file_hash
        self.note_body_hashes[doc_id] = body_hash
        self.note_versions[doc_id] = version
        self.note_projected_paths[doc_id] = projected_path

    def forget_project_context(self, *, doc_id: str) -> None:
        """Drop a doc's projection state (called on
        ``lithos.note.deleted`` after the local file is removed, and
        on filter-rejection-driven cleanup after the stale file is
        unlinked).

        Keeping a stale hash here would cause the dir-watcher to
        suppress a subsequent re-creation of the same doc (e.g. if
        the operator restores it from KB, or re-adds the
        ``project-context`` tag) as a self-write. Idempotent — silent
        no-op when the id isn't tracked. Clears all four parallel
        maps in one shot."""
        self.note_file_hashes.pop(doc_id, None)
        self.note_body_hashes.pop(doc_id, None)
        self.note_versions.pop(doc_id, None)
        self.note_projected_paths.pop(doc_id, None)
