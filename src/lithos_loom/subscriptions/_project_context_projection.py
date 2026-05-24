"""``project-context-projection`` subscription handler (Slice 4 US29).

Consumes ``lithos.note.{created,updated,deleted}`` events emitted by
:class:`~lithos_loom.sources.lithos_note_stream.LithosNoteStream` and
writes/rewrites/removes per-project-context Markdown files under
``<vault>/<projects_dir>/<slug>/<filename>.md``.

D26 puts the filter (path-prefix + tag) at the subscription, not the
source — the source publishes ALL ``lithos.note.*`` events. The
projection drops any note whose ``path`` doesn't start with
``projects/`` or whose tags don't include ``project-context``.
Symmetric with the permissive task source.

The handler is intentionally simpler than the tasks projection
(:mod:`._obsidian_projection`):

- No in-memory ``_StateEntry`` map. The task projection accumulates
  all open tasks into one file; project context is one file per doc,
  so each event is self-contained. State lives in
  :class:`~lithos_loom.sync_state.ProjectionSyncState` (per-doc hash
  + version) rather than per-handler.
- No TTL eviction. Project-context docs persist until deleted in
  Lithos.
- No debouncing. Each event corresponds to a distinct file; there's
  no coalescing benefit.
- Per-doc dedup via the body hash recorded in sync_state — on
  bootstrap with N unchanged docs, N writes are short-circuited.

Lifecycle per event:

1. **Filter at boundary.** Skip notes not under ``projects/`` and
   skip those missing the ``project-context`` tag. Logged at DEBUG.
2. **Re-fetch.** The SSE payload carries only ``{id, title, path}``;
   we need the body + metadata. Call ``ctx.lithos.note_read(id=...)``.
3. **Filter again on the freshly fetched tags.** The SSE event's
   tags can be stale (the bootstrap path doesn't carry tags at all).
   Re-check after fetch.
4. **Render** via :func:`render_project_context.render_doc`.
5. **Dedup.** If the body hash matches ``sync_state.note_content_hashes[id]``
   skip the write — same content already on disk.
6. **Atomic write.** Record sync_state *before* committing the
   rename (same ordering invariant as the tasks projection).
7. **Deleted events** remove the local file (best-effort) and
   ``forget_project_context`` so a re-creation later isn't suppressed
   as a self-write.

The render module is pure; the atomic write reuses
:func:`._atomic_write.write_file_atomic` so the same temp + fsync +
rename contract (and load-bearing no-await-inside invariant) applies
to per-doc projection.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from pathlib import Path
from typing import Any

from lithos_loom.bus import Event
from lithos_loom.config import LoomConfig
from lithos_loom.lithos_client import Note
from lithos_loom.render_project_context import render_doc
from lithos_loom.subscriptions import Handler, SubscriptionContext
from lithos_loom.subscriptions._atomic_write import write_file_atomic
from lithos_loom.sync_state import ProjectionSyncState

__all__ = ["make_handler"]

logger = logging.getLogger(__name__)


_PROJECTS_PATH_PREFIX = "projects/"
_PROJECT_CONTEXT_TAG = "project-context"

_REEVALUATE_EVENTS: frozenset[str] = frozenset(
    {"lithos.note.created", "lithos.note.updated"}
)
_REMOVAL_EVENTS: frozenset[str] = frozenset({"lithos.note.deleted"})


def make_handler(
    cfg: LoomConfig,
    *,
    sync_state: ProjectionSyncState | None = None,
) -> Handler:
    """Build a stateful ``project-context-projection`` handler bound to ``cfg``.

    The returned coroutine captures the vault path + projects_dir
    from ``cfg.obsidian_sync`` and the per-doc state living in
    ``sync_state``. ``sync_state=None`` (test default) constructs a
    fresh isolated state — the projection still works, just without
    a dir-watcher consumer to coordinate with (relevant once Slice 5
    lands).

    ``cfg.obsidian_sync`` must be set; the obsidian-sync child's
    spawn gate guarantees this, but we assert for defensive
    readability (same shape as the tasks projection).
    """
    obs = cfg.obsidian_sync
    if obs is None:
        raise RuntimeError(
            "make_handler called without [obsidian_sync] config; the "
            "supervisor's spawn gate should have prevented this"
        )
    projects_root = obs.vault_path / obs.projects_dir
    sync_state = sync_state if sync_state is not None else ProjectionSyncState()

    async def handle(event: Event, ctx: SubscriptionContext) -> None:
        # Branch on event type first — guards against malformed payloads
        # on unknown event types (same pattern as the tasks projection).
        if event.type not in _REEVALUATE_EVENTS and event.type not in _REMOVAL_EVENTS:
            ctx.logger.debug(
                "project-context-projection: ignoring unexpected event type %s",
                event.type,
            )
            return

        try:
            note_id = str(event.payload["id"])
        except (KeyError, TypeError) as exc:
            ctx.logger.warning(
                "project-context-projection: malformed payload for %s: %r",
                event.type,
                exc,
            )
            return

        if event.type in _REMOVAL_EVENTS:
            # Removal events carry ``path`` per the source's hard
            # requirement (see LithosNoteStream._handle_sse_event —
            # we fail closed at the source if path is missing). The
            # path is what tells us which on-disk file to remove
            # since the doc is gone from Lithos by the time we react.
            try:
                path = str(event.payload["path"])
            except (KeyError, TypeError) as exc:
                ctx.logger.warning(
                    "project-context-projection: malformed deleted payload "
                    "(missing path) for %s: %r",
                    event.type,
                    exc,
                )
                return
            await _handle_deleted(note_id, path, projects_root, sync_state, ctx)
            return

        # Path-prefix filter at the boundary (D26). The source publishes
        # all note events; we only project docs under ``projects/``.
        # Source-emitted ``path`` may be empty for bootstrap-via-note_list
        # entries that lack a path field — re-fetch and check post-read.
        sse_path = str(event.payload.get("path") or "")
        if sse_path and not sse_path.startswith(_PROJECTS_PATH_PREFIX):
            # The note may have a stale projection on disk from a
            # previous lifecycle (e.g. doc moved out of projects/).
            # Clean it up — otherwise the file lingers indefinitely
            # because Lithos only emits note.deleted for actual
            # deletes, not for moves/retags.
            _cleanup_stale_projection(
                note_id,
                sync_state,
                ctx,
                reason=f"sse path {sse_path!r} outside projects/",
            )
            return

        # Re-fetch for the full body + metadata (tags, version,
        # updated_at). The SSE payload only carries
        # ``{id, title, path}``, which is insufficient for rendering.
        note = await ctx.lithos.note_read(id=note_id)
        if note is None:
            ctx.logger.info(
                "project-context-projection: note %s not found in Lithos "
                "(possibly deleted between event and read); skipping",
                note_id,
            )
            return

        # Re-check filters on the FRESHLY fetched note. The SSE event's
        # tag set can be stale (bootstrap paths carry partial metadata,
        # tags may have changed). This is the authoritative filter.
        # A rejection here also triggers cleanup — the doc previously
        # qualified (we have a projected file) but no longer does, so
        # the stale projection must be removed.
        if not note.path.startswith(_PROJECTS_PATH_PREFIX):
            _cleanup_stale_projection(
                note_id,
                sync_state,
                ctx,
                reason=f"fetched path {note.path!r} outside projects/",
            )
            return
        if _PROJECT_CONTEXT_TAG not in note.tags:
            _cleanup_stale_projection(
                note_id,
                sync_state,
                ctx,
                reason=(
                    f"fetched tags {list(note.tags)} do not include "
                    f"{_PROJECT_CONTEXT_TAG!r}"
                ),
            )
            return

        await _project_note(note, projects_root, sync_state, ctx)

    return handle


def _cleanup_stale_projection(
    note_id: str,
    sync_state: ProjectionSyncState,
    ctx: Any,
    *,
    reason: str,
) -> None:
    """Remove a previously-projected file when the note no longer
    qualifies for projection (tag removed, path moved out of
    ``projects/``, etc.).

    Idempotent — if there's no prior projection on record (the doc
    never qualified, or was already cleaned up), this is a silent
    no-op. The ``note_projected_paths`` map is the source of truth
    for "did we ever write this doc."
    """
    prior_path = sync_state.note_projected_paths.get(note_id)
    if prior_path is None:
        # No prior projection — the doc was never projected (e.g.
        # an event for a non-project-context doc that we cheaply
        # filtered at the boundary). Nothing to clean up.
        ctx.logger.debug(
            "project-context-projection: skipping note %s — %s "
            "(no prior projection to clean up)",
            note_id,
            reason,
        )
        return

    with contextlib.suppress(FileNotFoundError):
        prior_path.unlink()
    sync_state.forget_project_context(doc_id=note_id)
    ctx.logger.info(
        "project-context-projection: cleaned up stale projection at %s "
        "(note %s no longer qualifies: %s)",
        prior_path,
        note_id,
        reason,
    )


async def _project_note(
    note: Note,
    projects_root: Path,
    sync_state: ProjectionSyncState,
    ctx: Any,
) -> None:
    """Render and write a single project-context note to the vault.

    Per-doc dedup uses a **whole-file hash** (not body-only). US30
    requires frontmatter fields (``lithos_version``, ``status``,
    ``tags``, ``lithos_updated_at``) to mirror Lithos, so a
    version-bump or status-flip with unchanged body MUST still
    rewrite the file. Hashing only the body would silently skip
    those updates and leave stale frontmatter on disk — breaking
    Slice 5's optimistic-lock contract (the projection's frontmatter
    version is what the dir-watcher reads to provide
    ``expected_version`` for push-back).

    Path migration: if the note's vault path differs from what we
    last wrote (e.g. doc moved from ``projects/foo/context.md`` to
    ``projects/bar/context.md``), the OLD file is unlinked only
    AFTER the new write succeeds. Earlier versions unlinked first
    and on a write failure the doc disappeared from the vault
    entirely (reviewer-finding regression on PR #37); the post-
    success ordering keeps the old file as a fallback when the
    new write fails.

    Self-write coordination: ``record_project_context_write`` fires
    *before* the atomic rename so a concurrent Slice 5 dir-watcher
    poll that sees the new file also sees the matching coordination
    state. On write failure, sync_state is rolled back to its
    *prior* state (not cleared) — preserving the prior_path memory
    so the next event can retry the migration with the same
    cleanup semantics rather than treating it as a fresh
    projection that wouldn't know about the orphan old file.
    """
    rendered = render_doc(note)
    rendered_file_hash = hashlib.sha256(rendered.encode("utf-8")).digest()

    # Lithos path is ``projects/<slug>/<filename>.md``; strip the
    # ``projects/`` prefix so the vault path is
    # ``<projects_root>/<slug>/<filename>.md``. This makes the slug +
    # filename map 1:1 across Lithos and vault.
    rel_path = note.path[len(_PROJECTS_PATH_PREFIX) :]
    target = projects_root / rel_path

    # Snapshot prior state so we can roll back on write failure to
    # the exact pre-event values (NOT to empty — preserving the
    # prior_path memory is what lets a retried migration still
    # know about the orphan old file).
    prior_hash = sync_state.note_file_hashes.get(note.id)
    prior_version = sync_state.note_versions.get(note.id)
    prior_path = sync_state.note_projected_paths.get(note.id)

    # Whole-file dedup. Skip only when the prior projection is at
    # the SAME path AND the rendered bytes are identical.
    if prior_hash == rendered_file_hash and prior_path == target:
        ctx.logger.debug(
            "project-context-projection: skipping note %s — rendered file "
            "matches last write (no-op)",
            note.id,
        )
        return

    # Coordination state BEFORE the write — any concurrent dir-watcher
    # poll that sees the new file's bytes must also see matching state.
    sync_state.record_project_context_write(
        doc_id=note.id,
        file_hash=rendered_file_hash,
        version=note.version,
        projected_path=target,
    )
    try:
        await write_file_atomic(target, rendered)
    except Exception:
        # Roll back to the PRIOR state (not empty). If there was no
        # prior projection, fully forget. Otherwise restore each
        # field so the next event sees the old projection and can
        # retry the migration / write cleanly.
        if prior_hash is None:
            sync_state.forget_project_context(doc_id=note.id)
        else:
            sync_state.note_file_hashes[note.id] = prior_hash
            # prior_version is paired with prior_hash — both populated or
            # both absent — so the int cast is safe here.
            assert prior_version is not None
            sync_state.note_versions[note.id] = prior_version
            assert prior_path is not None
            sync_state.note_projected_paths[note.id] = prior_path
        raise

    # Write succeeded. Now safe to remove the old file if this was
    # a path migration. Doing this AFTER the new write means a
    # transient failure leaves the vault with the OLD file intact
    # rather than empty until the next retry.
    if prior_path is not None and prior_path != target:
        with contextlib.suppress(FileNotFoundError):
            prior_path.unlink()
        ctx.logger.info(
            "project-context-projection: removed stale projection at %s "
            "(note %s moved to %s)",
            prior_path,
            note.id,
            target,
        )

    ctx.logger.info(
        "project-context-projection: wrote %s (slug=%s, version=%d)",
        target,
        note.slug,
        note.version,
    )


async def _handle_deleted(
    note_id: str,
    lithos_path: str,
    projects_root: Path,
    sync_state: ProjectionSyncState,
    ctx: Any,
) -> None:
    """Remove the local file and forget the projection state.

    Best-effort delete: missing file (operator manually removed,
    earlier failed write) is fine. The sync_state forget is what
    prevents a subsequent re-creation of the same doc from being
    suppressed as a self-write.
    """
    if not lithos_path.startswith(_PROJECTS_PATH_PREFIX):
        ctx.logger.debug(
            "project-context-projection: skipping delete for note %s — "
            "path %r outside projects/",
            note_id,
            lithos_path,
        )
        return
    rel_path = lithos_path[len(_PROJECTS_PATH_PREFIX) :]
    target = projects_root / rel_path

    with contextlib.suppress(FileNotFoundError):
        target.unlink()
    sync_state.forget_project_context(doc_id=note_id)

    ctx.logger.info(
        "project-context-projection: removed %s (note %s deleted in Lithos)",
        target,
        note_id,
    )
