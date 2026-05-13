"""Tests for ``lithos_loom.sources.lithos_poller`` (Slice 0 US3).

The poller fetches Lithos tasks at a configured interval, diffs the
returned list against an in-memory snapshot, and publishes
``lithos.task.*`` events for each transition. Tests inject a fake client
rather than the real ``LithosClient`` so the poller's diff logic is
exercised without an HTTP round trip — see ``test_lithos_client.py`` for
the client surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from lithos_loom.bus import EventBus, Subscription
from lithos_loom.lithos_client import Task
from lithos_loom.sources.lithos_poller import LithosPoller

# ── Test helpers ────────────────────────────────────────────────────────


def _task(
    id_: str,
    *,
    status: str = "open",
    tags: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
    claims: tuple[Mapping[str, Any], ...] = (),
    title: str = "t",
) -> Task:
    return Task(
        id=id_,
        title=title,
        status=status,
        tags=tags,
        metadata=metadata or {},
        claims=claims,
    )


class FakePoller:
    """Records the script of polls.

    Each ``task_list`` call dequeues the next entry from ``polls``; entries
    can be either a ``list[Task]`` (success) or an ``Exception`` (raised).
    When the script is exhausted the next call returns ``[]``.

    ``status_responses`` maps task_id → response for the follow-up
    ``task_status`` call: a ``Task`` (typically with status="completed" or
    "cancelled"), ``None`` (task_not_found), or an ``Exception`` (raised).
    Unknown ids default to ``None``.
    """

    def __init__(
        self,
        polls: list[list[Task] | Exception],
        status_responses: dict[str, Task | None | Exception] | None = None,
    ) -> None:
        self._polls = list(polls)
        self._status_responses = dict(status_responses or {})
        self.calls: list[dict[str, Any]] = []
        self.status_calls: list[str] = []

    async def task_list(
        self,
        *,
        status: str | None = None,
        with_claims: bool = False,
    ) -> list[Task]:
        self.calls.append({"status": status, "with_claims": with_claims})
        if not self._polls:
            return []
        nxt = self._polls.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def task_status(self, *, task_id: str) -> Task | None:
        self.status_calls.append(task_id)
        nxt = self._status_responses.get(task_id)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _drain(sub: Subscription) -> list[str]:
    """Drain a subscription's queue to a list of event types (in order)."""
    out: list[str] = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait().type)
    return out


# ── Per-tick diff/emit semantics ────────────────────────────────────────


async def test_poll_once_first_tick_emits_created_per_open_task() -> None:
    bus = EventBus()
    listener = bus.subscribe(
        event_types=[
            "lithos.task.created",
            "lithos.task.updated",
            "lithos.task.claimed",
            "lithos.task.released",
        ]
    )
    client = FakePoller([[_task("a"), _task("b"), _task("c")]])
    poller = LithosPoller(client=client, bus=bus, interval=0.0)

    await poller.poll_once()

    assert _drain(listener) == [
        "lithos.task.created",
        "lithos.task.created",
        "lithos.task.created",
    ]


async def test_poll_once_filters_to_open_tasks_via_status_arg() -> None:
    """The poller MUST query Lithos with status='open' (US3 contract).

    Regression test for a divergence where the poller called task_list
    with no status filter, broadening to 'all tasks'. Story 3 explicitly
    says the source polls open tasks; ensuring the wire-level argument
    matches the story keeps emission semantics aligned with the PRD.
    """
    bus = EventBus()
    client = FakePoller([[]])
    poller = LithosPoller(client=client, bus=bus, interval=0.0)
    await poller.poll_once()
    assert client.calls[0]["status"] == "open"
    assert client.calls[0]["with_claims"] is True


async def test_poll_once_emits_completed_when_disappeared_task_is_completed() -> None:
    """Task disappears from open set; task_status reports completed.

    The poller follows up with a single task_status call and emits the
    canonical lithos.task.completed event. Slice 1's obsidian-projection
    subscription depends on this contract.
    """
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.completed"])
    completed_task = _task("b", status="completed")
    client = FakePoller(
        polls=[[_task("a"), _task("b")], [_task("a")]],
        status_responses={"b": completed_task},
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.0)

    await poller.poll_once()
    _drain(listener)  # discard initial created events
    await poller.poll_once()

    assert _drain(listener) == ["lithos.task.completed"]
    assert client.status_calls == ["b"]


async def test_poll_once_emits_cancelled_when_disappeared_task_is_cancelled() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.cancelled"])
    cancelled_task = _task("b", status="cancelled")
    client = FakePoller(
        polls=[[_task("a"), _task("b")], [_task("a")]],
        status_responses={"b": cancelled_task},
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.0)

    await poller.poll_once()
    _drain(listener)
    await poller.poll_once()

    assert _drain(listener) == ["lithos.task.cancelled"]


async def test_poll_once_silently_handles_deleted_disappeared_task() -> None:
    """task_status returning None (task_not_found) is not a transition."""
    bus = EventBus()
    listener = bus.subscribe(
        event_types=[
            "lithos.task.created",
            "lithos.task.updated",
            "lithos.task.completed",
            "lithos.task.cancelled",
            "lithos.task.claimed",
            "lithos.task.released",
        ]
    )
    client = FakePoller(
        polls=[[_task("a"), _task("b")], [_task("a")]],
        status_responses={"b": None},  # task deleted
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.0)

    await poller.poll_once()
    _drain(listener)
    await poller.poll_once()

    # No completed/cancelled emission for a deleted task — only the
    # original created events from the first poll.
    assert _drain(listener) == []
    assert client.status_calls == ["b"]


async def test_poll_once_silently_handles_disappeared_task_back_to_open() -> None:
    """Race: task transitioned out and back in between polls.

    task_status reports the task is open again; the next poll will pick
    it up via the normal open-task path. No terminal-state event fires.
    """
    bus = EventBus()
    listener = bus.subscribe(
        event_types=[
            "lithos.task.completed",
            "lithos.task.cancelled",
        ]
    )
    reopened = _task("b", status="open")
    client = FakePoller(
        polls=[[_task("a"), _task("b")], [_task("a")]],
        status_responses={"b": reopened},
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.0)

    await poller.poll_once()
    await poller.poll_once()

    assert _drain(listener) == []


async def test_poll_once_does_not_call_task_status_when_nothing_disappeared() -> None:
    """Stable poll → zero task_status calls; preserves the cheap-path budget."""
    bus = EventBus()
    same = _task("a", tags=("x",))
    client = FakePoller(polls=[[same], [same]])
    poller = LithosPoller(client=client, bus=bus, interval=0.0)

    await poller.poll_once()
    await poller.poll_once()

    assert client.status_calls == []


async def test_poll_once_emits_updated_when_tags_change() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.updated"])
    client = FakePoller(
        [
            [_task("a", tags=("v1",))],
            [_task("a", tags=("v1", "v2"))],
        ]
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.0)

    await poller.poll_once()
    await poller.poll_once()

    assert _drain(listener) == ["lithos.task.updated"]


async def test_poll_once_emits_claimed_when_claim_appears() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.claimed"])
    claim = {"agent": "a1", "aspect": "impl", "expires_at": "2026-01-01T00:00:00Z"}
    client = FakePoller(
        [
            [_task("a", claims=())],
            [_task("a", claims=(claim,))],
        ]
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.0)
    await poller.poll_once()
    await poller.poll_once()
    assert _drain(listener) == ["lithos.task.claimed"]


async def test_poll_once_emits_released_when_claim_disappears() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.released"])
    claim = {"agent": "a1", "aspect": "impl", "expires_at": "2026-01-01T00:00:00Z"}
    client = FakePoller(
        [
            [_task("a", claims=(claim,))],
            [_task("a", claims=())],
        ]
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.0)
    await poller.poll_once()
    await poller.poll_once()
    assert _drain(listener) == ["lithos.task.released"]


async def test_poll_once_skips_unchanged_tasks() -> None:
    bus = EventBus()
    listener = bus.subscribe(
        event_types=[
            "lithos.task.created",
            "lithos.task.updated",
            "lithos.task.claimed",
            "lithos.task.released",
        ]
    )
    same = _task("a", tags=("x",))
    client = FakePoller([[same], [same]])
    poller = LithosPoller(client=client, bus=bus, interval=0.0)

    await poller.poll_once()  # created
    pre_drain = _drain(listener)
    await poller.poll_once()  # nothing should happen
    post_drain = _drain(listener)

    assert pre_drain == ["lithos.task.created"]
    assert post_drain == []


# ── run() lifecycle ─────────────────────────────────────────────────────


async def test_run_loops_until_cancelled() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    client = FakePoller([[_task("a")], [_task("b")], [_task("c")]])
    poller = LithosPoller(client=client, bus=bus, interval=0.001)

    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # At least two polls should have happened in 50ms.
    types = _drain(listener)
    assert len(types) >= 2
    assert all(t == "lithos.task.created" for t in types)


async def test_run_continues_through_transient_client_error() -> None:
    bus = EventBus()
    listener = bus.subscribe(event_types=["lithos.task.created"])
    client = FakePoller(
        [
            RuntimeError("transient network blip"),
            [_task("a")],
        ]
    )
    poller = LithosPoller(client=client, bus=bus, interval=0.001)

    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The poller recovered from the first poll's error and emitted created
    # for "a" on a later iteration.
    assert "lithos.task.created" in _drain(listener)
