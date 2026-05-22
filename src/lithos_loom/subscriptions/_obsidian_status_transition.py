"""``obsidian-status-transition`` subscription handler (Slice 2 US17+).

Consumes ``obsidian.task.status_changed`` events emitted by
:class:`~lithos_loom.sources.obsidian_fs_watcher.ObsidianFsWatcher`
and pushes the matching action to Lithos:

* ``("[ ]", "[x]")`` → :meth:`LithosClient.task_complete` (US17)
* ``("[ ]", "[-]")`` → :meth:`LithosClient.task_cancel`   (US18)
* ``("[x]", "[ ]")`` → :meth:`LithosClient.finding_post` with the
  ``[ReopenRequested]`` prefix — D17 workaround until upstream
  ``agent-lore/lithos#243`` adds a real ``task_reopen`` (US19)
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
``lithos_task_status``; until then, calling ``task_complete`` /
``task_cancel`` on an already-resolved task raises a
:class:`LithosClientError` which the :class:`SubscriptionRunner`
retry-then-friction path absorbs, and posting ``[ReopenRequested]``
on an already-open task is harmless but redundant.
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


# Constant reason passed alongside the Lithos cancel call. The Lithos
# spec says ``reason`` is accepted by the MCP surface but not persisted
# in SQLite, so this is breadcrumb-only — surfaces in MCP-level logs
# and traces and identifies the origin of the cancel for anyone
# inspecting them.
_CANCEL_REASON = "cancelled via Obsidian status flip"


async def _cancel(task_id: str, ctx: SubscriptionContext) -> None:
    """US18: ``[ ] → [-]`` — Obsidian cancel marker → Lithos cancel."""
    await ctx.lithos.task_cancel(
        task_id=task_id,
        agent=ctx.agent_id,
        reason=_CANCEL_REASON,
    )
    ctx.logger.info(
        "obsidian-status-transition: cancelled task %s via Obsidian flip",
        task_id,
    )


# Constant summary for the [ReopenRequested] finding. Lithos doesn't
# yet have ``task_reopen`` (upstream ``agent-lore/lithos#243``), so an
# untick can't actually reopen the task — instead we post a finding
# that lithos-lens and the operator can pick up as a signal to revisit.
# The ``[ReopenRequested]`` prefix follows the project's stable-prefix
# convention (mirrors ``[Friction]`` / ``[BlockerFailed]`` shapes
# elsewhere in the codebase).
_REOPEN_REQUEST_SUMMARY = "[ReopenRequested] task untoggled in Obsidian"


async def _reopen_request(task_id: str, ctx: SubscriptionContext) -> None:
    """US19: ``[x] → [ ]`` — Obsidian untick on completed task →
    ``[ReopenRequested]`` finding.

    Workaround for D17 — Lithos doesn't yet have ``task_reopen``
    (upstream ``agent-lore/lithos#243``). The finding gives lithos-lens
    (and the operator) a signal that the completed task should be
    revisited. The Lithos task itself stays in ``status=completed`` —
    no automatic reopen. When #243 ships, this row can be replaced
    with a real reopen call.

    No pre-check that the task is actually in ``status=completed`` —
    if the projection lagged and the task is still ``open`` in Lithos,
    the finding still posts (harmless but redundant). US22 adds the
    broader idempotency pre-check via ``lithos_task_status``.
    """
    await ctx.lithos.finding_post(
        task_id=task_id,
        summary=_REOPEN_REQUEST_SUMMARY,
        agent=ctx.agent_id,
    )
    ctx.logger.info(
        "obsidian-status-transition: posted [ReopenRequested] for "
        "task %s (untick from [x])",
        task_id,
    )


# Dispatch table. Each entry maps a ``(prior, new)`` checkbox-marker
# pair to the coroutine that pushes the corresponding action to Lithos.
# US20's silent no-op for ``[/]`` and ``[>]`` falls out of the missing-
# entry path below — no explicit row needed.
_TRANSITIONS: dict[tuple[str, str], TransitionFn] = {
    ("[ ]", "[x]"): _complete,
    ("[ ]", "[-]"): _cancel,
    ("[x]", "[ ]"): _reopen_request,
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
