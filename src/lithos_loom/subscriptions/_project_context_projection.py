"""``project-context-projection`` subscription handler.

Consumes ``lithos.note.{created,updated,deleted}`` events emitted by
:class:`~lithos_loom.sources.lithos_note_stream.LithosNoteStream` and
writes/rewrites/removes per-project-context Markdown files under
``<vault>/<projects_dir>/<slug>/<filename>.md``.

The filter (path-prefix + tag) lives at the subscription, not the
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
import dataclasses
import hashlib
import logging
from pathlib import Path
from typing import Any

from lithos_loom.bus import Event
from lithos_loom.config import LoomConfig
from lithos_loom.lithos_client import Note
from lithos_loom.render_project_context import compute_body_hash, render_doc
from lithos_loom.subscriptions import Handler, SubscriptionContext
from lithos_loom.subscriptions._atomic_write import write_file_atomic
from lithos_loom.subscriptions._note_conflict import resolve_conflict
from lithos_loom.sync_state import ProjectionSyncState

__all__ = ["make_handler"]

logger = logging.getLogger(__name__)


_PROJECTS_PATH_PREFIX = "projects/"
_PROJECT_CONTEXT_TAG = "project-context"
# Relative-to-vault location of the conflicts archive — mirror of the
# value the note-push handler uses. Centralised here rather than
# importing across modules to keep each module's deps shallow; the
# anti-drift contract is "if the operator surfaces conflicts somewhere
# else, both this constant AND _note_push._CONFLICTS_RELPATH must
# move together." Today there's nothing to drift against because
# there's no operator-facing config knob for it.
_CONFLICTS_RELPATH = Path("_lithos/conflicts")

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
    a dir-watcher consumer to coordinate with.

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
    conflicts_dir = obs.vault_path / _CONFLICTS_RELPATH
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

        # Path-prefix filter at the subscription boundary. The source
        # publishes all note events; we only project docs under
        # ``projects/``.
        # We MUST get path from the SSE event because ``lithos_read``
        # does not return ``path`` in its response (it lives in
        # ``metadata.namespace`` as the directory only, no filename).
        # Both event paths that reach us — bootstrap (from
        # ``note_list``) and live (from ``intake.write``) — populate
        # ``path`` in the payload. File-watcher-emitted events do not
        # carry ``id`` so the source drops them before they get here,
        # which means we'll never see a payload with id-but-no-path.
        sse_path = str(event.payload.get("path") or "")
        if not sse_path:
            ctx.logger.warning(
                "project-context-projection: payload for %s id=%s has no "
                "'path' (cannot compute vault target); skipping",
                event.type,
                note_id,
            )
            return
        if not sse_path.startswith(_PROJECTS_PATH_PREFIX):
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

        # Tag re-check on the freshly fetched note. The SSE event's
        # tags are not in the payload at all (the bootstrap and intake
        # paths both publish only ``{id, title, path}``) — this is the
        # only filter that runs against the canonical Lithos tags.
        # A rejection triggers cleanup so a tag removal in Lithos
        # removes the stale projection file on disk.
        #
        # Note: we do NOT re-check ``note.path`` because
        # ``lithos_read`` does not return path (only
        # ``metadata.namespace``), so ``note.path`` is always empty
        # after a real read. The SSE path is the authoritative source.
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

        await _project_note(
            note, sse_path, projects_root, conflicts_dir, sync_state, ctx
        )

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
    lithos_path: str,
    projects_root: Path,
    conflicts_dir: Path,
    sync_state: ProjectionSyncState,
    ctx: Any,
) -> None:
    """Render and write a single project-context note to the vault.

    ``lithos_path`` is the doc's Lithos-canonical path
    (``projects/<slug>/<filename>.md``) sourced from the SSE event
    payload rather than from ``note.path`` — ``lithos_read`` does
    not return ``path`` (only ``metadata.namespace`` which is the
    directory) so the note we get back from the client has
    ``path=""``. The SSE-published payload is the only authoritative
    source for the full path under projection.

    Per-doc dedup uses a **whole-file hash** (not body-only). Frontmatter
    fields (``lithos_version``, ``status``, ``tags``, ``lithos_updated_at``)
    must mirror Lithos, so a version-bump or status-flip with unchanged
    body MUST still rewrite the file. Hashing only the body would silently
    skip those updates and leave stale frontmatter on disk — breaking the
    optimistic-lock contract (the projection's frontmatter version is what
    the dir-watcher reads to provide ``expected_version`` for push-back).

    Path migration: if the note's vault path differs from what we
    last wrote (e.g. doc moved from ``projects/foo/context.md`` to
    ``projects/bar/context.md``), the OLD file is unlinked only
    AFTER the new write succeeds. Earlier versions unlinked first
    and on a write failure the doc disappeared from the vault
    entirely (reviewer-finding regression on PR #37); the post-
    success ordering keeps the old file as a fallback when the
    new write fails.

    Self-write coordination: ``record_project_context_write`` fires
    *before* the atomic rename so a concurrent dir-watcher poll that
    sees the new file also sees the matching coordination state. On
    write failure, sync_state is rolled back to its
    *prior* state (not cleared) — preserving the prior_path memory
    so the next event can retry the migration with the same
    cleanup semantics rather than treating it as a fresh
    projection that wouldn't know about the orphan old file.
    """
    # Inject SSE-derived path + slug into the Note so the renderer's
    # frontmatter carries the ``slug`` field (without this, queries
    # filtering on ``slug:`` in the vault break).
    # ``projects/<slug>/...`` → ``<slug>``; lithos_path was already
    # validated to start with ``projects/`` at the handler boundary,
    # so the indexing here is safe (split yields at least 2 parts).
    slug = lithos_path[len(_PROJECTS_PATH_PREFIX) :].split("/", 1)[0]
    note_for_render = dataclasses.replace(note, path=lithos_path, slug=slug)
    rendered = render_doc(note_for_render)
    rendered_file_hash = hashlib.sha256(rendered.encode("utf-8")).digest()
    # Body-only hash is what the dir-watcher reads as the baseline for
    # its body-only diff. Recording it here means the watcher can
    # suppress its own self-write detection on body changes AND absorb
    # frontmatter-only operator edits silently.
    rendered_body_hash = compute_body_hash(rendered)

    # Lithos path is ``projects/<slug>/<filename>.md``; strip the
    # ``projects/`` prefix so the vault path is
    # ``<projects_root>/<slug>/<filename>.md``. This makes the slug +
    # filename map 1:1 across Lithos and vault.
    rel_path = lithos_path[len(_PROJECTS_PATH_PREFIX) :]
    target = projects_root / rel_path

    # Snapshot prior state so we can roll back on write failure to
    # the exact pre-event values (NOT to empty — preserving the
    # prior_path memory is what lets a retried migration still
    # know about the orphan old file).
    prior_hash = sync_state.note_file_hashes.get(note.id)
    prior_body_hash = sync_state.note_body_hashes.get(note.id)
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

    # Cold-start divergence check (reviewer finding on PR #45). The
    # projection bootstrap fires a ``lithos.note.created`` event for
    # every project-context doc when the daemon starts. If the daemon
    # was previously running and the operator edited a projected file
    # while it was down, the local body differs from the canonical body
    # Lithos has — and sync_state is empty across restart so the
    # in-memory baseline doesn't tell us "this is an operator edit."
    # Without this check, the projection would silently overwrite the
    # operator's edit with the canonical body (data loss).
    #
    # Detection: file exists on disk + no ``note_body_hashes`` baseline
    # for this doc THIS session (=> first event for this doc since
    # daemon start). If the on-disk body differs from canonical, route
    # through the same conflict resolver runtime version_conflict uses
    # — operator's local body is moved to ``<vault>/_lithos/conflicts``
    # for recovery, canonical body is pulled to the original path, and
    # the ``[Friction]`` breadcrumb fires for operator visibility.
    #
    # The check is conservative on two axes: it ONLY fires when no
    # baseline exists (won't re-trigger during runtime), and ONLY when
    # the existing file's body actually differs from canonical (no-op
    # for the common "daemon restart with no operator edits" case).
    if (
        target.exists()
        and note.id not in sync_state.note_body_hashes
        and await _resolve_cold_start_divergence(
            note=note,
            lithos_path=lithos_path,
            target=target,
            rendered_body_hash=rendered_body_hash,
            conflicts_dir=conflicts_dir,
            sync_state=sync_state,
            ctx=ctx,
        )
    ):
        # Resolver moved local + pulled canonical + populated
        # sync_state. Nothing else to do — the normal write path
        # would re-write what the resolver already wrote.
        return

    # Coordination state BEFORE the write — any concurrent dir-watcher
    # poll that sees the new file's bytes must also see matching state.
    sync_state.record_project_context_write(
        doc_id=note.id,
        file_hash=rendered_file_hash,
        body_hash=rendered_body_hash,
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
            # prior_body_hash + prior_version + prior_path are populated
            # together with prior_hash — all four are written by
            # ``record_project_context_write`` in one shot — so the
            # paired asserts express the invariant rather than guarding
            # against drift in this method.
            assert prior_body_hash is not None
            sync_state.note_body_hashes[note.id] = prior_body_hash
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
        slug,
        note.version,
    )


async def _resolve_cold_start_divergence(
    *,
    note: Note,
    lithos_path: str,
    target: Path,
    rendered_body_hash: bytes,
    conflicts_dir: Path,
    sync_state: ProjectionSyncState,
    ctx: Any,
) -> bool:
    """If the on-disk file's body differs from canonical, route through
    the conflict resolver and return True; otherwise return False.

    Called by :func:`_project_note` when a file exists on disk but no
    sync_state baseline exists for this doc this session (= cold
    start). A matching body means the operator didn't edit while we
    were down; the caller falls through to the normal write path
    (which then writes canonical bytes — needed because frontmatter
    may have advanced even when body didn't).

    The resolver itself updates sync_state with the canonical hashes,
    so subsequent events for this doc go through the normal write
    path (the ``note.id in sync_state.note_body_hashes`` gate flips
    True).
    """
    try:
        existing_text = target.read_text(encoding="utf-8")
    except OSError as exc:
        # File became unreadable between the exists() check and the
        # read. Skip the divergence check; the normal write path will
        # overwrite (no operator edit to preserve if we can't read).
        ctx.logger.debug(
            "project-context-projection: cold-start check skipped for "
            "note %s — file at %s unreadable: %s",
            note.id,
            target,
            exc,
        )
        return False
    existing_body_hash = compute_body_hash(existing_text)
    if existing_body_hash == rendered_body_hash:
        # No body divergence — operator didn't edit while we were
        # down (or edited the body identically). Fall through to the
        # normal write path so frontmatter still refreshes.
        return False

    # Operator edit detected. Route through the same resolver
    # runtime version_conflict uses.
    rel_path = lithos_path[len(_PROJECTS_PATH_PREFIX) :]
    slug, _, filename = rel_path.partition("/")
    if not filename:
        # Path was exactly "projects/<slug>" with no filename — not a
        # shape the projection writes. Skip the cold-start check;
        # the normal path will surface this as a validation issue.
        ctx.logger.debug(
            "project-context-projection: cold-start check skipped for "
            "note %s — path %r has no filename component",
            note.id,
            lithos_path,
        )
        return False

    ctx.logger.warning(
        "project-context-projection: cold-start divergence for doc=%s "
        "(local body at %s differs from canonical); routing through "
        "conflict resolver to preserve operator edit",
        note.id,
        target,
    )
    await resolve_conflict(
        local_path=target,
        canonical_note=note,
        canonical_lithos_path=lithos_path,
        conflicts_dir=conflicts_dir,
        slug=slug,
        filename=filename,
        sync_state=sync_state,
        doc_id=note.id,
        logger_=ctx.logger,
    )
    return True


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
