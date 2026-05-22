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

    Records ``task_complete`` and ``task_cancel`` invocations on
    class-level lists so the obsidian-status-transition end-to-end
    tests can assert on the round-trip. The ``_reset_stub_lithos_state``
    autouse fixture clears them between tests.
    """

    task_complete_calls: ClassVar[list[dict[str, Any]]] = []
    task_cancel_calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _StubLithosClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def finding_post(self, **kwargs: Any) -> None:
        # In case persistent-failure handling triggers in a test, no-op.
        return None

    async def task_complete(self, *, task_id: str, agent: str | None = None) -> None:
        type(self).task_complete_calls.append({"task_id": task_id, "agent": agent})

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


@pytest.fixture(autouse=True)
def _reset_stub_lithos_state() -> None:
    """Clear ``_StubLithosClient`` class-level call records between tests
    so cross-test leakage can't make an assertion accidentally pass."""
    _StubLithosClient.task_complete_calls.clear()
    _StubLithosClient.task_cancel_calls.clear()


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
    """Replace LithosClient + LithosEventStream in the obsidian_sync
    module and return a list that captures the bus each _StubSource was
    constructed with — tests publish to ``captured_buses[-1]``."""
    captured: list[EventBus] = []

    class _CapturingSource(_StubSource):
        def __init__(self, *, client: Any, bus: EventBus, events_url: str, **kw: Any):
            super().__init__(client=client, bus=bus, events_url=events_url, **kw)
            captured.append(bus)

    monkeypatch.setattr(obs_sync_mod, "LithosClient", _StubLithosClient)
    monkeypatch.setattr(obs_sync_mod, "LithosEventStream", _CapturingSource)
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
        "no obsidian-projection or obsidian-status-transition subscription" in m
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
