"""Conflict resolver for the ``note-push`` handler.

Called inline from :mod:`._note_push` when ``lithos_write`` returns
``status="version_conflict"``. The operator's local body and Lithos's
canonical body have diverged: the operator edited locally while a
parallel write (another agent, the projection itself, an MCP call)
bumped the version upstream.

Resolution strategy:

1. Render the canonical Lithos body into the form the projection
   would have written.
2. Atomically move the operator's local file to
   ``<vault>/_lithos/conflicts/<slug>.<filename>.<timestamp>.md``.
3. Record the canonical hash + body hash + version + path into
   ``sync_state`` **before** writing the canonical body. This is the
   same ordering invariant the projection upholds — any dir-watcher
   poll racing against the canonical write must see matching
   coordination state so the rewrite is absorbed as a self-write
   rather than re-firing the push.
4. Atomic-write the canonical body to the operator's original path.
5. Log loudly at WARNING with a ``[Friction]`` breadcrumb + the
   conflict file path. The operator can diff the two files in their
   vault to recover their local changes.

Why move-first-then-write rather than write-first-then-move: if the
canonical write fails between the rename and the write, the operator
ends up with NO file at the original path — visible in daemon logs
and recoverable from the conflicts directory. If we wrote canonical
first and then moved, a failure between the write and the move would
leave the operator's local body in the conflicts archive AND the
canonical at the local path, but with sync_state still pointing at
the local hash — the next poll would re-emit, hammering Lithos.

We deliberately do NOT post a Lithos ``finding`` here: the current
``lithos_finding_post`` MCP tool requires a ``task_id``, and a
project-context conflict is doc-scoped, not task-scoped. The
``[Friction]`` log breadcrumb is the operator-visible signal until
Lithos grows a KB-doc-scoped finding tool. The breadcrumb shape
matches the project's stable-prefix convention so a future log-
scraper can surface conflicts the same way it surfaces other
``[Friction]`` events.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from lithos_loom.lithos_client import Note
from lithos_loom.render_project_context import compute_body_hash, render_doc
from lithos_loom.subscriptions._atomic_write import write_file_atomic
from lithos_loom.sync_state import ProjectionSyncState

__all__ = ["format_conflict_filename", "resolve_conflict"]


_DEFAULT_LOGGER = logging.getLogger(__name__)


def format_conflict_filename(slug: str, filename: str, timestamp: datetime) -> str:
    """Build the conflicts-directory filename for a single conflict.

    Shape: ``<slug>.<flat-filename>.<YYYYMMDDTHHMMSSZ>.md``.

    ``filename`` may contain ``/`` for nested vault paths — we replace
    those with ``-`` so the conflict file lives flat under
    ``_lithos/conflicts/`` rather than recreating the project's
    subdirectory structure (which would clutter the conflicts
    archive and make ``ls`` reviews painful).

    The trailing ``.md`` is preserved exactly once: if the source
    filename already has it we strip-then-reapply so the timestamp
    sits between the basename and the extension, where any
    Markdown-aware tool (Obsidian opens, glob patterns) will still
    recognise the file.
    """
    flat = filename.replace("/", "-")
    if flat.endswith(".md"):
        flat = flat[:-3]
    ts = timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{slug}.{flat}.{ts}.md"


async def resolve_conflict(
    *,
    local_path: Path,
    canonical_note: Note,
    canonical_lithos_path: str,
    conflicts_dir: Path,
    slug: str,
    filename: str,
    sync_state: ProjectionSyncState,
    doc_id: str,
    timestamp_provider: Callable[[], datetime] = lambda: datetime.now(UTC),
    logger_: logging.Logger | None = None,
) -> Path:
    """Move the operator's local file to the conflicts archive and
    pull the canonical body to the original path.

    Returns the absolute path of the relocated conflict file so the
    caller can surface it to the operator (log line, finding summary).

    Raises if the canonical render fails, or if the atomic move /
    canonical write fails after best-effort restore. The note-push
    handler treats the raise as a transient failure and lets the
    subscription runner's retry policy handle it; on terminal failure
    the operator sees a ``[Friction]`` finding via the runner's
    persistent-failure path.
    """
    log = logger_ or _DEFAULT_LOGGER

    # Render canonical first. If rendering raises (e.g. malformed
    # Note from a server schema drift) we haven't touched the
    # operator's local copy yet, so they can keep editing.
    note_for_render = dataclasses.replace(
        canonical_note, path=canonical_lithos_path, slug=slug
    )
    canonical_text = render_doc(note_for_render)
    canonical_file_hash = hashlib.sha256(canonical_text.encode("utf-8")).digest()
    canonical_body_hash = compute_body_hash(canonical_text)

    # Prepare the conflict path. Build the conflicts directory lazily —
    # most operators will never see a conflict, no need to provision
    # the directory ahead of time.
    timestamp = timestamp_provider()
    conflict_name = format_conflict_filename(slug, filename, timestamp)
    conflict_path = conflicts_dir / conflict_name
    conflicts_dir.mkdir(parents=True, exist_ok=True)

    # Move the operator's local file into the conflicts archive.
    # ``os.replace`` is atomic on same-filesystem renames (POSIX);
    # cross-filesystem renames fall back to copy+unlink which is
    # NOT atomic — that's an operator-misconfigured-vault concern
    # rather than something this code can sensibly recover from.
    # If the conflict_path already exists (timestamp collision under
    # extreme rapid-fire conflicts), ``os.replace`` overwrites it:
    # one-conflict-per-second is the practical upper bound, and we
    # prefer overwriting an older snapshot to crashing the handler.
    os.replace(local_path, conflict_path)
    log.warning(
        "note-conflict: moved operator's local body to %s (doc=%s, slug=%s, file=%s)",
        conflict_path,
        doc_id,
        slug,
        filename,
    )

    # Record sync_state BEFORE the canonical write. A concurrent
    # dir-watcher poll racing this rewrite must see the new file
    # AND the matching coordination state — otherwise it re-emits
    # the canonical body as a "user edit" and loops the push.
    sync_state.record_project_context_write(
        doc_id=doc_id,
        file_hash=canonical_file_hash,
        body_hash=canonical_body_hash,
        version=canonical_note.version,
        projected_path=local_path,
    )

    try:
        await write_file_atomic(local_path, canonical_text)
    except Exception:
        # Best-effort restore: try to put the operator's body back
        # under the original path so they don't end up with neither
        # canonical nor local at that path. The restore can also
        # fail; if it does, the conflict file is still at
        # conflict_path as a recovery option.
        log.exception(
            "note-conflict: canonical write to %s failed after move; "
            "attempting to restore operator's local copy from %s",
            local_path,
            conflict_path,
        )
        with contextlib.suppress(OSError):
            os.replace(conflict_path, local_path)
        # Reset sync_state — we don't know what's on disk now, so
        # the next event needs to start fresh rather than trusting
        # a hash for content that may not exist.
        sync_state.forget_project_context(doc_id=doc_id)
        raise

    # Stable [Friction] breadcrumb. Matches the project's other
    # breadcrumb prefixes ([Plan], [Drift], [ReviewPending], ...) so
    # a log-scraper can surface conflicts uniformly. Once Lithos has
    # a KB-doc-scoped finding tool we can also post upstream — for
    # now the daemon log is the sole channel.
    log.warning(
        "[Friction] note-push conflict for doc=%s slug=%s file=%s "
        "(operator body preserved at %s; canonical v%d pulled to %s)",
        doc_id,
        slug,
        filename,
        conflict_path,
        canonical_note.version,
        local_path,
    )

    return conflict_path
