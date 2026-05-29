"""``is_human_actionable`` — the projection filter for Obsidian.

Centralised so the ``obsidian-projection`` handler and any future
sub-systems (digest, doctor warnings) share a single definition of
what counts as "operator-facing work."

A task is projected iff it is open AND (not claimable by any route,
OR claimed by a ``human_blocking = true`` route).

Decision order (cheapest tests first):

1. Operator opt-out for blocked work: if ``include_blocked = false``
   in ``[obsidian_sync]`` and the task carries a non-empty
   ``metadata.depends_on`` list → False.
2. Operator tag denylist: if any of ``task.tags`` is in
   ``cfg.exclude_tags`` → False.
3. Open orphan task: ``task.status == "open"`` AND no configured route
   is claimable against the task — a route is claimable only when every
   tag in its ``match.tags`` is present on the task, matching the
   all-tags semantic the bus matcher enforces. Nothing automated will
   pick it up → True.
4. Claimed by a human_blocking route: any entry in ``task.claims``
   whose ``aspect`` field equals the ``name`` of a route configured
   with ``human_blocking = true`` → True. The route-runner sets
   ``aspect = route.name`` on every claim it makes (see
   ``RouteRunner.run`` in ``subscriptions/route_runner.py``), so this
   is how we tell which route owns the claim.
5. Otherwise → False. This covers:
   - claimable by an autonomous route, not yet claimed (waiting for
     automation; nothing for the operator to do)
   - claimed by an autonomous route (automation is running)
   - claimable by a human_blocking route but not yet claimed (waiting
     for the route-runner to claim it; we wait for the claim before
     projecting so the operator only sees work actually blocked on them)

Pure function with no I/O; trivial to unit-test in isolation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from lithos_loom.config import ObsidianSyncConfig, RouteConfig
from lithos_loom.lithos_client import Task

__all__ = [
    "human_blocking_route_name",
    "is_human_actionable",
    "would_be_actionable",
]


def is_human_actionable(
    task: Task,
    routes: Sequence[RouteConfig],
    cfg: ObsidianSyncConfig,
) -> bool:
    """Return ``True`` iff an open task should appear in the operator's view.

    See module docstring for the decision order. Terminal tasks always
    return ``False`` — the obsidian-projection handler routes resolved
    tasks through :func:`would_be_actionable` instead, which answers
    "would this have been actionable while it was open?" so the TTL-
    lingering window correctly skips never-actionable autonomous work
    (PR #21 review feedback).
    """
    if task.status != "open":
        return False
    return would_be_actionable(task, routes, cfg)


def would_be_actionable(
    task: Task,
    routes: Sequence[RouteConfig],
    cfg: ObsidianSyncConfig,
) -> bool:
    """Return ``True`` iff the task's tags/metadata would make it
    actionable to the operator, *ignoring* its current status.

    Same decision tree as :func:`is_human_actionable` minus the
    status gate. Used by:

    - :func:`is_human_actionable` to compose the live-event decision
      (open + would-be-actionable = actionable).
    - The obsidian-projection handler's terminal-event branch to decide
      whether a completed/cancelled task should join the TTL lingering
      window — a task that was never projected while open
      (autonomous-route work) should not suddenly appear in "done this
      week" queries on completion (PR #21 review).
    - ``LithosEventStream`` bootstrap-resolved over-fetch to filter
      Lithos-discovered resolved tasks before publishing them as bus
      events, so restart-recovery only rehydrates tasks that would have
      been on the operator's view.
    """
    depends_on = task.metadata.get("depends_on") or []
    if not cfg.include_blocked and depends_on:
        return False

    task_tag_set = set(task.tags)
    if task_tag_set & set(cfg.exclude_tags):
        return False

    # A route is "claimable" only when every tag in its match block is
    # present on the task — the same all-tags semantic the bus enforces
    # against ``[[routes]]`` subscriptions. Any-overlap (set & set) would
    # mark multi-tag-route tasks as claimable when they actually wouldn't
    # be claimed, hiding them from Obsidian without ever processing them.
    claimable_routes = [r for r in routes if set(r.match.tags).issubset(task_tag_set)]

    # First disjunct: not claimable by any route → would project as orphan.
    if not claimable_routes:
        return True

    # Second disjunct: claimed by a human_blocking route.
    return human_blocking_route_name(task, routes) is not None


def human_blocking_route_name(
    task: Task,
    routes: Sequence[RouteConfig],
) -> str | None:
    """Name of the ``human_blocking`` route currently claiming ``task``.

    Returns ``None`` when no such claim exists. The claim's ``aspect``
    field carries the route name (see ``RouteRunner.run`` in
    ``subscriptions/route_runner.py``), so we match aspects against
    the names of routes with ``human_blocking = true``.

    Shared by:

    - :func:`is_human_actionable` to satisfy the second disjunct.
    - The ``obsidian-projection`` renderer to emit
      ``#lithos/<route-name>`` and the computed ``today`` date.

    If multiple human-blocking routes have claimed the task (unusual
    in practice), the first match wins; ``task.claims`` ordering is
    Lithos-canonical so the result is stable across calls.
    """
    human_blocking_names = {r.name for r in routes if r.human_blocking}
    if not human_blocking_names:
        return None
    for claim in task.claims:
        aspect = _claim_aspect(claim)
        if aspect is not None and aspect in human_blocking_names:
            return aspect
    return None


def _claim_aspect(claim: Mapping[str, Any] | Any) -> str | None:
    """Pull the ``aspect`` field out of a claim record, defensively.

    The :class:`Task` dataclass declares ``claims`` as a tuple of
    mappings, but the real wire payload is JSON so we treat it as a
    duck-typed mapping. Returns ``None`` for any shape we can't read,
    which falls through to "not a matching claim" in the caller.
    """
    if isinstance(claim, Mapping):
        val = claim.get("aspect")
        if isinstance(val, str):
            return val
    return None
