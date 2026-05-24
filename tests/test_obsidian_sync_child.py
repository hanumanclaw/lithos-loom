"""Tests for ``lithos_loom.children.obsidian_sync`` (Slice 1 US7+US8).

These tests drive ``_amain`` directly with a fabricated ``LoomConfig``
so they don't shell out to subprocess. The supervisor-level
end-to-end gating is exercised in ``test_supervisor.py``.

US8 replaced the SIGTERM-park with a real wiring chain (LithosClient
+ LithosEventStream + SubscriptionRunner). We monkeypatch the client
and source so tests stay in-process without a real Lithos to connect
to; the bus is captured so tests can publish events directly and
observe the projection handler's file writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

from lithos_loom.bus import Event, EventBus
from lithos_loom.children import obsidian_sync as obs_sync_mod
from lithos_loom.children.obsidian_sync import _amain
from lithos_loom.config import (
    LoomConfig,
    ObsidianSyncConfig,
    OrchestratorConfig,
    RetryPolicy,
    SubscriptionConfig,
)
from lithos_loom.lithos_client import Task

# ── Helpers ────────────────────────────────────────────────────────────


async def _cancel_and_drain(task: asyncio.Task[Any]) -> None:
    """Cancel a helper task and await its completion so it can't leak past
    the test (Copilot review on #16)."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _sigterm_soon(delay: float = 0.1) -> None:
    """Send SIGTERM to this process after a short delay. Used to unblock
    the stop_event inside _amain so the test can complete."""
    await asyncio.sleep(delay)
    os.kill(os.getpid(), signal.SIGTERM)


def _cfg_with_obsidian(
    tmp_path: Path,
    *,
    subscriptions: tuple[SubscriptionConfig, ...] = (),
) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        subscriptions=subscriptions,
        obsidian_sync=ObsidianSyncConfig(
            vault_path=tmp_path / "vault",
            tasks_file=Path("_lithos/tasks.md"),
            resolved_ttl_days=7,
            include_blocked=False,
            exclude_tags=("debug:trace",),
        ),
    )


def _cfg_without_obsidian(tmp_path: Path) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
    )


def _projection_subscription(
    name: str = "obsidian-tasks",
    action: str = "obsidian-projection",
) -> SubscriptionConfig:
    return SubscriptionConfig(
        name=name,
        event_types=(
            "lithos.task.created",
            "lithos.task.updated",
            "lithos.task.completed",
            "lithos.task.cancelled",
        ),
        action=action,
        retry=RetryPolicy(attempts=1, initial_delay_seconds=0.0, max_delay_seconds=0.0),
        on_persistent_failure="ignore",
    )


def _status_transition_subscription(
    name: str = "obsidian-tasks-transition",
) -> SubscriptionConfig:
    return SubscriptionConfig(
        name=name,
        event_types=("obsidian.task.status_changed",),
        action="obsidian-status-transition",
        retry=RetryPolicy(attempts=1, initial_delay_seconds=0.0, max_delay_seconds=0.0),
        on_persistent_failure="ignore",
    )


def _priority_changed_subscription(
    name: str = "obsidian-tasks-priority",
) -> SubscriptionConfig:
    return SubscriptionConfig(
        name=name,
        event_types=("obsidian.task.priority_changed",),
        action="obsidian-priority-changed",
        retry=RetryPolicy(attempts=1, initial_delay_seconds=0.0, max_delay_seconds=0.0),
        on_persistent_failure="ignore",
    )


def _event(
    event_type: str,
    *,
    task_id: str,
    title: str = "test task",
    tags: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
) -> Event:
    return Event(
        type=event_type,
        timestamp=datetime.now(UTC),
        payload={
            "id": task_id,
            "title": title,
            "status": "open",
            "tags": list(tags),
            "metadata": dict(metadata or {}),
            "claims": [],
        },
    )


# ── Stubs for LithosClient + LithosEventStream ─────────────────────────


class _StubLithosClient:
    """Async-context-manager stand-in for ``LithosClient``.

    The real client does an MCP/SSE handshake on __aenter__; tests
    can't reach a real Lithos, so we substitute this no-op.

    Records ``task_complete``, ``task_cancel``, ``task_update``, and
    ``finding_post`` invocations on class-level lists so the
    obsidian-status-transition and obsidian-priority-changed
    end-to-end tests can assert on the round-trip. The
    ``_reset_stub_lithos_state`` autouse fixture clears them between
    tests.

    US22 added a ``task_status`` surface so the status-transition
    handler can pre-check Lithos-side state before mutating. Pre-seed
    ``task_status_returns[task_id]`` to either a :class:`Task`
    instance (returned verbatim) or ``None`` (simulates a deleted
    task — ``lithos_task_not_found``). Tests that don't pre-seed
    receive a synthetic open ``Task`` so existing happy-path
    end-to-end tests reach their mutating call without setup
    changes.

    Post-lithos#294 the status-transition and priority-changed
    handlers use ``task_get`` instead of ``task_status`` (lighter:
    no claims). The stub's ``task_get`` reads from the same
    ``task_status_returns`` map so a single per-test pre-seed
    governs both surfaces and the auto-state-transitions on
    ``task_complete`` / ``task_cancel`` remain visible to whichever
    method a handler picks. ``task_get_calls`` records lookups so
    tests can assert which surface was used.
    """

    task_complete_calls: ClassVar[list[dict[str, Any]]] = []
    task_cancel_calls: ClassVar[list[dict[str, Any]]] = []
    task_update_calls: ClassVar[list[dict[str, Any]]] = []
    finding_post_calls: ClassVar[list[dict[str, Any]]] = []
    task_status_returns: ClassVar[dict[str, Task | None]] = {}
    task_status_calls: ClassVar[list[str]] = []
    task_get_calls: ClassVar[list[str]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _StubLithosClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def finding_post(
        self,
        *,
        task_id: str,
        summary: str,
        agent: str | None = None,
        knowledge_id: str | None = None,
    ) -> None:
        type(self).finding_post_calls.append(
            {
                "task_id": task_id,
                "summary": summary,
                "agent": agent,
                "knowledge_id": knowledge_id,
            }
        )

    async def task_complete(self, *, task_id: str, agent: str | None = None) -> None:
        type(self).task_complete_calls.append({"task_id": task_id, "agent": agent})
        # Mirror real Lithos: the task transitions to status=completed.
        # Lets subsequent ``task_status`` pre-checks observe the new
        # state without test-side bookkeeping.
        type(self).task_status_returns[task_id] = Task(
            id=task_id,
            title="t",
            status="completed",
            tags=(),
            metadata={},
            claims=(),
        )

    async def task_cancel(
        self,
        *,
        task_id: str,
        agent: str | None = None,
        reason: str | None = None,
    ) -> None:
        type(self).task_cancel_calls.append(
            {"task_id": task_id, "agent": agent, "reason": reason}
        )
        # Mirror real Lithos: the task transitions to status=cancelled.
        type(self).task_status_returns[task_id] = Task(
            id=task_id,
            title="t",
            status="cancelled",
            tags=(),
            metadata={},
            claims=(),
        )

    async def task_update(
        self,
        *,
        task_id: str,
        agent: str | None = None,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        type(self).task_update_calls.append(
            {
                "task_id": task_id,
                "agent": agent,
                "title": title,
                "description": description,
                "tags": tags,
                "metadata": metadata,
            }
        )

    async def task_status(self, *, task_id: str) -> Task | None:
        """Return the pre-seeded Task for ``task_id``, or a synthetic
        open Task by default. ``task_status_returns[task_id] = None``
        simulates the ``task_not_found`` (deleted-upstream) case.

        Records each lookup in ``task_status_calls`` so US22 round-trip
        tests can assert the pre-check actually ran.
        """
        type(self).task_status_calls.append(task_id)
        cls = type(self)
        if task_id in cls.task_status_returns:
            return cls.task_status_returns[task_id]
        return Task(
            id=task_id,
            title="t",
            status="open",
            tags=(),
            metadata={},
            claims=(),
        )

    async def task_get(self, *, task_id: str) -> Task | None:
        """Post-lithos#294: lightweight single-task fetch (no claims).
        Reads from the same ``task_status_returns`` map as
        :meth:`task_status` so auto-state-transitions on
        ``task_complete`` / ``task_cancel`` remain visible to
        whichever method a handler picks.
        """
        type(self).task_get_calls.append(task_id)
        cls = type(self)
        if task_id in cls.task_status_returns:
            return cls.task_status_returns[task_id]
        return Task(
            id=task_id,
            title="t",
            status="open",
            tags=(),
            metadata={},
            claims=(),
        )


@pytest.fixture(autouse=True)
def _reset_stub_lithos_state() -> None:
    """Clear ``_StubLithosClient`` class-level call records between tests
    so cross-test leakage can't make an assertion accidentally pass."""
    _StubLithosClient.task_complete_calls.clear()
    _StubLithosClient.task_cancel_calls.clear()
    _StubLithosClient.task_update_calls.clear()
    _StubLithosClient.finding_post_calls.clear()
    _StubLithosClient.task_status_returns.clear()
    _StubLithosClient.task_status_calls.clear()
    _StubLithosClient.task_get_calls.clear()


class _StubSource:
    """Stand-in for ``LithosEventStream`` that exposes its bus and idles."""

    def __init__(
        self, *, client: Any, bus: EventBus, events_url: str, **_: Any
    ) -> None:
        self.bus = bus
        self.events_url = events_url

    async def run(self) -> None:
        await asyncio.sleep(3600)  # park; the source contract is "run forever"


@pytest.fixture
def stub_io(monkeypatch: pytest.MonkeyPatch) -> list[EventBus]:
    """Replace LithosClient + LithosEventStream + LithosNoteStream in
    the obsidian_sync module and return a list that captures the bus
    each _StubSource was constructed with — tests publish to
    ``captured_buses[-1]``.

    Slice 4 added the second source; both are stubbed so the wiring
    test can assert which subscriptions cause which sources to spawn
    without touching a real Lithos."""
    captured: list[EventBus] = []

    class _CapturingSource(_StubSource):
        def __init__(self, *, client: Any, bus: EventBus, events_url: str, **kw: Any):
            super().__init__(client=client, bus=bus, events_url=events_url, **kw)
            captured.append(bus)

    monkeypatch.setattr(obs_sync_mod, "LithosClient", _StubLithosClient)
    monkeypatch.setattr(obs_sync_mod, "LithosEventStream", _CapturingSource)
    monkeypatch.setattr(obs_sync_mod, "LithosNoteStream", _CapturingSource)
    return captured


# ── US7 behaviour (still asserted under US8 wiring) ─────────────────────


async def test_obsidian_sync_main_exits_zero_on_stop_event(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """``_amain`` parks until SIGTERM regardless of subscription config."""
    cfg = _cfg_with_obsidian(tmp_path)
    sender = asyncio.create_task(_sigterm_soon())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=2.0)
    finally:
        await _cancel_and_drain(sender)
    assert rc == 0


async def test_obsidian_sync_main_exits_one_when_config_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Defensive guard: ``_amain`` returns 1 when ``obsidian_sync`` is None,
    rather than parking on a stop event under an undefined config.

    No stub_io needed — _amain returns before reaching the LithosClient.
    """
    cfg = _cfg_without_obsidian(tmp_path)
    source_logger = "lithos_loom.children.obsidian_sync"
    with caplog.at_level(logging.ERROR, logger=source_logger):
        rc = await _amain(cfg)
    assert rc == 1
    error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("obsidian-sync spawned without" in m for m in error_msgs), error_msgs


async def test_obsidian_sync_logs_config_on_startup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stub_io: list[EventBus]
) -> None:
    """The startup INFO log names vault / tasks_file / resolved_ttl_days /
    include_blocked / exclude_tags so an operator can grep-confirm."""
    cfg = _cfg_with_obsidian(tmp_path)
    source_logger = "lithos_loom.children.obsidian_sync"

    sender = asyncio.create_task(_sigterm_soon())
    try:
        with caplog.at_level(logging.INFO, logger=source_logger):
            await asyncio.wait_for(_amain(cfg), timeout=2.0)
    finally:
        await _cancel_and_drain(sender)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    started = next((m for m in info_msgs if "obsidian-sync child started" in m), None)
    assert started is not None, f"no startup log; got {info_msgs}"
    assert str(cfg.obsidian_sync.vault_path) in started  # type: ignore[union-attr]
    assert "_lithos/tasks.md" in started
    assert "resolved_ttl_days=7" in started
    assert "include_blocked=False" in started
    assert "debug:trace" in started


# ── US8 wiring ─────────────────────────────────────────────────────────


async def test_obsidian_sync_child_wires_projection_subscription(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """End-to-end through _amain: configure an obsidian-projection
    subscription, publish a task.created event onto the captured bus,
    confirm the projection file gets written."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(_projection_subscription(),),
    )

    async def _drive() -> None:
        # Let _amain reach the await stop_event.wait() — by then the
        # subscription is wired on the captured bus.
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        await bus.publish(
            _event("lithos.task.created", task_id="abc", title="Review PR")
        )
        # Give the SubscriptionRunner a beat to drain and the handler
        # to write the file.
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=3.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    tasks_file = cfg.obsidian_sync.vault_path / cfg.obsidian_sync.tasks_file  # type: ignore[union-attr]
    assert tasks_file.exists(), "projection file was not written"
    # Explicit encoding — projected content contains the non-ASCII
    # task-id marker 🆔, so default-encoding reads can fail on systems
    # where the locale isn't UTF-8.
    content = tasks_file.read_text(encoding="utf-8")
    assert "- [ ] Review PR 🆔 lithos:abc" in content


async def test_obsidian_sync_child_idles_when_no_obsidian_subscription(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stub_io: list[EventBus]
) -> None:
    """Config has [obsidian_sync] but no matching subscription — the
    child does NOT open a LithosClient or start the SSE source (no
    point connecting to Lithos with no consumer), but the fs watcher
    spawns regardless (Slice 2 US16 treats it as a first-class source
    gated on ``[obsidian_sync]``, not on a projection subscription)."""
    cfg = _cfg_with_obsidian(tmp_path)  # no subscriptions
    source_logger = "lithos_loom.children.obsidian_sync"

    sender = asyncio.create_task(_sigterm_soon())
    try:
        with caplog.at_level(logging.WARNING, logger=source_logger):
            rc = await asyncio.wait_for(_amain(cfg), timeout=2.0)
    finally:
        await _cancel_and_drain(sender)
    assert rc == 0

    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "no obsidian-projection, obsidian-status-transition, "
        "obsidian-priority-changed, obsidian-due-date-changed, or "
        "project-context-projection subscription configured"
        in m
        and "fs watcher runs but emits nothing" in m
        for m in warn_msgs
    ), warn_msgs
    # The Lithos source must NOT have been constructed — there is no
    # consumer to justify the Lithos handshake. fs-watcher's own
    # spawn is exercised in the dedicated test below.
    assert stub_io == [], "Lithos source was constructed despite no subscriptions"


async def test_obsidian_sync_child_spawns_fs_watcher_even_without_projection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stub_io: list[EventBus]
) -> None:
    """Slice 2 US16: the fs watcher is a source in its own right; its
    spawn is gated on ``[obsidian_sync]`` alone, not on the presence
    of an ``obsidian-projection`` subscription. Runtime behaviour
    without projection is unchanged (no projection writes →
    sync_state empty → no events emitted) but the source task DOES
    run, ready to emit as soon as something populates sync_state."""
    cfg = _cfg_with_obsidian(tmp_path)  # no subscriptions

    sender = asyncio.create_task(_sigterm_soon())
    fs_watcher_logger = "lithos_loom.sources.obsidian_fs_watcher"
    try:
        with caplog.at_level(logging.INFO, logger=fs_watcher_logger):
            rc = await asyncio.wait_for(_amain(cfg), timeout=2.0)
    finally:
        await _cancel_and_drain(sender)
    assert rc == 0

    info_msgs = [r.getMessage() for r in caplog.records if r.name == fs_watcher_logger]
    assert any("ObsidianFsWatcher: watching" in m for m in info_msgs), (
        f"fs watcher did not log its startup; got {info_msgs}"
    )


async def test_obsidian_sync_child_ignores_non_obsidian_subscription_actions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stub_io: list[EventBus]
) -> None:
    """Config with `noop` and `obsidian-projection` subscriptions — only
    the obsidian one is wired. The noop one is silently skipped here
    (it's some other child's job; routing comes in a future story)."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription("obs-tasks", action="obsidian-projection"),
            _projection_subscription("noop-smoke", action="noop"),
        ),
    )
    source_logger = "lithos_loom.children.obsidian_sync"

    sender = asyncio.create_task(_sigterm_soon())
    try:
        with caplog.at_level(logging.INFO, logger=source_logger):
            rc = await asyncio.wait_for(_amain(cfg), timeout=2.0)
    finally:
        await _cancel_and_drain(sender)
    assert rc == 0

    # The wiring log line names exactly the obsidian one — not noop.
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    wiring = next((m for m in info_msgs if "wiring subscription" in m), None)
    assert wiring is not None, f"no wiring log; got {info_msgs}"
    assert "obs-tasks" in wiring
    assert "noop-smoke" not in wiring


async def test_obsidian_sync_child_spawns_fs_watcher_that_emits_user_edits(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """Slice 2 US16 + US23: the child spawns an ObsidianFsWatcher
    alongside the projection. Publishing a created event writes the
    projection file; a subsequent user edit to that file flows back
    onto the bus as an ``obsidian.task.status_changed`` event without
    triggering a self-write feedback loop.
    """
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(_projection_subscription(),),
    )
    tasks_file = cfg.obsidian_sync.vault_path / cfg.obsidian_sync.tasks_file  # type: ignore[union-attr]
    captured_status_events: list[Event] = []

    async def _drive() -> None:
        # Wait for _amain to wire everything up.
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        # Subscribe to obsidian.task.status_changed BEFORE the user edit
        # so we don't miss the publication.
        sub = bus.subscribe(
            event_types=("obsidian.task.status_changed",),
            name="test-status-listener",
        )

        async def _drain() -> None:
            while True:
                event = await sub.queue.get()
                captured_status_events.append(event)

        drain_task = asyncio.create_task(_drain())
        try:
            # 1. Publish a task.created → projection writes file.
            await bus.publish(
                _event("lithos.task.created", task_id="abc", title="Review PR")
            )
            # Wait for the projection's debounced flush to commit.
            await asyncio.sleep(0.2)
            assert tasks_file.exists()
            # Explicit encoding — the projected content contains the
            # non-ASCII task-id marker 🆔, so default-encoding reads
            # could fail on systems where the locale isn't UTF-8.
            assert "- [ ] Review PR 🆔 lithos:abc" in tasks_file.read_text(
                encoding="utf-8"
            )

            # 2. User edits the file: flip [ ] to [x].
            tasks_file.write_text(
                tasks_file.read_text(encoding="utf-8").replace(
                    "- [ ] Review PR 🆔 lithos:abc",
                    "- [x] Review PR 🆔 lithos:abc",
                ),
                encoding="utf-8",
            )
            # Wait for at least one watcher poll cycle.
            await asyncio.sleep(0.4)
        finally:
            await _cancel_and_drain(drain_task)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=5.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    # The user-tick event must have flowed through the bus.
    status_payloads = [e.payload for e in captured_status_events]
    assert any(
        p.get("task_id") == "abc" and p.get("new") == "[x]" for p in status_payloads
    ), f"expected obsidian.task.status_changed for abc → [x]; got {status_payloads}"


async def test_obsidian_sync_child_refuses_duplicate_obsidian_projection_specs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stub_io: list[EventBus]
) -> None:
    """Two [[subscriptions]] both with action='obsidian-projection' would
    share a single stateful handler and race on the same file. Refuse
    at startup with a non-zero exit (Copilot review on #17)."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription("first", action="obsidian-projection"),
            _projection_subscription("second", action="obsidian-projection"),
        ),
    )
    source_logger = "lithos_loom.children.obsidian_sync"
    with caplog.at_level(logging.ERROR, logger=source_logger):
        rc = await asyncio.wait_for(_amain(cfg), timeout=2.0)
    assert rc == 1
    error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("refusing to wire" in m for m in error_msgs), error_msgs
    assert stub_io == [], "source should not be constructed when refusing"


# ── US17: status-transition handler wiring ─────────────────────────────


async def test_obsidian_sync_child_wires_status_transition_handler(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """Slice 2 US17 end-to-end: configure both projection AND
    status-transition subscriptions. Publish a task.created → projection
    writes the file. User edits the file to ``[x]`` → fs watcher emits
    ``obsidian.task.status_changed`` → status-transition handler calls
    ``lithos.task_complete`` with the right task_id and agent.
    """
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription(),
            _status_transition_subscription(),
        ),
    )
    tasks_file = cfg.obsidian_sync.vault_path / cfg.obsidian_sync.tasks_file  # type: ignore[union-attr]

    async def _drive() -> None:
        # Wait for _amain to wire everything up.
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        # 1. Publish a task.created → projection writes file.
        await bus.publish(
            _event("lithos.task.created", task_id="abc", title="Review PR")
        )
        # Wait for the projection's debounced flush to commit.
        await asyncio.sleep(0.2)
        assert tasks_file.exists()
        assert "- [ ] Review PR 🆔 lithos:abc" in tasks_file.read_text(encoding="utf-8")

        # 2. User edits the file: flip [ ] to [x].
        tasks_file.write_text(
            tasks_file.read_text(encoding="utf-8").replace(
                "- [ ] Review PR 🆔 lithos:abc",
                "- [x] Review PR 🆔 lithos:abc",
            ),
            encoding="utf-8",
        )
        # Allow time for: fs watcher poll cycle + bus delivery +
        # status-transition handler awaiting task_complete.
        await asyncio.sleep(0.6)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=5.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    # The status-transition handler must have pushed exactly one
    # task_complete for the ticked task, with the configured agent_id.
    assert _StubLithosClient.task_complete_calls == [
        {"task_id": "abc", "agent": "lithos-orchestrator-test"}
    ], (
        f"expected one task_complete call for abc; got "
        f"{_StubLithosClient.task_complete_calls}"
    )


async def test_obsidian_sync_child_status_transition_pushes_cancel_to_lithos(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """Slice 2 US18 end-to-end: same wiring as the US17 round-trip,
    but user flips ``[ ]`` to ``[-]`` instead of ``[x]``. Handler must
    call ``lithos.task_cancel`` with the constant reason."""
    from lithos_loom.subscriptions._obsidian_status_transition import _CANCEL_REASON

    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription(),
            _status_transition_subscription(),
        ),
    )
    tasks_file = cfg.obsidian_sync.vault_path / cfg.obsidian_sync.tasks_file  # type: ignore[union-attr]

    async def _drive() -> None:
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        await bus.publish(
            _event("lithos.task.created", task_id="cxl", title="Drop old README")
        )
        await asyncio.sleep(0.2)
        assert tasks_file.exists()
        assert "- [ ] Drop old README 🆔 lithos:cxl" in tasks_file.read_text(
            encoding="utf-8"
        )

        # User cancels via the [-] marker.
        tasks_file.write_text(
            tasks_file.read_text(encoding="utf-8").replace(
                "- [ ] Drop old README 🆔 lithos:cxl",
                "- [-] Drop old README 🆔 lithos:cxl",
            ),
            encoding="utf-8",
        )
        await asyncio.sleep(0.6)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=5.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    # Exactly one task_cancel; zero task_complete (US18-only path).
    assert _StubLithosClient.task_cancel_calls == [
        {
            "task_id": "cxl",
            "agent": "lithos-orchestrator-test",
            "reason": _CANCEL_REASON,
        }
    ], (
        f"expected one task_cancel call for cxl; got "
        f"{_StubLithosClient.task_cancel_calls}"
    )
    assert _StubLithosClient.task_complete_calls == [], (
        "task_complete must not be called for [ ]→[-] transitions; "
        f"got {_StubLithosClient.task_complete_calls}"
    )


async def test_obsidian_sync_child_status_transition_posts_reopen_finding(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """Slice 2 US19 end-to-end: configure both projection AND
    status-transition. Publish a task.created, simulate a user tick
    `[ ]` → `[x]` (verifies the task_complete path still works), then
    a user untick `[x]` → `[ ]`. Assert the handler posts a
    `[ReopenRequested]` finding for the untick — the D17 workaround
    until upstream `agent-lore/lithos#243` ships `task_reopen`."""
    from lithos_loom.subscriptions._obsidian_status_transition import (
        _REOPEN_REQUEST_SUMMARY,
    )

    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription(),
            _status_transition_subscription(),
        ),
    )
    tasks_file = cfg.obsidian_sync.vault_path / cfg.obsidian_sync.tasks_file  # type: ignore[union-attr]

    # US22: the stub auto-transitions ``task_status_returns`` on
    # task_complete / task_cancel calls, so the pre-check for the
    # second untick step naturally sees ``status=completed`` after
    # the first tick step's task_complete records.

    async def _drive() -> None:
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        await bus.publish(
            _event("lithos.task.created", task_id="rop", title="Reopen me")
        )
        await asyncio.sleep(0.2)
        assert tasks_file.exists()
        assert "- [ ] Reopen me 🆔 lithos:rop" in tasks_file.read_text(encoding="utf-8")

        # 1. User tick [ ] → [x] (causes a task_complete call).
        tasks_file.write_text(
            tasks_file.read_text(encoding="utf-8").replace(
                "- [ ] Reopen me 🆔 lithos:rop",
                "- [x] Reopen me 🆔 lithos:rop",
            ),
            encoding="utf-8",
        )
        # Give the watcher + handler time to fire.
        await asyncio.sleep(0.5)

        # 2. User untick [x] → [ ] (must post [ReopenRequested]).
        tasks_file.write_text(
            tasks_file.read_text(encoding="utf-8").replace(
                "- [x] Reopen me 🆔 lithos:rop",
                "- [ ] Reopen me 🆔 lithos:rop",
            ),
            encoding="utf-8",
        )
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=5.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    # Untick must have posted exactly one [ReopenRequested] finding
    # for rop with the expected summary + agent.
    assert _StubLithosClient.finding_post_calls == [
        {
            "task_id": "rop",
            "summary": _REOPEN_REQUEST_SUMMARY,
            "agent": "lithos-orchestrator-test",
            "knowledge_id": None,
        }
    ], (
        f"expected one [ReopenRequested] finding for rop; got "
        f"{_StubLithosClient.finding_post_calls}"
    )
    # And the prior tick is still in the complete-call log (sanity:
    # the handler is correctly dispatching on both transitions).
    assert _StubLithosClient.task_complete_calls == [
        {"task_id": "rop", "agent": "lithos-orchestrator-test"}
    ], (
        f"expected one task_complete for rop from the prior tick; got "
        f"{_StubLithosClient.task_complete_calls}"
    )


@pytest.mark.parametrize("marker", ["[/]", "[>]"])
async def test_obsidian_sync_child_no_op_for_in_progress_or_rescheduled(
    tmp_path: Path, stub_io: list[EventBus], marker: str
) -> None:
    """Slice 2 US20 end-to-end: configure both projection +
    status-transition; publish a task.created so the projection writes
    a ``[ ]`` line; simulate the user flipping the checkbox to ``[/]``
    or ``[>]``; assert ZERO Lithos calls. The watcher emits the event
    (verified by the existing fs-watcher test suite), the handler sees
    it (verified by the handler-level US20 test), but neither
    ``task_complete``, ``task_cancel``, nor ``finding_post`` fire.
    This closes the integration gap and pins the no-Lithos contract
    end-to-end."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription(),
            _status_transition_subscription(),
        ),
    )
    tasks_file = cfg.obsidian_sync.vault_path / cfg.obsidian_sync.tasks_file  # type: ignore[union-attr]

    async def _drive() -> None:
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        await bus.publish(
            _event("lithos.task.created", task_id="ipr", title="Working on it")
        )
        await asyncio.sleep(0.2)
        assert tasks_file.exists()
        assert "- [ ] Working on it 🆔 lithos:ipr" in tasks_file.read_text(
            encoding="utf-8"
        )

        # User flips to [/] or [>] — Obsidian-only convention; must not
        # leak into Lithos.
        tasks_file.write_text(
            tasks_file.read_text(encoding="utf-8").replace(
                "- [ ] Working on it 🆔 lithos:ipr",
                f"- {marker} Working on it 🆔 lithos:ipr",
            ),
            encoding="utf-8",
        )
        # Give the watcher poll + handler dispatch window time to fire.
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=5.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    # All three call-record lists MUST be empty — US20's "silent
    # no-op" contract.
    assert _StubLithosClient.task_complete_calls == [], (
        f"task_complete must not be called for {marker} transitions; "
        f"got {_StubLithosClient.task_complete_calls}"
    )
    assert _StubLithosClient.task_cancel_calls == [], (
        f"task_cancel must not be called for {marker} transitions; "
        f"got {_StubLithosClient.task_cancel_calls}"
    )
    assert _StubLithosClient.finding_post_calls == [], (
        f"finding_post must not be called for {marker} transitions; "
        f"got {_StubLithosClient.finding_post_calls}"
    )


async def test_obsidian_sync_child_rejects_duplicate_status_transition_specs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stub_io: list[EventBus]
) -> None:
    """Two ``[[subscriptions]]`` both with
    action='obsidian-status-transition' would mean duplicate Lithos
    calls per event. Refuse at startup with the per-action error
    message."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _status_transition_subscription("first"),
            _status_transition_subscription("second"),
        ),
    )
    source_logger = "lithos_loom.children.obsidian_sync"
    with caplog.at_level(logging.ERROR, logger=source_logger):
        rc = await asyncio.wait_for(_amain(cfg), timeout=2.0)
    assert rc == 1
    error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(
        "refusing to wire" in m and "obsidian-status-transition" in m
        for m in error_msgs
    ), error_msgs
    assert stub_io == [], "Lithos source should not be constructed when refusing"


async def test_obsidian_sync_child_warns_when_status_transition_without_projection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stub_io: list[EventBus]
) -> None:
    """status-transition without projection is permitted but inert
    (the fs watcher silently skips tasks whose marker is unknown to
    the projection). The child must warn at startup so the operator
    isn't left wondering why their ticks aren't pushing."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(_status_transition_subscription("only-transition"),),
    )
    source_logger = "lithos_loom.children.obsidian_sync"
    sender = asyncio.create_task(_sigterm_soon())
    try:
        with caplog.at_level(logging.WARNING, logger=source_logger):
            rc = await asyncio.wait_for(_amain(cfg), timeout=3.0)
    finally:
        await _cancel_and_drain(sender)
    assert rc == 0

    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "'only-transition' is configured but no obsidian-projection" in m
        for m in warn_msgs
    ), warn_msgs


async def test_obsidian_sync_child_skips_event_stream_for_status_transition_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stub_io: list[EventBus]
) -> None:
    """status-transition alone needs ``LithosClient`` (to call
    ``task_complete``) but NOT ``LithosEventStream`` (it consumes
    obsidian-side events only). Verify the SSE source isn't started
    when projection is absent."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(_status_transition_subscription(),),
    )
    sender = asyncio.create_task(_sigterm_soon())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=3.0)
    finally:
        await _cancel_and_drain(sender)
    assert rc == 0
    # The LithosEventStream stub captures every construction into
    # stub_io. With no projection, it must NOT have been instantiated.
    assert stub_io == [], (
        "LithosEventStream should not be constructed when only "
        "status-transition is wired"
    )


# ── US21: priority-changed end-to-end ──────────────────────────────────


async def test_obsidian_sync_child_priority_change_calls_task_update(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """Slice 2 US21 end-to-end (post-Lithos #290): configure projection
    + priority-changed (and status-transition for full-stack symmetry).
    Publish a ``task.created`` with ``metadata.priority='medium'`` so
    the projection writes a line with ``🔼``; simulate the user
    swapping it for ``🔺``; assert the handler calls
    ``lithos.task_update(metadata={"priority": "highest"})``. No
    ``task_complete`` / ``task_cancel`` / ``finding_post`` fires
    (priority-only edit, no other surface touched)."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription(),
            _status_transition_subscription(),
            _priority_changed_subscription(),
        ),
    )
    tasks_file = cfg.obsidian_sync.vault_path / cfg.obsidian_sync.tasks_file  # type: ignore[union-attr]

    async def _drive() -> None:
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        await bus.publish(
            _event(
                "lithos.task.created",
                task_id="pri",
                title="Pick the right priority",
                metadata={"priority": "medium"},
            )
        )
        await asyncio.sleep(0.2)
        assert tasks_file.exists()
        text_before = tasks_file.read_text(encoding="utf-8")
        assert "🔼" in text_before, text_before
        assert "🆔 lithos:pri" in text_before

        # User flips the priority emoji from medium to highest.
        tasks_file.write_text(
            text_before.replace("🔼", "🔺"),
            encoding="utf-8",
        )
        await asyncio.sleep(0.6)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=5.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    # Exactly one task_update call for the priority change.
    assert len(_StubLithosClient.task_update_calls) == 1, (
        f"expected one task_update call; got {_StubLithosClient.task_update_calls}"
    )
    call = _StubLithosClient.task_update_calls[0]
    assert call["task_id"] == "pri"
    assert call["agent"] == "lithos-orchestrator-test"
    assert call["metadata"] == {"priority": "highest"}
    # The handler MUST NOT pass title/description/tags — only the
    # priority key in metadata, so Lithos's per-key merge preserves
    # everything else on the task.
    assert call["title"] is None
    assert call["description"] is None
    assert call["tags"] is None

    # The user didn't change the checkbox, so status-transition path
    # must not have fired. No findings either — task_update is the
    # actual API call now, the [PriorityChangeRequested] workaround
    # is gone.
    assert _StubLithosClient.task_complete_calls == [], (
        f"task_complete must not be called for a priority-only edit; "
        f"got {_StubLithosClient.task_complete_calls}"
    )
    assert _StubLithosClient.task_cancel_calls == [], (
        f"task_cancel must not be called for a priority-only edit; "
        f"got {_StubLithosClient.task_cancel_calls}"
    )
    assert _StubLithosClient.finding_post_calls == [], (
        f"finding_post must not be called now that task_update is wired; "
        f"got {_StubLithosClient.finding_post_calls}"
    )


async def test_obsidian_sync_child_warns_when_priority_changed_without_projection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stub_io: list[EventBus]
) -> None:
    """priority-changed without projection is permitted but inert.
    Mirrors the status-transition warning."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(_priority_changed_subscription("only-priority"),),
    )
    source_logger = "lithos_loom.children.obsidian_sync"
    sender = asyncio.create_task(_sigterm_soon())
    try:
        with caplog.at_level(logging.WARNING, logger=source_logger):
            rc = await asyncio.wait_for(_amain(cfg), timeout=3.0)
    finally:
        await _cancel_and_drain(sender)
    assert rc == 0

    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "'only-priority' is configured but no obsidian-projection" in m
        for m in warn_msgs
    ), warn_msgs


async def test_obsidian_sync_child_rejects_duplicate_priority_changed_specs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stub_io: list[EventBus]
) -> None:
    """Two priority-changed specs would mean duplicate findings per
    user edit. Refused with the per-action error message."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _priority_changed_subscription("first"),
            _priority_changed_subscription("second"),
        ),
    )
    source_logger = "lithos_loom.children.obsidian_sync"
    with caplog.at_level(logging.ERROR, logger=source_logger):
        rc = await asyncio.wait_for(_amain(cfg), timeout=2.0)
    assert rc == 1
    error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(
        "refusing to wire" in m and "obsidian-priority-changed" in m for m in error_msgs
    ), error_msgs


# ── US22: idempotency pre-check end-to-end ─────────────────────────────


async def test_obsidian_sync_child_skips_complete_when_already_completed(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """Slice 2 US22 end-to-end: pre-seed ``task_status_returns`` so the
    handler's pre-check sees the task already in ``status=completed``.
    Simulating the user ticking the line must produce zero
    ``task_complete`` calls. The pre-check RPC itself must still fire."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription(),
            _status_transition_subscription(),
        ),
    )
    tasks_file = cfg.obsidian_sync.vault_path / cfg.obsidian_sync.tasks_file  # type: ignore[union-attr]

    # Pre-seed the stub so the pre-check returns a completed task.
    _StubLithosClient.task_status_returns["dup1"] = Task(
        id="dup1",
        title="t",
        status="completed",
        tags=(),
        metadata={},
        claims=(),
    )

    async def _drive() -> None:
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        await bus.publish(
            _event("lithos.task.created", task_id="dup1", title="Already done")
        )
        await asyncio.sleep(0.2)
        assert tasks_file.exists()
        assert "- [ ] Already done 🆔 lithos:dup1" in tasks_file.read_text(
            encoding="utf-8"
        )
        # User ticks the line — pre-check sees completed → skip.
        tasks_file.write_text(
            tasks_file.read_text(encoding="utf-8").replace(
                "- [ ] Already done 🆔 lithos:dup1",
                "- [x] Already done 🆔 lithos:dup1",
            ),
            encoding="utf-8",
        )
        await asyncio.sleep(0.6)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=5.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    # The pre-check fired, but task_complete did not.
    assert "dup1" in _StubLithosClient.task_get_calls, (
        f"pre-check task_get must have been called for dup1; "
        f"got {_StubLithosClient.task_get_calls}"
    )
    assert _StubLithosClient.task_complete_calls == [], (
        "task_complete must NOT be called when task is already completed; "
        f"got {_StubLithosClient.task_complete_calls}"
    )


async def test_obsidian_sync_child_skips_cancel_when_already_cancelled(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """US22: pre-seed the stub so the pre-check sees status=cancelled.
    Simulating user flipping to [-] must produce zero task_cancel calls."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription(),
            _status_transition_subscription(),
        ),
    )
    tasks_file = cfg.obsidian_sync.vault_path / cfg.obsidian_sync.tasks_file  # type: ignore[union-attr]

    _StubLithosClient.task_status_returns["dup2"] = Task(
        id="dup2",
        title="t",
        status="cancelled",
        tags=(),
        metadata={},
        claims=(),
    )

    async def _drive() -> None:
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        await bus.publish(
            _event("lithos.task.created", task_id="dup2", title="Already cancelled")
        )
        await asyncio.sleep(0.2)
        tasks_file.write_text(
            tasks_file.read_text(encoding="utf-8").replace(
                "- [ ] Already cancelled 🆔 lithos:dup2",
                "- [-] Already cancelled 🆔 lithos:dup2",
            ),
            encoding="utf-8",
        )
        await asyncio.sleep(0.6)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=5.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    assert "dup2" in _StubLithosClient.task_get_calls
    assert _StubLithosClient.task_cancel_calls == [], (
        "task_cancel must NOT be called when task is already cancelled; "
        f"got {_StubLithosClient.task_cancel_calls}"
    )


async def test_obsidian_sync_child_skips_reopen_when_task_is_open(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """US22: untick on a task that Lithos shows as still open (the
    projection-lag case). The handler must NOT post a [ReopenRequested]
    finding — posting a reopen request on an already-open task is
    nonsensical.

    Publishes the status_changed event directly onto the bus rather
    than going through the watcher chain — the chain would naturally
    transition the stub's status (auto-flip via task_complete) and
    mask the scenario we want to test. Projection is wired only so
    the bus gets captured via :class:`_CapturingSource`; no projected
    task is needed for the assertion."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription(),
            _status_transition_subscription(),
        ),
    )
    # The stub's default task_status return is a synthetic open Task,
    # so the reopen pre-check sees open and skips. No pre-seed needed.

    async def _drive() -> None:
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        # Synthesise the [x]→[ ] untick event directly.
        await bus.publish(
            Event(
                type="obsidian.task.status_changed",
                timestamp=datetime.now(UTC),
                payload={"task_id": "lag1", "prior": "[x]", "new": "[ ]"},
            )
        )
        await asyncio.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=5.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    assert "lag1" in _StubLithosClient.task_get_calls, (
        f"pre-check task_get must have been called for lag1; "
        f"got {_StubLithosClient.task_get_calls}"
    )
    assert _StubLithosClient.finding_post_calls == [], (
        "finding_post must NOT be called for untick on already-open task; "
        f"got {_StubLithosClient.finding_post_calls}"
    )


async def test_obsidian_sync_child_skips_priority_when_prior_equals_new(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """US22: when a third party publishes an
    ``obsidian.task.priority_changed`` event with ``prior == new``,
    the handler short-circuits before calling task_update. The
    fs-watcher won't naturally emit prior==new in steady state, so
    this test publishes the event directly onto the bus."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription(),
            _priority_changed_subscription(),
        ),
    )

    async def _drive() -> None:
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        # Publish a degenerate priority_changed event directly.
        await bus.publish(
            Event(
                type="obsidian.task.priority_changed",
                timestamp=datetime.now(UTC),
                payload={"task_id": "p1", "prior": "high", "new": "high"},
            )
        )
        await asyncio.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=5.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    assert _StubLithosClient.task_update_calls == [], (
        "task_update must NOT be called when prior==new; "
        f"got {_StubLithosClient.task_update_calls}"
    )


async def test_obsidian_sync_child_happy_paths_still_work_with_pre_check(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """US22 regression guard: when ``task_status_returns`` is empty
    (default synthetic open Task), the normal ``[ ]→[x]`` path
    completes and produces a task_complete call as before. Without
    this test the pre-check could silently break every happy-path
    end-to-end flow."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription(),
            _status_transition_subscription(),
        ),
    )
    tasks_file = cfg.obsidian_sync.vault_path / cfg.obsidian_sync.tasks_file  # type: ignore[union-attr]

    async def _drive() -> None:
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        await bus.publish(
            _event("lithos.task.created", task_id="hp1", title="Happy path")
        )
        await asyncio.sleep(0.2)
        tasks_file.write_text(
            tasks_file.read_text(encoding="utf-8").replace(
                "- [ ] Happy path 🆔 lithos:hp1",
                "- [x] Happy path 🆔 lithos:hp1",
            ),
            encoding="utf-8",
        )
        await asyncio.sleep(0.6)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=5.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    # Pre-check fired and task_complete then fired (default open task).
    assert "hp1" in _StubLithosClient.task_get_calls, (
        f"pre-check must have been called for hp1; "
        f"got {_StubLithosClient.task_get_calls}"
    )
    assert _StubLithosClient.task_complete_calls == [
        {"task_id": "hp1", "agent": "lithos-orchestrator-test"}
    ], (
        f"happy path must still produce a task_complete call; "
        f"got {_StubLithosClient.task_complete_calls}"
    )


# ── Lithos#294: strict priority idempotency end-to-end ─────────────────


async def test_obsidian_sync_child_skips_priority_when_lithos_already_matches(
    tmp_path: Path, stub_io: list[EventBus]
) -> None:
    """Lithos#294 strict priority pre-check end-to-end. Pre-seed the
    stub so the task already has ``metadata.priority="high"``.
    Synthesise a ``priority_changed`` event with ``prior="medium",
    new="high"`` (genuine change from watcher view, but Lithos
    already has the new value). The handler's strict pre-check must
    skip the ``task_update`` call."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription(),
            _priority_changed_subscription(),
        ),
    )

    # Lithos already has priority=high — strict pre-check should skip.
    _StubLithosClient.task_status_returns["pri1"] = Task(
        id="pri1",
        title="t",
        status="open",
        tags=(),
        metadata={"priority": "high"},
        claims=(),
    )

    async def _drive() -> None:
        await asyncio.sleep(0.1)
        bus = stub_io[-1]
        await bus.publish(
            Event(
                type="obsidian.task.priority_changed",
                timestamp=datetime.now(UTC),
                payload={"task_id": "pri1", "prior": "medium", "new": "high"},
            )
        )
        await asyncio.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    driver = asyncio.create_task(_drive())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=5.0)
    finally:
        await _cancel_and_drain(driver)
    assert rc == 0

    # Pre-check fired but task_update did not — strict idempotency
    # closed the gap that the payload-only check would have missed.
    assert "pri1" in _StubLithosClient.task_get_calls, (
        f"strict pre-check task_get must have been called for pri1; "
        f"got {_StubLithosClient.task_get_calls}"
    )
    assert _StubLithosClient.task_update_calls == [], (
        "task_update must NOT be called when Lithos already has the "
        f"target priority; got {_StubLithosClient.task_update_calls}"
    )


# ── Slice 4: project-context-projection wiring ─────────────────────────


def _project_context_projection_subscription(
    name: str = "project-context",
) -> SubscriptionConfig:
    return SubscriptionConfig(
        name=name,
        event_types=(
            "lithos.note.created",
            "lithos.note.updated",
            "lithos.note.deleted",
        ),
        action="project-context-projection",
        retry=RetryPolicy(attempts=1, initial_delay_seconds=0.0, max_delay_seconds=0.0),
        on_persistent_failure="ignore",
    )


async def test_obsidian_sync_spawns_note_stream_when_project_context_configured(
    tmp_path: Path,
    stub_io: list[EventBus],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``project-context-projection`` subscription triggers a
    LithosNoteStream spawn alongside the task stream. The Slice 4
    wiring contract: each source's lifecycle is gated on a consumer
    existing for its events.

    ``stub_io`` captures one entry per source-constructor call (both
    LithosEventStream and LithosNoteStream are stubbed by the same
    ``_CapturingSource``), so two captures means both sources spawned."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(
            _projection_subscription(),
            _project_context_projection_subscription(),
        ),
    )

    sender = asyncio.create_task(_sigterm_soon())
    try:
        with caplog.at_level(logging.INFO, logger="lithos_loom.children.obsidian_sync"):
            rc = await asyncio.wait_for(_amain(cfg), timeout=2.0)
    finally:
        await _cancel_and_drain(sender)

    assert rc == 0
    assert len(stub_io) == 2, (
        f"expected 2 source spawns (event + note); got {len(stub_io)}"
    )
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("wiring subscription 'project-context'" in m for m in info_msgs), (
        info_msgs
    )


async def test_no_note_stream_without_project_context_subscription(
    tmp_path: Path,
    stub_io: list[EventBus],
) -> None:
    """LithosNoteStream's lifecycle is gated on the
    project-context-projection subscription — without it, the source
    would publish events nobody consumes, wasting an SSE connection.

    With only the task projection subscription, exactly one source
    spawns (the event stream)."""
    cfg = _cfg_with_obsidian(
        tmp_path,
        subscriptions=(_projection_subscription(),),  # task only
    )

    sender = asyncio.create_task(_sigterm_soon())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=2.0)
    finally:
        await _cancel_and_drain(sender)

    assert rc == 0
    assert len(stub_io) == 1, (
        f"expected exactly 1 source spawn (event stream only); got {len(stub_io)}"
    )
