"""``obsidian-priority-changed`` subscription handler.

Consumes ``obsidian.task.priority_changed`` events emitted by
:class:`~lithos_loom.sources.obsidian_fs_watcher.ObsidianFsWatcher`
when the user edits the priority emoji on a projected line, and
pushes the change to Lithos via
``lithos_task_update(task_id, metadata={"priority": <enum>})``.

The handler is **stateless** — mirrors :mod:`._noop` and
:mod:`._obsidian_status_transition`. The obsidian-sync child wires
this module's :func:`handle` directly into its ``my_handlers`` dict.

**Priority enum**: ``"highest"`` / ``"high"`` / ``"medium"`` /
``"low"`` / ``"lowest"`` or ``None`` for "no priority". The fs watcher
emits ``prior`` and ``new`` as enum strings (not emoji literals), so
the handler doesn't need the emoji-to-enum mapping; it just forwards
``new`` into the metadata patch.

**Clearing a priority.** When the user deletes the emoji entirely
(``new=None``), the handler sends ``metadata={"priority": None}``.
Per Lithos's additive-per-key merge semantics (spec §5.4, post
lithos#290), a ``null`` value deletes the key from
``task.metadata``. Other metadata keys (``depends_on``,
``scheduled_for``, ``story_doc_id``, etc.) are preserved
unconditionally.

Idempotency
-----------

Re-firing for an unchanged priority must be a no-op so source-replay
on restart is safe. This is delivered jointly by the fs-watcher and
this handler, defence-in-depth:

1. **Source-side (load-bearing for cold-start replay).** The watcher's
   poll loop gates every emission on ``prior_status is None: continue``
   (``obsidian_fs_watcher.py``, layer-3 loop). On a cold-start restart,
   both ``_observed_priorities`` and ``sync_state.task_priority_markers``
   are empty for every task, so the status-side check fires first and the
   ``continue`` short-circuits the entire iteration — including the
   priority diff. No priority_changed events are emitted on cold start.

2. **Handler-side payload short-circuit (cheap).** If a third-party
   producer ever publishes a priority_changed event with ``prior == new``
   (or the degenerate ``None → None`` case), the payload-only check below
   catches it before any RPC.

3. **Handler-side Lithos pre-check.** Calls ``task_get`` and skips when
   ``current.metadata.get("priority")`` already matches ``new``. Catches
   the case where the watcher emits a genuine ``prior != new`` but Lithos
   already has ``new`` as the canonical priority — e.g. another agent
   updated the priority directly between watcher emission and handler
   dispatch, or the sync_state baseline drifted from Lithos truth.

The three layers are ordered cheapest-first: payload check (no I/O) →
Lithos read → Lithos write. If Lithos reports the task as
``task_not_found`` (returned as ``None`` from ``task_get``), the handler
logs and skips rather than letting the subsequent ``task_update`` fail.
"""

from __future__ import annotations

from lithos_loom.bus import Event
from lithos_loom.subscriptions import SubscriptionContext

__all__ = ["handle"]


async def handle(event: Event, ctx: SubscriptionContext) -> None:
    """Dispatch a single ``obsidian.task.priority_changed`` event."""
    payload = event.payload
    try:
        task_id = str(payload["task_id"])
        prior = payload["prior"]
        new = payload["new"]
    except (KeyError, TypeError) as exc:
        ctx.logger.warning(
            "obsidian-priority-changed: malformed payload for %s: %r",
            event.type,
            exc,
        )
        return

    # Source emits prior/new as ``str | None``; coerce defensively in
    # case a third party publishes the event with a non-string value.
    prior_str: str | None = str(prior) if prior is not None else None
    new_str: str | None = str(new) if new is not None else None

    # Layer 2: payload-only short-circuit. Free (no I/O); catches
    # degenerate ``prior == new`` publishes before reaching the
    # Lithos-side pre-check below. The fs-watcher won't naturally
    # emit such events in steady state.
    if prior_str == new_str:
        ctx.logger.info(
            "obsidian-priority-changed: payload prior==new (%s); "
            "skipping idempotent update for task %s",
            prior_str,
            task_id,
        )
        return

    # Layer 3: Lithos-side strict pre-check. Reads the canonical task
    # and skips when ``metadata.priority`` already matches ``new`` —
    # catches the case where the watcher emits a genuine prior!=new but
    # Lithos already has the new value (another agent updated it, or
    # sync_state drifted from Lithos truth). Uses ``task_get`` since
    # claims aren't needed here.
    current = await ctx.lithos.task_get(task_id=task_id)
    if current is None:
        ctx.logger.info(
            "obsidian-priority-changed: task %s not found in Lithos "
            "(possibly deleted); skipping",
            task_id,
        )
        return
    current_priority = current.metadata.get("priority")
    if current_priority == new_str:
        ctx.logger.info(
            "obsidian-priority-changed: task %s already at priority %s; "
            "skipping idempotent update",
            task_id,
            new_str,
        )
        return

    # Per-key merge patch. ``None`` deletes the priority key entirely
    # (Lithos JSON-null delete semantics); a string value sets it.
    # Other metadata keys are untouched.
    await ctx.lithos.task_update(
        task_id=task_id,
        agent=ctx.agent_id,
        metadata={"priority": new_str},
    )
    ctx.logger.info(
        "obsidian-priority-changed: updated task %s priority (%s → %s)",
        task_id,
        prior_str,
        new_str,
    )
