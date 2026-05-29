"""In-process pub/sub bus for sources and subscribers.

One bus instance lives inside each supervisor child (e.g. the route-runner
child or the obsidian-sync child). Sources publish typed :class:`Event`
objects; subscribers receive the ones whose type, structural
:class:`MatchFilter`, and optional ``where`` predicate all match.

Delivery is **fire-and-forget with bounded queues**:

* Each subscription has its own ``asyncio.Queue`` sized at subscribe time.
* :meth:`EventBus.publish` writes into each matching queue with
  ``put_nowait``; if a queue is full, the event is dropped and the
  subscription's :attr:`Subscription.drop_count` is incremented.
* A slow subscriber therefore never stalls the publisher or its siblings.

The bus does not own consumer tasks — subscribers run their own consumer
loops over ``Subscription.queue``. The bus only owns the registry and the
queues.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

__all__ = [
    "Event",
    "EventBus",
    "MatchFilter",
    "Subscription",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    """A typed pub/sub event.

    ``type`` is a dotted name like ``lithos.task.created`` or
    ``obsidian.task.toggled``. ``payload`` is event-type-specific; the bus
    treats it as opaque data passed by reference.
    """

    type: str
    timestamp: datetime
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class MatchFilter:
    """Structural filter against an :class:`Event`'s ``payload``.

    Each top-level key in ``fields`` is looked up in the event payload:

    * If the filter value is a ``Mapping``, recursively match against the
      payload's value (which must also be a mapping).
    * If the filter value is a ``list``, the payload value must be a list
      that contains *every* item in the filter (set-superset semantics).
    * Otherwise the filter value and payload value must compare equal.

    A missing key in the payload is a non-match.
    """

    fields: Mapping[str, Any]

    def matches(self, event: Event) -> bool:
        return _matches_struct(self.fields, event.payload)


def _matches_struct(filter_: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    for key, expected in filter_.items():
        if key not in payload:
            return False
        actual = payload[key]
        if isinstance(expected, Mapping):
            if not isinstance(actual, Mapping):
                return False
            if not _matches_struct(expected, actual):
                return False
        elif isinstance(expected, list):
            if not isinstance(actual, list):
                return False
            if not all(item in actual for item in expected):
                return False
        elif actual != expected:
            return False
    return True


@dataclass
class Subscription:
    """Handle returned by :meth:`EventBus.subscribe`.

    Consumers read events from :attr:`queue`. :attr:`drop_count` is bumped
    each time a matching event is dropped because the queue was full.
    """

    event_types: frozenset[str]
    queue: asyncio.Queue[Event]
    match: MatchFilter | None = None
    where: Callable[[Event], bool] | None = None
    drop_count: int = 0
    name: str | None = None

    def matches(self, event: Event) -> bool:
        if event.type not in self.event_types:
            return False
        if self.match is not None and not self.match.matches(event):
            return False
        if self.where is not None:
            try:
                if not self.where(event):
                    return False
            except Exception:
                # Buggy predicate must not poison sibling subscribers.
                logger.exception(
                    "subscription %s where-predicate raised; treating as no match",
                    self.name or "<anonymous>",
                )
                return False
        return True


class EventBus:
    """Async fan-out registry for in-process pub/sub.

    Not thread-safe: all calls must run on the same asyncio event loop.
    """

    def __init__(self) -> None:
        self._subscriptions: list[Subscription] = []

    def subscribe(
        self,
        *,
        event_types: Sequence[str],
        match: Mapping[str, Any] | None = None,
        where: Callable[[Event], bool] | None = None,
        queue_size: int = 1000,
        name: str | None = None,
    ) -> Subscription:
        """Register a new subscription and return its handle.

        ``event_types`` is the allow-list of dotted event types; only those
        events are considered. ``match`` is the structural filter table;
        ``where`` is an optional Python predicate evaluated last.

        ``queue_size`` must be >= 1 — unbounded queues defeat the
        fire-and-forget bounded-buffer guarantee.

        ``match`` is deep-copied so subsequent mutation of the caller's
        dict (or any nested dict/list within it) cannot silently change
        routing behaviour after subscribe-time.
        """
        if queue_size < 1:
            raise ValueError(
                f"queue_size must be >= 1 (got {queue_size}); unbounded "
                "queues are not allowed (fire-and-forget bounded buffers)"
            )
        sub = Subscription(
            event_types=frozenset(event_types),
            queue=asyncio.Queue(maxsize=queue_size),
            match=MatchFilter(copy.deepcopy(dict(match))) if match else None,
            where=where,
            name=name,
        )
        self._subscriptions.append(sub)
        return sub

    async def publish(self, event: Event) -> None:
        """Fan out ``event`` to every matching subscription.

        Never blocks: each subscriber's queue receives via ``put_nowait``,
        and full queues drop the event with a ``drop_count`` bump.
        """
        for sub in self._subscriptions:
            if not sub.matches(event):
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                sub.drop_count += 1
                logger.debug(
                    "bus: dropped %s for subscription %s (drop_count=%d)",
                    event.type,
                    sub.name or "<anonymous>",
                    sub.drop_count,
                )

    @property
    def subscriptions(self) -> tuple[Subscription, ...]:
        return tuple(self._subscriptions)
