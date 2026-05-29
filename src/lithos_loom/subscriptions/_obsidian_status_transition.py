"""``obsidian-status-transition`` subscription handler.

Consumes ``obsidian.task.status_changed`` events emitted by
:class:`~lithos_loom.sources.obsidian_fs_watcher.ObsidianFsWatcher`
and pushes the matching action to Lithos:

* ``("[ ]", "[x]")`` → :meth:`LithosClient.task_complete`
* ``("[ ]", "[-]")`` → :meth:`LithosClient.task_cancel`
* ``("[x]", "[ ]")`` → :meth:`LithosClient.finding_post` with the
  ``[ReopenRequested]`` prefix — workaround until upstream
  ``agent-lore/lithos#243`` adds a real ``task_reopen``
* ``("[ ]", "[/]")`` / ``("[ ]", "[>]")`` — silent no-op
* anything else — silent no-op with a debug log

Unknown ``(prior, new)`` pairs naturally fall through to the debug-log
skip path; no special case needed.

The handler is **stateless** — no factory, no closure. Mirrors
:mod:`._noop`'s shape and contrasts with
:func:`._obsidian_projection.make_handler` which carries per-handler
state. The obsidian-sync child wires this module's :func:`handle`
directly into its ``my_handlers`` dict.

Idempotency pre-check
---------------------

Before invoking any transition function, :func:`handle` calls
:meth:`LithosClient.task_get` once to read the task's current state
from Lithos and passes the resulting :class:`Task` down to the
transition function. Each function then runs its own skip predicate so
the action is co-located with the predicate that guards it:

* :func:`_complete` skips when ``status`` ∈ ``{completed, cancelled}``
  (the task is already terminal).
* :func:`_cancel` skips when ``status`` ∈ ``{completed, cancelled}``
  (likewise terminal).
* :func:`_reopen_request` skips when ``status != "completed"`` —
  posting a ``[ReopenRequested]`` finding on an already-open task is
  nonsensical; this is the projection-lag case where the user unticked a
  line that Lithos was already showing as open.

``task_get`` returning ``None`` (task deleted upstream) is treated as a
skip for all three transitions.

The pre-check costs one extra RPC per dispatched event but guarantees
source-replay on daemon restart is silent — re-reading
``_lithos/tasks.md`` after a restart no longer drives ``task_complete``
/ ``task_cancel`` / ``finding_post`` calls that would otherwise fail or
be redundant.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from lithos_loom.bus import Event
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions import SubscriptionContext

__all__ = ["handle"]


TransitionFn = Callable[[str, Task, SubscriptionContext], Awaitable[None]]


# Terminal statuses: a task in any of these cannot meaningfully be
# completed or cancelled again. Used by both _complete and _cancel.
_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "cancelled"})


async def _complete(task_id: str, current: Task, ctx: SubscriptionContext) -> None:
    """``[ ] → [x]`` — Obsidian tick → Lithos complete.

    Pre-check: skip when the task is already terminal.
    """
    if current.status in _TERMINAL_STATUSES:
        ctx.logger.info(
            "obsidian-status-transition: task %s already %s; "
            "idempotent skip of [ ]→[x]",
            task_id,
            current.status,
        )
        return
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


async def _cancel(task_id: str, current: Task, ctx: SubscriptionContext) -> None:
    """``[ ] → [-]`` — Obsidian cancel marker → Lithos cancel.

    Pre-check: skip when the task is already terminal.
    """
    if current.status in _TERMINAL_STATUSES:
        ctx.logger.info(
            "obsidian-status-transition: task %s already %s; "
            "idempotent skip of [ ]→[-]",
            task_id,
            current.status,
        )
        return
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


async def _reopen_request(
    task_id: str, current: Task, ctx: SubscriptionContext
) -> None:
    """``[x] → [ ]`` — Obsidian untick on completed task →
    ``[ReopenRequested]`` finding.

    Workaround until upstream ``agent-lore/lithos#243`` adds a real
    ``task_reopen``. The finding gives lithos-lens (and the operator) a
    signal that the completed task should be revisited. The Lithos task
    itself stays in ``status=completed`` — no automatic reopen.

    Pre-check: skip when the task is NOT in ``status=completed``.
    Posting ``[ReopenRequested]`` on an open task is nonsensical
    (the projection must have lagged). On a cancelled task it's also
    misleading; we only post on genuinely completed tasks.
    """
    if current.status != "completed":
        ctx.logger.info(
            "obsidian-status-transition: task %s is %s (not completed); "
            "skipping [ReopenRequested] for [x]→[ ]",
            task_id,
            current.status,
        )
        return
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
# Silent no-ops for ``[/]`` and ``[>]`` fall out of the missing-entry
# path below — no explicit row needed.
_TRANSITIONS: dict[tuple[str, str], TransitionFn] = {
    ("[ ]", "[x]"): _complete,
    ("[ ]", "[-]"): _cancel,
    ("[x]", "[ ]"): _reopen_request,
}


async def handle(event: Event, ctx: SubscriptionContext) -> None:
    """Dispatch a single ``obsidian.task.status_changed`` event.

    Calls :meth:`LithosClient.task_get` once before invoking the
    transition function. The dispatch-table miss path short-circuits
    before the pre-check (so silent no-op cases don't incur the RPC).
    """
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
        # Unrecognised transition — includes the ``[/]`` and ``[>]``
        # no-op cases and anything unusual the user might type. Debug-log
        # for visibility but don't emit any side effect; new transitions
        # fill in via the dispatch table, not by changing this fall-through.
        ctx.logger.debug(
            "obsidian-status-transition: no handler for transition "
            "%s→%s on task %s; skipping",
            prior,
            new,
            task_id,
        )
        return

    # One task_get RPC per dispatched event, regardless of which
    # transition fires. Each transition fn then runs its own skip
    # predicate against ``current.status``.
    current = await ctx.lithos.task_get(task_id=task_id)
    if current is None:
        ctx.logger.info(
            "obsidian-status-transition: task %s not found in Lithos "
            "(possibly deleted); skipping %s→%s",
            task_id,
            prior,
            new,
        )
        return

    await fn(task_id, current, ctx)
