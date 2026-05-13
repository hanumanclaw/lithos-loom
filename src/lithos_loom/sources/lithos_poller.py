"""LithosPoller — periodic ``lithos_task_list(status="open")`` source (Slice 0 US3).

Per the user story this source polls **open** tasks; per the broader
contract it normalises *all* observed task state changes into
``lithos.task.*`` events. The two are reconciled by treating the open-task
list as the discovery surface and following up with a ``task_status`` call
per disappearance to determine whether the task transitioned to
``completed`` or ``cancelled``.

Emitted event types:

* ``lithos.task.created`` — id newly seen in the open set this poll
* ``lithos.task.updated`` — same id, content changed (tags, title, metadata)
* ``lithos.task.claimed`` — claims went empty → non-empty
* ``lithos.task.released`` — claims went non-empty → empty
* ``lithos.task.completed`` — id disappeared from open set, ``task_status``
  reports it as completed
* ``lithos.task.cancelled`` — id disappeared from open set, ``task_status``
  reports it as cancelled

When ``task_status`` reports ``task_not_found`` (the task was deleted
entirely) or finds the task back in the open state (a brief race between
polls), no event is emitted.

D11/D13 make this a re-authoritative source: on daemon restart the first
poll replays whatever the current open-task list looks like, and
subscribers are responsible for idempotency.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol

from lithos_loom.bus import Event, EventBus
from lithos_loom.lithos_client import Task

__all__ = ["LithosPoller", "PollerClient"]

logger = logging.getLogger(__name__)


class PollerClient(Protocol):
    """Minimum surface the poller depends on. Lets tests inject a fake."""

    async def task_list(
        self,
        *,
        status: str | None = None,
        with_claims: bool = False,
    ) -> list[Task]: ...

    async def task_status(self, *, task_id: str) -> Task | None: ...


@dataclass
class LithosPoller:
    client: PollerClient
    bus: EventBus
    interval: float = 30.0

    def __post_init__(self) -> None:
        self._snapshot: dict[str, Task] = {}
        self._first_poll: bool = True

    async def run(self) -> None:
        """Poll forever at ``interval`` seconds. Cancellable."""
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("LithosPoller: poll_once raised; retrying after sleep")
            await asyncio.sleep(self.interval)

    async def poll_once(self) -> None:
        """One poll iteration. Useful in tests + for manual triggering."""
        tasks = await self.client.task_list(status="open", with_claims=True)
        new_snapshot = {t.id: t for t in tasks}

        # Disappeared ids first — they need a follow-up status call to
        # disambiguate completed vs cancelled.
        disappeared = set(self._snapshot) - set(new_snapshot)
        for task_id in disappeared:
            await self._emit_for_disappeared(task_id, self._snapshot[task_id])

        for task in tasks:
            await self._emit_for_task(task)

        self._snapshot = new_snapshot
        self._first_poll = False

    async def _emit_for_task(self, task: Task) -> None:
        prev = self._snapshot.get(task.id)
        if prev is None:
            await self._publish("lithos.task.created", task)
            return

        # Claim transitions.
        if not prev.claims and task.claims:
            await self._publish("lithos.task.claimed", task)
            return
        if prev.claims and not task.claims:
            await self._publish("lithos.task.released", task)
            return

        # Generic content change (tags, title, metadata).
        if task != prev:
            await self._publish("lithos.task.updated", task)

    async def _emit_for_disappeared(self, task_id: str, prev_task: Task) -> None:
        """An id present last poll is gone now — figure out what happened."""
        current = await self.client.task_status(task_id=task_id)
        if current is None:
            # Task deleted entirely; no transition event.
            return
        if current.status not in ("completed", "cancelled"):
            # Race: task came back to open between polls. Next poll will
            # pick it up and emit created (it's not in our snapshot).
            return
        # Build the emission payload from prev_task + the canonical terminal
        # status. task_status doesn't return tags/metadata, so we carry
        # forward what the open-task poll last knew about it.
        terminal_task = Task(
            id=prev_task.id,
            title=current.title or prev_task.title,
            status=current.status,
            tags=prev_task.tags,
            metadata=prev_task.metadata,
            claims=current.claims,
        )
        await self._publish(f"lithos.task.{current.status}", terminal_task)

    async def _publish(self, event_type: str, task: Task) -> None:
        event = Event(
            type=event_type,
            timestamp=datetime.now(UTC),
            payload=_event_payload(task),
        )
        await self.bus.publish(event)


def _event_payload(task: Task) -> Mapping[str, Any]:
    """Project a :class:`Task` into the read-only event payload shape."""
    return MappingProxyType(
        {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "tags": list(task.tags),
            "metadata": dict(task.metadata),
            "claims": [dict(c) for c in task.claims],
        }
    )
