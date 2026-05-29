"""Per-project task archive subscription.

Appends every human-surfaced Lithos task that reaches a terminal state
(``completed`` / ``cancelled``) to a per-project, append-only Markdown
file at ``<vault>/<projects_dir>/<slug>/<slug>-done.md`` as a
Tasks-plugin line. This gives the operator a durable, grep-/Dataview-
queryable per-project history that outlives the global ``tasks.md``
file's lingering window.

The archive is a one-way, vault-only artifact derived from Lithos
events — never Lithos-canonical, never regenerated. The dir-watcher
excludes ``-done.md`` files so operator edits to them are inert (no
push, no reopen-request findings).

Coupling with the tasks projection, both running in the ``obsidian-sync``
child over one shared :class:`ProjectionSyncState`:

* **Surfaced gate.** The projection sets ``sync_state.surfaced[id]``
  when it writes an open actionable task line (and seeds it from the
  on-disk ``tasks.md`` at startup). The archiver only archives tasks
  with that flag set — automated / route-claimed-only work that never
  reached the operator's view is skipped.
* **Archive-then-evict.** On success the archiver sets
  ``sync_state.archived[id]``; the projection's flush-time eviction
  predicate drops the line from the global file in the same write the
  terminal event scheduled. A failed append leaves the flag unset, so
  the task stays ``[x]``/``[-]`` in the global file under the TTL
  fallback — no data-loss window.

The handler does synchronous I/O only (no internal ``await``): a single
O_APPEND write per event. It never schedules a projection flush — it
just sets the flag and lets the projection's own debounced flush do the
eviction.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from lithos_loom.bus import Event
from lithos_loom.config import LoomConfig
from lithos_loom.render import extract_task_ids, render_resolved_line
from lithos_loom.subscriptions import Handler, SubscriptionContext

# Reused verbatim from the tasks projection so the archived Task is
# reconstructed identically (same id/metadata/resolved_at parsing) and
# the resolution date matches what the global file showed. Same package,
# no import cycle (the projection does not import this module).
from lithos_loom.subscriptions._obsidian_projection import (
    _resolved_at_for,
    _task_from_payload,
)
from lithos_loom.sync_state import ProjectionSyncState

__all__ = ["make_handler"]

logger = logging.getLogger(__name__)

_TERMINAL_EVENTS: frozenset[str] = frozenset(
    {"lithos.task.completed", "lithos.task.cancelled"}
)

# Tasks whose ``metadata.project`` is missing, malformed, or unsafe land
# here so metadata drift never silently drops an archive line.
_UNASSIGNED = "_unassigned"

# A safe project slug for path construction: starts alphanumeric, then
# alphanumerics / underscores / hyphens only. ``metadata.project`` is
# agent/operator-controlled (unlike the project-context flow, whose slug
# comes from a Lithos-validated ``projects/`` path prefix), so anything
# that could escape ``projects_root`` (``/``, ``..``, leading dot, empty)
# fails this and routes to ``_unassigned``.
_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _safe_slug(value: object) -> str:
    """Return a filesystem-safe project slug, or ``_unassigned``.

    Guards against path traversal / nested-dir creation from a malformed
    ``metadata.project`` value. The rendered line may
    still echo the raw ``#project/<value>`` tag — that's cosmetic and
    matches what the global projection rendered for the same task; only
    the *path* is sanitised here.
    """
    if isinstance(value, str) and _SAFE_SLUG_RE.match(value):
        return value
    return _UNASSIGNED


def _done_file(projects_root: Path, slug: str) -> Path:
    """Resolve the per-project done-file path ``<root>/<slug>/<slug>-done.md``."""
    return projects_root / slug / f"{slug}-done.md"


def _load_done_ids(path: Path) -> set[str]:
    """Read the task ids already archived in ``path`` (empty set if absent).

    Used to build the per-slug dedup set lazily on the first event for
    that project, so a cold-start replay never double-appends. A missing
    file is the normal first-write case → empty set, no log. Any other
    read error (e.g. a permission flip) is warn-logged for diagnosability
    before returning empty — the subsequent ``_append_line`` will then
    fail on the same underlying problem and re-raise into the runner's
    retry/friction path, so the empty dedup set here can't cause a silent
    double-append.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except OSError as exc:
        logger.warning(
            "task-archive: could not read done file %s for dedup (%r); "
            "treating as empty",
            path,
            exc,
        )
        return set()
    return extract_task_ids(raw)


def _append_line(path: Path, line: str) -> None:
    """Append ``line`` (plus newline) to ``path`` via O_APPEND.

    Creates the parent directory and the file if absent. O_APPEND makes
    each write append at the current end of file, and — unlike the
    projection's temp+rename — no transient sibling file is produced, so
    there's nothing for Obsidian Sync to trip on (the done file itself IS
    meant to sync).

    ``os.write`` may perform a short write (POSIX permits writing fewer
    bytes than requested), so we loop until the whole buffer is flushed —
    otherwise a partial write would leave a truncated archive line while
    the caller goes on to mark the task archived, breaking the no-data-loss
    contract. Raises ``OSError`` on any I/O failure so the caller leaves
    the archived flag unset and the runner's retry/friction policy takes
    over.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        view = memoryview((line + "\n").encode("utf-8"))
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


def make_handler(
    cfg: LoomConfig,
    *,
    sync_state: ProjectionSyncState | None = None,
) -> Handler:
    """Build a stateful ``task-archive`` handler bound to ``cfg``.

    Captures the vault path + projects_dir from ``cfg.obsidian_sync``, a
    per-slug dedup cache, and the shared ``sync_state`` (the coupling
    seam with the tasks projection). ``sync_state=None`` (test default)
    constructs a fresh isolated state — note the archiver is then a no-op
    in practice because nothing populates ``surfaced``; production wires
    the shared instance from the obsidian-sync child.

    ``cfg.obsidian_sync`` must be set; the child's spawn gate guarantees
    this, asserted here for defensive readability (same shape as the
    tasks projection).
    """
    obs = cfg.obsidian_sync
    if obs is None:
        raise RuntimeError(
            "make_handler called without [obsidian_sync] config; the "
            "supervisor's spawn gate should have prevented this"
        )
    projects_root = obs.vault_path / obs.projects_dir
    sync_state = sync_state if sync_state is not None else ProjectionSyncState()
    # Per-slug set of task ids already on disk in that project's done
    # file. Lazily loaded on the first event for a slug (see _load_done_ids).
    dedup_cache: dict[str, set[str]] = {}

    async def handle(event: Event, ctx: SubscriptionContext) -> None:
        # Branch on event type first so a foreign payload can't blow up
        # on parsing (same guard shape as the tasks projection).
        if event.type not in _TERMINAL_EVENTS:
            ctx.logger.debug(
                "task-archive: ignoring unexpected event type %s", event.type
            )
            return

        try:
            task = _task_from_payload(event.payload)
        except (KeyError, TypeError, ValueError) as exc:
            ctx.logger.warning(
                "task-archive: malformed payload for %s: %r", event.type, exc
            )
            return

        # Surfaced gate: only archive tasks the operator actually saw in
        # the global projection. The projection sets ``surfaced`` when it
        # writes an open line and seeds it from disk on restart, so this
        # flag is the authoritative "was operator-visible" signal —
        # re-running ``would_be_actionable`` here would be redundant and
        # could disagree if route config changed mid-session.
        if not sync_state.surfaced.get(task.id):
            ctx.logger.debug(
                "task-archive: skipping never-surfaced task %s on %s",
                task.id,
                event.type,
            )
            return

        slug = _safe_slug(task.metadata.get("project"))
        done_path = _done_file(projects_root, slug)

        if slug not in dedup_cache:
            dedup_cache[slug] = _load_done_ids(done_path)
        seen = dedup_cache[slug]

        if task.id in seen:
            # Already archived (cold-start replay, or a duplicate live
            # event). Still set the archived flag so the projection
            # evicts the line if it's somehow back in the global file.
            ctx.logger.debug(
                "task-archive: %s already in %s; skipping append", task.id, done_path
            )
            sync_state.archived[task.id] = True
            await _request_projection_evict()
            return

        status = "completed" if event.type.endswith("completed") else "cancelled"
        resolved_at = _resolved_at_for(event, task)
        line = render_resolved_line(task, status, resolved_at)

        # Append first. On OSError do NOT touch ``archived`` / the cache
        # and re-raise → the runner retries, then posts a [Friction]
        # finding. The task stays in the global file (TTL fallback) until
        # the append eventually succeeds (no-data-loss guarantee).
        _append_line(done_path, line)

        seen.add(task.id)
        sync_state.archived[task.id] = True
        # Bounded-memory cleanup: the surfaced flag has done its job for
        # this task now that it's terminal + archived.
        sync_state.surfaced.pop(task.id, None)
        ctx.logger.info("task-archive: appended %s to %s", task.id, done_path.name)
        # Ask the projection to flush now that ``archived`` is set, so the
        # line is evicted from tasks.md causally (not racing the debounce
        # timer). No-op when no projection is wired.
        await _request_projection_evict()

    async def _request_projection_evict() -> None:
        reflush = sync_state.request_projection_flush
        if reflush is not None:
            await reflush()

    return handle
