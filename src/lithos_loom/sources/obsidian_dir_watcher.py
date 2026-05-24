"""ObsidianDirWatcher — polling source for vault edits to project-context
docs under ``<vault>/<projects_dir>/<slug>/<filename>.md`` (Slice 5 US33).

Parallel to :class:`~lithos_loom.sources.obsidian_fs_watcher.ObsidianFsWatcher`
but per-file rather than single-file:

* Tasks live in ONE file; the single-file watcher tracks a single
  ``_last_seen_hash`` and per-task state maps.
* Project-context docs live in MANY files; this watcher walks the
  directory tree, tracks per-file hashes, and emits per-file events.

D27 explanation for why we don't parameterise the single-file watcher
instead: the shapes are different (per-file vs per-task state keying,
directory walk vs single read, body-only diff vs whole-line diff).
Bolting both shapes onto one class would couple unrelated logic — both
watchers live side-by-side in the obsidian-sync child.

D28 body-only invariant
-----------------------

Operator frontmatter edits MUST NOT push back to Lithos. The renderer
overwrites frontmatter on every projection write (``lithos_version``
bump, ``lithos_updated_at`` refresh) so any frontmatter the operator
adds locally would be silently clobbered anyway — pushing it back to
Lithos would propagate that clobber upstream. The watcher computes
:func:`~lithos_loom.render_project_context.compute_body_hash` and only
emits when the body half changes; frontmatter-only edits are absorbed
silently.

Self-write suppression
----------------------

Two layers, cheapest-first, mirroring the single-file watcher:

1. **Whole-file hash unchanged.** ``current_file_hash`` matches the
   per-file ``_last_seen_file_hashes`` entry → nothing happened since
   our last poll. The hot-path no-op.
2. **Projection self-write.** ``current_file_hash`` matches
   ``sync_state.note_file_hashes[doc_id]`` — the projection (or the
   note-push handler's post-success re-render) committed these exact
   bytes. Update local state and absorb without emitting.
3. **Body-only diff.** ``current_body_hash`` compared against the
   ``_observed_body_hashes`` entry layered over
   ``sync_state.note_body_hashes`` (the projection's view). Match →
   frontmatter-only edit, absorb. Mismatch → real body edit, emit
   ``obsidian.note.modified``.

The body-only baseline comes from sync_state by design: the projection
records body hash at write time so a watcher poll that sees the new
file also sees the matching body-hash baseline. Local in-memory
``_observed_body_hashes`` layers on top so successive polls comparing
against the same operator-edit baseline don't re-emit the same change
twice (US33: at-most-one event per actual body transition).

Event payload shape::

    {
        "lithos_id":      "abc-123",     # doc id from frontmatter
        "lithos_version": 12,             # last-known version (for optimistic locking)
        "slug":           "lithos-loom",
        "filename":       "context.md",
        "vault_path":     "/abs/path/to/file.md",
        "body":           "<title-included body string>",
    }

The note-push handler consumes this and calls
:meth:`~lithos_loom.lithos_client.LithosClient.note_write` with
``expected_version=lithos_version``.

What this watcher does NOT do
-----------------------------

* **File added** (no prior projection): logged at DEBUG and skipped.
  Operator-created project context docs go through the Slice 5
  ``project create`` CLI (PR #5 — not yet wired); a bare new file
  with a manually-typed frontmatter is treated as a stale draft.
* **File removed**: logged at DEBUG and skipped. The projection
  will re-create on the next bootstrap; mirroring the delete to
  Lithos is out of scope for Slice 5.
* **Malformed frontmatter**: logged at WARNING and skipped — the
  parse returns no ``lithos_id`` so we have nothing to push against.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from lithos_loom.bus import Event, EventBus
from lithos_loom.render_project_context import compute_body_hash, extract_frontmatter
from lithos_loom.sync_state import ProjectionSyncState

__all__ = ["ObsidianDirWatcher"]

logger = logging.getLogger(__name__)


@dataclass
class ObsidianDirWatcher:
    """Polling-based source for project-context vault edits.

    Constructed by the ``obsidian-sync`` child with a bus + the same
    :class:`ProjectionSyncState` handed to the project-context
    projection. ``run()`` loops forever; cancel the task to stop.

    ``projects_root`` is the absolute path to
    ``<vault>/<projects_dir>``. The watcher walks
    ``<projects_root>/**/*.md`` per poll.
    """

    bus: EventBus
    projects_root: Path
    sync_state: ProjectionSyncState
    poll_interval_seconds: float = 0.25
    _now_provider: Any = field(default=lambda: datetime.now(UTC))
    """Wall-clock seam for tests so emitted event timestamps are
    deterministic. Production callers leave at the default."""

    def __post_init__(self) -> None:
        # Per-file whole-file hash from the last poll. Lets the cheapest
        # layer (unchanged-since-last-poll) short-circuit before any
        # frontmatter parsing happens.
        self._last_seen_file_hashes: dict[Path, bytes] = {}
        # Per-doc-id body hash we last observed the operator commit to
        # disk. Layered ON TOP of ``sync_state.note_body_hashes`` (the
        # projection's view): on first sight of a doc we fall back to
        # sync_state, then advance this map as we observe each user
        # edit so a frontmatter-only re-save after a body edit doesn't
        # re-emit the same body transition. Mirror of
        # ``_observed_markers`` in the single-file watcher.
        self._observed_body_hashes: dict[str, bytes] = {}

    async def run(self) -> None:
        """Poll forever. Cancellable.

        Unlike the single-file watcher we don't pre-seed any hash here
        — per-file state populates on first poll. The first poll for
        each file performs the layered self-write check (sync_state +
        ``_observed_body_hashes``) so projection-known docs at their
        projected hash are silently absorbed and operator-edited docs
        emit on the first real body diff.
        """
        logger.info(
            "ObsidianDirWatcher: watching %s (poll=%.3fs)",
            self.projects_root,
            self.poll_interval_seconds,
        )
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "ObsidianDirWatcher: poll failed for %s; continuing",
                    self.projects_root,
                )
            await asyncio.sleep(self.poll_interval_seconds)

    async def poll_once(self) -> int:
        """Walk the projects tree once, emit events for body-only diffs.

        Returns the total count of ``obsidian.note.modified`` events
        published this poll. Zero when no files exist, all files match
        the projection's last write, all changes are frontmatter-only,
        or all changes are operator-created files we don't track.

        Files removed since the previous poll are noted in
        ``_last_seen_file_hashes`` and dropped from the map (no emit
        — see module docstring); they're handled silently rather than
        being mirrored to Lithos.
        """
        if not self.projects_root.exists():
            return 0

        published = 0
        current_files: set[Path] = set()
        for path in sorted(self.projects_root.rglob("*.md")):
            if not path.is_file():
                continue
            current_files.add(path)
            if await self._poll_one_file(path):
                published += 1

        # Drop entries for files that disappeared so a re-creation later
        # isn't suppressed by a stale hash. We deliberately do NOT emit
        # for removals (see module docstring — projection bootstrap will
        # re-create; mirroring deletes to Lithos is out of scope).
        removed = set(self._last_seen_file_hashes) - current_files
        for path in removed:
            logger.debug(
                "ObsidianDirWatcher: file %s removed since last poll; "
                "dropping cached hash (no emit)",
                path,
            )
            self._last_seen_file_hashes.pop(path, None)

        return published

    async def _poll_one_file(self, path: Path) -> bool:
        """Process a single file's poll cycle.

        Returns ``True`` iff an ``obsidian.note.modified`` event was
        published for this file this poll. ``False`` covers all the
        absorb paths (unchanged, projection self-write,
        frontmatter-only edit, missing lithos_id, malformed
        frontmatter).
        """
        raw = _read_file(path)
        if raw is None:
            return False
        current_file_hash = hashlib.sha256(raw).digest()

        # Layer 1: nothing changed since last poll. Single hash compare,
        # no parsing, no I/O beyond the read.
        if current_file_hash == self._last_seen_file_hashes.get(path):
            return False

        text = raw.decode("utf-8", errors="replace")
        frontmatter, body = extract_frontmatter(text)
        doc_id = frontmatter.get("lithos_id") if isinstance(frontmatter, dict) else None
        if not isinstance(doc_id, str) or not doc_id:
            # No frontmatter, malformed frontmatter, or operator created
            # a file without going through the projection / CLI. We have
            # nothing to push against; cache the hash so subsequent
            # polls don't re-parse on every cycle, and log loudly the
            # first time we see it.
            if path not in self._last_seen_file_hashes:
                logger.warning(
                    "ObsidianDirWatcher: file %s has no lithos_id in "
                    "frontmatter; skipping (operator must use 'project "
                    "create' to introduce new project-context docs)",
                    path,
                )
            self._last_seen_file_hashes[path] = current_file_hash
            return False

        # Layer 2: this exact file matches the projection's last write
        # (or the note-push handler's post-success re-render). Either
        # way we wrote these bytes; absorb silently and refresh the
        # body-hash baseline so a subsequent operator edit measures
        # against the projection's fresh view.
        projection_file_hash = self.sync_state.note_file_hashes.get(doc_id)
        if (
            projection_file_hash is not None
            and projection_file_hash == current_file_hash
        ):
            logger.debug(
                "ObsidianDirWatcher: %s matches projection's last write "
                "for doc %s; suppressing self-write",
                path,
                doc_id,
            )
            self._last_seen_file_hashes[path] = current_file_hash
            # Drop the local body-hash override; projection is the
            # fresh authority. Next operator edit measures against
            # ``sync_state.note_body_hashes`` until we observe one.
            self._observed_body_hashes.pop(doc_id, None)
            return False

        # Layer 3: body-only diff. Frontmatter-only edits (operator
        # adds a Dataview field, e.g.) yield identical body hashes and
        # silently absorb. A genuine body edit emits exactly once per
        # transition — subsequent polls observing the same body hash
        # don't re-emit.
        current_body_hash = compute_body_hash(text)
        # Prefer the local observed-hash if we have one (operator has
        # made an edit we already processed) over sync_state's view
        # (projection's last-rendered body) — without this, two
        # successive saves of the same body would emit twice.
        baseline_body_hash = self._observed_body_hashes.get(
            doc_id
        ) or self.sync_state.note_body_hashes.get(doc_id)

        if baseline_body_hash is None:
            # First sight of a doc that has frontmatter but no
            # projection baseline. Could be: operator's draft sitting
            # in the projects directory with a synthetic id, or a
            # pre-existing file before the daemon was started. Cache
            # the hash (so subsequent polls short-circuit) and seed
            # the body baseline so a SUBSEQUENT edit emits — but the
            # first sight itself doesn't, since we have nothing
            # authoritative to push against.
            logger.debug(
                "ObsidianDirWatcher: %s has lithos_id=%s with no projection "
                "baseline; seeding body-hash from disk without emitting",
                path,
                doc_id,
            )
            self._observed_body_hashes[doc_id] = current_body_hash
            self._last_seen_file_hashes[path] = current_file_hash
            return False

        if current_body_hash == baseline_body_hash:
            # Body unchanged → frontmatter-only edit (operator added a
            # Dataview field, fixed a typo in a tag list, etc.). D28
            # invariant: absorb silently.
            logger.debug(
                "ObsidianDirWatcher: %s body unchanged for doc %s "
                "(frontmatter-only edit); absorbing",
                path,
                doc_id,
            )
            self._last_seen_file_hashes[path] = current_file_hash
            return False

        # Real body edit. Emit.
        lithos_version_raw = frontmatter.get("lithos_version")
        try:
            lithos_version = int(lithos_version_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            # Frontmatter missing/malformed version. We could fall back
            # to ``sync_state.note_versions[doc_id]`` but that may be
            # stale relative to what's actually in the file the
            # operator's looking at. Safer to skip the push than to
            # provide a bogus ``expected_version`` and walk straight
            # into a guaranteed conflict.
            logger.warning(
                "ObsidianDirWatcher: %s has lithos_id=%s but missing/"
                "malformed lithos_version (%r); skipping (cannot provide "
                "expected_version for optimistic locking)",
                path,
                doc_id,
                lithos_version_raw,
            )
            self._last_seen_file_hashes[path] = current_file_hash
            self._observed_body_hashes[doc_id] = current_body_hash
            return False

        slug, filename = _slug_and_filename(path, self.projects_root)
        await self._publish_modified(
            doc_id=doc_id,
            lithos_version=lithos_version,
            slug=slug,
            filename=filename,
            vault_path=path,
            body=body,
        )

        self._observed_body_hashes[doc_id] = current_body_hash
        self._last_seen_file_hashes[path] = current_file_hash
        return True

    async def _publish_modified(
        self,
        *,
        doc_id: str,
        lithos_version: int,
        slug: str,
        filename: str,
        vault_path: Path,
        body: str,
    ) -> None:
        """Publish a single ``obsidian.note.modified`` event."""
        event = Event(
            type="obsidian.note.modified",
            timestamp=self._now_provider(),
            payload=MappingProxyType(
                {
                    "lithos_id": doc_id,
                    "lithos_version": lithos_version,
                    "slug": slug,
                    "filename": filename,
                    "vault_path": str(vault_path),
                    "body": body,
                }
            ),
        )
        await self.bus.publish(event)
        logger.info(
            "ObsidianDirWatcher: published obsidian.note.modified doc=%s "
            "(slug=%s file=%s version=%d)",
            doc_id,
            slug,
            filename,
            lithos_version,
        )


# ── helpers ────────────────────────────────────────────────────────────


def _read_file(path: Path) -> bytes | None:
    """Read ``path``'s current contents, or ``None`` when absent /
    unreadable.

    Mirrors :func:`obsidian_fs_watcher._read_file` — returning raw bytes
    (not a hash or decoded text) lets the caller reuse the same bytes
    for both hashing and parsing without a second disk read.
    """
    try:
        return path.read_bytes()
    except (FileNotFoundError, OSError):
        return None


def _slug_and_filename(path: Path, projects_root: Path) -> tuple[str, str]:
    """Compute ``(slug, filename)`` for an event payload.

    ``projects_root`` is ``<vault>/<projects_dir>``; ``path`` is
    ``<projects_root>/<slug>/<filename>.md`` (or, for nested files,
    ``<projects_root>/<slug>/<subdir>/<filename>.md`` — we treat the
    full subdirectory + filename as the ``filename`` so the
    slug-extraction stays stable on ``path.parts[len(root.parts)]``).

    Returns ``("<slug>", "<rest of relpath>")``. The slug is the first
    directory segment under ``projects_root``; the filename is
    everything after it, joined back with ``/`` for cross-platform
    stability in the event payload.
    """
    try:
        relative = path.relative_to(projects_root)
    except ValueError:
        # Defensive: rglob inside projects_root should always yield
        # paths under it. If somehow it doesn't (symlink shenanigans),
        # treat the whole path as the filename with an empty slug so
        # the downstream handler logs+drops rather than crashes.
        return "", str(path)
    parts = relative.parts
    if len(parts) < 2:
        # File directly under projects_root with no slug subdir — not
        # a shape the projection creates, but we degrade gracefully
        # rather than crashing.
        return "", parts[-1] if parts else ""
    slug = parts[0]
    filename = "/".join(parts[1:])
    return slug, filename
