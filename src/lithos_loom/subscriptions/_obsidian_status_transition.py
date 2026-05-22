"""``obsidian-status-transition`` subscription handler (Slice 2 US17+).

Consumes ``obsidian.task.status_changed`` events emitted by
:class:`~lithos_loom.sources.obsidian_fs_watcher.ObsidianFsWatcher`
and pushes the matching action to Lithos:

* ``("[ ]", "[x]")`` → :meth:`LithosClient.task_complete` (US17)
* ``("[ ]", "[-]")`` → :meth:`LithosClient.task_cancel`   (US18 — follow-up)
* ``("[x]", "[ ]")`` → ``[ReopenRequested]`` finding      (US19 — follow-up)
* ``("[ ]", "[/]")`` / ``("[ ]", "[>]")`` — silent no-op  (US20)
* anything else — silent no-op with a debug log

The dispatch table is small and grows by one row per follow-up
story. Unknown ``(prior, new)`` pairs naturally fall through to a
debug-log skip, which is exactly the behaviour US20 specifies for
``[/]``/``[>]`` markers; no special case needed.

The handler is **stateless** — no factory, no closure. Mirrors
:mod:`._noop`'s shape and contrasts with
:func:`._obsidian_projection.make_handler` which carries per-handler
state. The obsidian-sync child wires this module's :func:`handle`
directly into its ``my_handlers`` dict.

Idempotency is **not** enforced here. US22 will add a pre-check via
``lithos_task_status``; until then, calling ``task_complete`` on an
already-completed task raises a :class:`LithosClientError` which the
:class:`SubscriptionRunner` retry-then-friction path absorbs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from lithos_loom.bus import Event
from lithos_loom.subscriptions import SubscriptionContext

__all__ = ["handle"]


TransitionFn = Callable[[str, SubscriptionContext], Awaitable[None]]


async def _complete(task_id: str, ctx: SubscriptionContext) -> None:
    """US17: ``[ ] → [x]`` — Obsidian tick → Lithos complete."""
    await ctx.lithos.task_complete(task_id=task_id, agent=ctx.agent_id)
    ctx.logger.info(
        "obsidian-status-transition: completed task %s via Obsidian tick",
        task_id,
    )


# Dispatch table. Each entry maps a ``(prior, new)`` checkbox-marker
# pair to the coroutine that pushes the corresponding action to Lithos.
# US18/19 add one row each; US20's silent no-op for ``[/]`` and ``[>]``
# falls out of the missing-entry path below — no explicit row needed.
_TRANSITIONS: dict[tuple[str, str], TransitionFn] = {
    ("[ ]", "[x]"): _complete,
}


async def handle(event: Event, ctx: SubscriptionContext) -> None:
    """Dispatch a single ``obsidian.task.status_changed`` event."""
    payload = event.payload
    try:
        task_id = str(payload["task_id"])
        prior = str(payload["prior"])
        new = str(payload["new"])
    except (KeyError, TypeError) as exc:
        ctx.logger.warning(
            "obsidian-status-transition: malformed payload for %s: %r",
            event.type,
            exc,
        )
        return

    fn = _TRANSITIONS.get((prior, new))
    if fn is None:
        # Unrecognised transition — includes the US20 cases (`[/]`,
        # `[>]`) and anything weird the user might type. Debug-log
        # for visibility but don't emit any side effect; the future
        # status-transition rows fill in via the dispatch table, not
        # by changing this fall-through.
        ctx.logger.debug(
            "obsidian-status-transition: no handler for transition "
            "%s→%s on task %s; skipping",
            prior,
            new,
            task_id,
        )
        return

    await fn(task_id, ctx)
