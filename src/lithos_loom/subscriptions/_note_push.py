"""``note-push`` subscription handler.

Consumes ``obsidian.note.modified`` events emitted by
:class:`~lithos_loom.sources.obsidian_dir_watcher.ObsidianDirWatcher`
and pushes the operator's body edit back to Lithos via
:meth:`LithosClient.note_write` with optimistic locking.

Lifecycle per event:

1. **Parse payload** for ``lithos_id``, ``lithos_version``, ``body``,
   ``slug``, ``filename``, ``vault_path``.
2. **Fetch canonical** via :meth:`LithosClient.note_read`. We need
   the title (``lithos_write`` requires it and we can't derive it
   from the body without re-parsing the H1, which is a separate
   source of truth from the doc's identity), the canonical tag list
   (so we don't accidentally drop tags the operator hasn't seen),
   and the ``note_type`` / ``status`` (preserved verbatim).
3. **Call note_write** with ``expected_version=lithos_version``.
4. **Branch on result.status**:

   * ``"updated"`` — fetch the bumped version, rewrite local
     frontmatter (body unchanged) so the next edit's
     ``expected_version`` matches.
   * ``"version_conflict"`` — re-fetch canonical, hand off to
     :func:`._note_conflict.resolve_conflict`.
   * anything else (``"slug_collision"`` shouldn't happen on update;
     ``"invalid_input"`` / ``"content_too_large"`` are operator
     errors) — log loudly with the server message, leave local
     file alone.

The handler is **stateful** via :func:`make_handler` because it
needs ``sync_state`` (to coordinate self-write suppression with the
dir-watcher) and ``conflicts_dir`` (derived from
``cfg.obsidian_sync.vault_path``). Mirror-shape of the projection's
factory.

Idempotency: re-firing the same event after a successful push is a
no-op because the renderer produces byte-stable output for a given
Note, and :meth:`LithosClient.note_write` returns ``"updated"`` even
when ``content`` matches (the version bump happens server-side
unconditionally). The next dir-watcher poll then sees the bumped
frontmatter as a self-write (sync_state matches), absorbs it, and
moves on.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from pathlib import Path

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


_CONFLICTS_RELPATH = Path("_lithos/conflicts")


def make_handler(
    cfg: LoomConfig,
    *,
    sync_state: ProjectionSyncState | None = None,
) -> Handler:
    """Build the note-push handler bound to ``cfg``.

    ``cfg.obsidian_sync`` must be set; the obsidian-sync child's
    spawn gate guarantees this. ``sync_state=None`` (test default)
    constructs a fresh isolated state — fine for unit tests where the
    dir-watcher isn't running, but production wiring shares one
    sync_state between the projection, the dir-watcher, and this
    handler so all three see consistent coordination state.
    """
    obs = cfg.obsidian_sync
    if obs is None:
        raise RuntimeError(
            "make_handler called without [obsidian_sync] config; the "
            "supervisor's spawn gate should have prevented this"
        )
    conflicts_dir = obs.vault_path / _CONFLICTS_RELPATH
    sync_state = sync_state if sync_state is not None else ProjectionSyncState()

    async def handle(event: Event, ctx: SubscriptionContext) -> None:
        try:
            doc_id = str(event.payload["lithos_id"])
            expected_version = int(event.payload["lithos_version"])
            body = str(event.payload["body"])
            slug = str(event.payload.get("slug") or "")
            filename = str(event.payload.get("filename") or "")
            vault_path_str = str(event.payload.get("vault_path") or "")
        except (KeyError, TypeError, ValueError) as exc:
            ctx.logger.warning(
                "note-push: malformed payload for %s: %r",
                event.type,
                exc,
            )
            return
        if not vault_path_str:
            ctx.logger.warning(
                "note-push: payload for doc=%s missing vault_path; skipping",
                doc_id,
            )
            return
        local_path = Path(vault_path_str)

        # Pre-fetch canonical for title / tags / note_type / status. The
        # operator's body is the only field we push; everything else is
        # preserved verbatim from the server's current view. Skipping
        # this fetch would force us to invent values for title (from
        # the body's H1 — fragile) and tags (from frontmatter — but
        # the operator might have removed them deliberately).
        current = await ctx.lithos.note_read(id=doc_id)
        if current is None:
            ctx.logger.warning(
                "note-push: doc=%s not found in Lithos (deleted between "
                "operator edit and push?); skipping",
                doc_id,
            )
            return

        result = await ctx.lithos.note_write(
            id=doc_id,
            agent=ctx.agent_id,
            title=current.title,
            content=body,
            tags=list(current.tags),
            note_type=current.note_type or "concept",
            expected_version=expected_version,
            status=current.status,
        )

        if result.status == "updated":
            # Re-fetch canonical for the post-write frontmatter rewrite.
            # The real Lithos write envelope is top-level
            # ``{status, id, path, version, warnings}`` (see
            # lithos/src/lithos/server.py:1327) — no ``document``
            # field, so :attr:`WriteResult.note` is always ``None``
            # in production. Without re-fetching, the rewrite uses
            # ``current`` (the pre-write canonical) and writes the
            # STALE version into the operator's local frontmatter,
            # so the operator's next edit would push with the old
            # ``expected_version`` and hit a guaranteed conflict.
            # One extra RPC per push is acceptable; these are
            # operator-initiated edits, not bulk writes.
            post_write = await ctx.lithos.note_read(id=doc_id)
            if post_write is None:
                ctx.logger.warning(
                    "note-push: doc=%s vanished between successful push "
                    "and post-write fetch; skipping frontmatter refresh "
                    "(local file left at pre-push version)",
                    doc_id,
                )
                return
            await _refresh_local_frontmatter(
                doc_id=doc_id,
                note=post_write,
                lithos_path=_pick_lithos_path(post_write, current, slug, filename),
                local_path=local_path,
                sync_state=sync_state,
                ctx=ctx,
            )
            return

        if result.status == "duplicate":
            # Lithos reports the body was already what we sent — no
            # version bump, no rewrite needed. The local file's
            # ``lithos_version`` is already current. Common when
            # re-firing the same event (idempotency) and harmless.
            ctx.logger.info(
                "note-push: doc=%s reported duplicate (body unchanged "
                "server-side); skipping frontmatter refresh",
                doc_id,
            )
            return

        if result.status == "version_conflict":
            ctx.logger.warning(
                "note-push: version_conflict for doc=%s (operator had "
                "v%d, canonical at v%s); resolving",
                doc_id,
                expected_version,
                result.current_version,
            )
            # Re-fetch canonical body — the version_conflict envelope
            # carries ``current_version`` but not the canonical body.
            canonical = await ctx.lithos.note_read(id=doc_id)
            if canonical is None:
                ctx.logger.warning(
                    "note-push: doc=%s vanished between conflict detection "
                    "and canonical fetch; skipping conflict resolution",
                    doc_id,
                )
                return
            canonical_lithos_path = _pick_lithos_path(
                canonical, current, slug, filename
            )
            await resolve_conflict(
                local_path=local_path,
                canonical_note=canonical,
                canonical_lithos_path=canonical_lithos_path,
                conflicts_dir=conflicts_dir,
                slug=slug,
                filename=filename,
                sync_state=sync_state,
                doc_id=doc_id,
                logger_=ctx.logger,
            )
            return

        # Anything else — slug_collision (shouldn't happen on update),
        # invalid_input, content_too_large. Operator error or server
        # schema drift; leave local file alone and surface the message.
        ctx.logger.warning(
            "note-push: doc=%s write returned status=%s message=%r; "
            "leaving local file unchanged",
            doc_id,
            result.status,
            result.message,
        )

    return handle


async def _refresh_local_frontmatter(
    *,
    doc_id: str,
    note: Note,
    lithos_path: str,
    local_path: Path,
    sync_state: ProjectionSyncState,
    ctx: SubscriptionContext,
) -> None:
    """After a successful push, re-render the local file with the
    server's bumped version + updated_at.

    The body half is unchanged — we just pushed it — but the
    frontmatter carries the new ``lithos_version`` the dir-watcher
    will read as ``expected_version`` for the operator's next edit.
    Without this step the next edit would push with a stale version
    and trip a guaranteed conflict on every single save.

    sync_state records BEFORE the atomic write, matching the
    projection's ordering invariant: a dir-watcher poll racing this
    rewrite must see both the new bytes AND the matching coordination
    state so the rewrite is absorbed as a self-write.
    """
    note_for_render = dataclasses.replace(
        note, path=lithos_path, slug=_slug_from_path(lithos_path)
    )
    rendered = render_doc(note_for_render)
    file_hash = hashlib.sha256(rendered.encode("utf-8")).digest()
    body_hash = compute_body_hash(rendered)

    sync_state.record_project_context_write(
        doc_id=doc_id,
        file_hash=file_hash,
        body_hash=body_hash,
        version=note.version,
        projected_path=local_path,
    )
    await write_file_atomic(local_path, rendered)
    ctx.logger.info(
        "note-push: pushed doc=%s and refreshed local frontmatter at %s (version=%d)",
        doc_id,
        local_path,
        note.version,
    )


def _pick_lithos_path(
    primary: Note,
    fallback: Note,
    slug: str,
    filename: str,
) -> str:
    """Pick the first non-empty Lithos path from primary/fallback Notes,
    falling back to ``projects/<slug>/<filename>`` reconstructed from
    the watcher payload.

    Why two Notes: :meth:`LithosClient.note_read` doesn't return
    ``path`` (see the projection module's docstring), so the freshly
    fetched canonical typically has ``path=""``. The ``current`` /
    ``updated`` notes may carry path from earlier list-shaped
    responses, hence the primary→fallback chain. Watcher payload's
    slug+filename is the path of last resort — always populated and
    sufficient for renderer frontmatter even when both notes are
    path-empty.
    """
    if primary.path:
        return primary.path
    if fallback.path:
        return fallback.path
    if slug and filename:
        return f"projects/{slug}/{filename}"
    return ""


def _slug_from_path(lithos_path: str) -> str:
    """Extract slug from a Lithos doc path (mirror of the projection's
    derivation)."""
    parts = lithos_path.split("/")
    if len(parts) >= 2 and parts[0] == "projects":
        return parts[1]
    return parts[0] if parts else ""
