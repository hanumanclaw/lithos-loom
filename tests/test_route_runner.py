"""Tests for ``lithos_loom.subscriptions.route_runner`` (Slice 0 US5).

The RouteRunner is a claim-bound subscriber: it subscribes to bus
``lithos.task.created`` / ``lithos.task.released`` events filtered by the
route's tag match, claims matching open tasks via Lithos, runs the plugin
subprocess, and applies the resulting status (complete on succeeded,
release + ``[BlockerFailed]`` finding on failed). Tests inject fake
``lithos`` and patched ``run_plugin`` to exercise the dispatch logic
without HTTP or real subprocesses.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event, EventBus
from lithos_loom.config import RouteConfig, RouteMatch
from lithos_loom.errors import LithosClientError, PluginContractError
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions.route_runner import RouteRunner

# ── Helpers ────────────────────────────────────────────────────────────


def _route(
    name: str = "story-implement",
    *,
    tags: tuple[str, ...] = ("trigger:story-implement",),
    command: str = "echo {{task_json}} {{work_dir}} {{result_file}}",
    max_runtime_seconds: int | None = None,
    completes_task: bool = True,
) -> RouteConfig:
    return RouteConfig(
        name=name,
        command=command,
        match=RouteMatch(tags=tags),
        max_runtime_seconds=max_runtime_seconds,
        completes_task=completes_task,
    )


def _payload(
    task_id: str = "task-1",
    *,
    status: str = "open",
    tags: tuple[str, ...] = ("trigger:story-implement",),
    metadata: Mapping[str, Any] | None = None,
    claims: tuple[Mapping[str, Any], ...] = (),
) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "id": task_id,
            "title": "t",
            "status": status,
            "tags": list(tags),
            "metadata": dict(metadata or {}),
            "claims": [dict(c) for c in claims],
        }
    )


def _evt(
    type_: str = "lithos.task.created",
    payload: Mapping[str, Any] | None = None,
) -> Event:
    return Event(
        type=type_,
        timestamp=datetime.now(UTC),
        payload=payload if payload is not None else _payload(),
    )


def _make_runner(
    *,
    bus: EventBus,
    route: RouteConfig | None = None,
    lithos: AsyncMock | None = None,
    work_dir: Path,
    succeeded_result: dict[str, Any] | None = None,
    plugin_runner: Any = None,
) -> tuple[RouteRunner, AsyncMock]:
    if lithos is None:
        lithos = AsyncMock()
        lithos.task_claim.return_value = "2026-05-13T13:00:00Z"
    runner = RouteRunner(
        route=route or _route(),
        bus=bus,
        lithos=lithos,
        agent_id="lithos-orchestrator-test",
        work_dir_base=work_dir,
        renew_interval_seconds=3600,  # never fires in unit tests
        plugin_runner=plugin_runner
        or AsyncMock(
            return_value=succeeded_result
            or {
                "schema_version": 1,
                "task_id": "task-1",
                "status": "succeeded",
                "exit_code": 0,
            }
        ),
    )
    return runner, lithos


async def _run_for(runner: RouteRunner, *, seconds: float = 0.1) -> None:
    """Run the subscriber loop briefly, then cancel cleanly."""
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── Filter / match behaviour ───────────────────────────────────────────


async def test_runner_skips_tasks_with_non_matching_tags(tmp_path: Path) -> None:
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)

    await bus.publish(_evt(payload=_payload(tags=("trigger:other",))))
    await _run_for(runner)
    lithos.task_claim.assert_not_called()


async def test_runner_skips_non_open_tasks(tmp_path: Path) -> None:
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)

    await bus.publish(_evt(payload=_payload(status="completed")))
    await _run_for(runner)
    lithos.task_claim.assert_not_called()


async def test_runner_skips_when_dependencies_not_completed(tmp_path: Path) -> None:
    bus = EventBus()
    lithos = AsyncMock()
    # task_get for the dep returns an open dep task — not completed.
    # (Post-lithos#294 the runner uses task_get rather than task_status:
    # claims aren't needed for the dep check.)
    lithos.task_get.return_value = Task(
        id="dep-1", title="t", status="open", tags=(), metadata={}, claims=()
    )
    runner, _ = _make_runner(
        bus=bus,
        lithos=lithos,
        work_dir=tmp_path,
    )

    await bus.publish(_evt(payload=_payload(metadata={"depends_on": ["dep-1"]})))
    await _run_for(runner)
    lithos.task_get.assert_awaited_with(task_id="dep-1")
    lithos.task_claim.assert_not_called()


async def test_runner_runs_when_dependencies_are_completed(tmp_path: Path) -> None:
    bus = EventBus()
    lithos = AsyncMock()
    lithos.task_claim.return_value = "expires"
    lithos.task_get.return_value = Task(
        id="dep-1",
        title="t",
        status="completed",
        tags=(),
        metadata={},
        claims=(),
    )
    runner, _ = _make_runner(bus=bus, lithos=lithos, work_dir=tmp_path)

    await bus.publish(_evt(payload=_payload(metadata={"depends_on": ["dep-1"]})))
    await _run_for(runner)
    lithos.task_claim.assert_awaited_once()
    lithos.task_complete.assert_awaited_once()


# ── Claim race ─────────────────────────────────────────────────────────


async def test_runner_lost_claim_race_does_not_run_plugin(tmp_path: Path) -> None:
    bus = EventBus()
    lithos = AsyncMock()
    lithos.task_claim.side_effect = LithosClientError("claim_failed", "aspect taken")
    plugin_runner = AsyncMock()
    runner, _ = _make_runner(
        bus=bus,
        lithos=lithos,
        work_dir=tmp_path,
        plugin_runner=plugin_runner,
    )

    await bus.publish(_evt())
    await _run_for(runner)
    plugin_runner.assert_not_called()
    lithos.task_complete.assert_not_called()


# ── Success path ───────────────────────────────────────────────────────


async def test_runner_claims_runs_plugin_then_completes_task(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)

    await bus.publish(_evt())
    await _run_for(runner)

    lithos.task_claim.assert_awaited_once()
    claim_args = lithos.task_claim.await_args.kwargs
    assert claim_args["task_id"] == "task-1"
    assert claim_args["agent"] == "lithos-orchestrator-test"
    assert claim_args["aspect"] == "story-implement"

    lithos.task_complete.assert_awaited_once()
    complete_args = lithos.task_complete.await_args.kwargs
    assert complete_args["task_id"] == "task-1"


async def test_runner_writes_task_json_to_work_dir(tmp_path: Path) -> None:
    """The plugin sees task.json with the event payload at invocation time."""
    bus = EventBus()
    seen_body: dict[str, Any] = {}

    async def capturing_plugin(**kwargs: Any) -> dict[str, Any]:
        # Read the task.json the runner wrote, before success cleanup.
        import json as _json

        seen_body.update(_json.loads(kwargs["task_json_path"].read_text()))
        return {
            "schema_version": 1,
            "task_id": "task-77",
            "status": "succeeded",
            "exit_code": 0,
        }

    runner, _ = _make_runner(bus=bus, work_dir=tmp_path, plugin_runner=capturing_plugin)

    await bus.publish(_evt(payload=_payload(task_id="task-77")))
    await _run_for(runner)

    assert seen_body["task"]["id"] == "task-77"


# ── Failure paths ──────────────────────────────────────────────────────


async def test_runner_failed_result_releases_and_posts_finding(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    plugin_runner = AsyncMock(
        return_value={
            "schema_version": 1,
            "task_id": "task-1",
            "status": "failed",
            "exit_code": 1,
            "error": {"category": "agent", "message": "plugin gave up"},
        }
    )
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )

    await bus.publish(_evt())
    await _run_for(runner)

    lithos.task_complete.assert_not_called()
    lithos.task_release.assert_awaited_once()
    lithos.finding_post.assert_awaited_once()
    summary = lithos.finding_post.await_args.kwargs["summary"]
    assert summary.startswith("[BlockerFailed]")
    assert "story-implement" in summary
    assert "plugin gave up" in summary


async def test_runner_plugin_contract_violation_releases_and_posts(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    plugin_runner = AsyncMock(side_effect=PluginContractError("malformed result.json"))
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )

    await bus.publish(_evt())
    await _run_for(runner)

    lithos.task_complete.assert_not_called()
    lithos.task_release.assert_awaited_once()
    lithos.finding_post.assert_awaited_once()
    assert "[BlockerFailed]" in lithos.finding_post.await_args.kwargs["summary"]


async def test_runner_plugin_timeout_releases_and_posts(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    plugin_runner = AsyncMock(side_effect=TimeoutError("ran too long"))
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )

    await bus.publish(_evt())
    await _run_for(runner)

    lithos.task_release.assert_awaited_once()
    lithos.finding_post.assert_awaited_once()


async def test_runner_interrupted_result_releases_without_finding(
    tmp_path: Path,
) -> None:
    """Status=interrupted means the plugin caught a shutdown signal — release
    the claim so a future run can pick the task up again, but no [BlockerFailed]
    finding (it wasn't an error, the operator stopped us).
    """
    bus = EventBus()
    plugin_runner = AsyncMock(
        return_value={
            "schema_version": 1,
            "task_id": "task-1",
            "status": "interrupted",
            "exit_code": 30,
        }
    )
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )

    await bus.publish(_evt())
    await _run_for(runner)

    lithos.task_release.assert_awaited_once()
    lithos.finding_post.assert_not_called()
    lithos.task_complete.assert_not_called()


# ── Resilience ─────────────────────────────────────────────────────────


async def test_runner_recovers_from_unexpected_exception_in_handler(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug in handle() must not stop the consumer loop."""
    bus = EventBus()
    plugin_runner = AsyncMock(
        side_effect=[
            RuntimeError("handler boom"),
            {
                "schema_version": 1,
                "task_id": "task-2",
                "status": "succeeded",
                "exit_code": 0,
            },
        ]
    )
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )

    with caplog.at_level(logging.ERROR):
        await bus.publish(_evt(payload=_payload("task-1")))
        await asyncio.sleep(0.05)
        await bus.publish(_evt(payload=_payload("task-2")))
        await _run_for(runner, seconds=0.1)

    # Second event still got handled.
    assert lithos.task_complete.await_count == 1
    assert lithos.task_complete.await_args.kwargs["task_id"] == "task-2"


async def test_runner_dedupes_repeat_events_for_same_task(tmp_path: Path) -> None:
    """Stale created/released events for an already-handled task must not re-run.

    Regression: without an in-runner processed-set, two queued events for
    the same open task each ran the plugin and called task_complete twice
    (mock-Lithos always returns success, so the runner couldn't rely on
    claim_failed to dedupe). Real Lithos enforces this, but the runner
    should too.
    """
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)

    payload = _payload("task-1")
    await bus.publish(_evt(type_="lithos.task.created", payload=payload))
    await bus.publish(_evt(type_="lithos.task.released", payload=payload))
    await _run_for(runner, seconds=0.2)

    # Exactly one claim → exactly one plugin run → exactly one complete.
    assert lithos.task_claim.await_count == 1
    assert lithos.task_complete.await_count == 1


# ── Issue #86: dispatch on task.updated (tag added without restart) ────


async def test_runner_dispatches_on_updated_for_existing_task(
    tmp_path: Path,
) -> None:
    """The core #86 capability: a route's trigger tag added to an already-open
    task arrives as ``lithos.task.updated`` and dispatches — no `created`, no
    daemon restart. The enriched envelope already carries the new tags, so the
    bus matcher passes it through and the standard claim → run path fires.
    """
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)

    await bus.publish(_evt(type_="lithos.task.updated", payload=_payload("task-1")))
    await _run_for(runner)

    assert lithos.task_claim.await_count == 1
    assert lithos.task_complete.await_count == 1


async def test_runner_ignores_own_metadata_update_within_process(
    tmp_path: Path,
) -> None:
    """No self-trigger loop: a plugin's own end-of-run ``task_update`` (e.g.
    story-develop writing ``develop_*`` metadata) fires ``lithos.task.updated``
    for a task this process already claimed. ``_processed_tasks`` drops it, so
    the plugin does not re-run itself. This is the load-bearing safety property
    for subscribing to ``updated``.
    """
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)

    payload = _payload("task-1")
    await bus.publish(_evt(type_="lithos.task.created", payload=payload))
    # Same id arrives again as `updated` — as if the plugin wrote metadata.
    await bus.publish(
        _evt(
            type_="lithos.task.updated",
            payload=_payload("task-1", metadata={"develop_pr_url": "http://x/pull/1"}),
        )
    )
    await _run_for(runner, seconds=0.2)

    # One claim, one run — the second event was deduped.
    assert lithos.task_claim.await_count == 1
    assert lithos.task_complete.await_count == 1


async def test_runner_ignores_unrelated_update_on_processed_task(
    tmp_path: Path,
) -> None:
    """An unrelated edit (priority / due-date) on a task this process already
    ran does not re-run it — the same ``_processed_tasks`` guard, framed as the
    operator-edit case from issue #86's test (c).
    """
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)

    await bus.publish(_evt(type_="lithos.task.created", payload=_payload("task-1")))
    await bus.publish(
        _evt(
            type_="lithos.task.updated",
            payload=_payload("task-1", metadata={"priority": "high"}),
        )
    )
    await _run_for(runner, seconds=0.2)

    assert lithos.task_claim.await_count == 1
    assert lithos.task_complete.await_count == 1


async def test_runner_does_not_re_attempt_after_own_release_with_finding(
    tmp_path: Path,
) -> None:
    """Behaviour contract: a task we claimed-then-released stays suppressed.

    When the plugin fails the runner releases the claim and posts a
    [BlockerFailed] finding. Lithos then emits ``task.released`` — but
    this runner's ``_processed_tasks`` set already contains the task id
    from the successful claim, so the released event is silently
    dropped. Without this dedup the runner would tight-loop:
    fail → release → released event → re-claim → fail → ...

    This codifies the current "fail once per task per daemon process"
    behaviour. Retry budget for legitimate re-attempts is tracked in
    issue #11.
    """
    bus = EventBus()
    plugin_runner = AsyncMock(
        return_value={
            "schema_version": 1,
            "task_id": "task-1",
            "status": "failed",
            "exit_code": 1,
            "error": {"category": "agent", "message": "boom"},
        }
    )
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )

    # First created event: claim + plugin fail + release with finding.
    await bus.publish(_evt(type_="lithos.task.created", payload=_payload("task-1")))
    await _run_for(runner)
    assert lithos.task_claim.await_count == 1
    assert lithos.task_release.await_count == 1
    assert lithos.finding_post.await_count == 1

    # Now the released event from Lithos lands. We must NOT re-claim —
    # _processed_tasks already contains the id from the successful claim.
    await bus.publish(_evt(type_="lithos.task.released", payload=_payload("task-1")))
    await _run_for(runner)
    assert lithos.task_claim.await_count == 1  # unchanged
    assert lithos.task_release.await_count == 1  # unchanged
    assert lithos.finding_post.await_count == 1  # unchanged


async def test_runner_releases_dedupe_when_claim_race_lost(tmp_path: Path) -> None:
    """If we lose the claim race, we must NOT add the task to the dedupe set —
    otherwise a future event (e.g. after the winner releases) would be
    silently dropped instead of giving us a chance to take ownership.
    """
    bus = EventBus()
    lithos = AsyncMock()
    # First call: lose the race. Second call (after release): succeed.
    lithos.task_claim.side_effect = [
        LithosClientError("claim_failed", "taken"),
        "expires-2",
    ]
    runner, _ = _make_runner(bus=bus, lithos=lithos, work_dir=tmp_path)

    payload = _payload("task-9")
    await bus.publish(_evt(payload=payload))
    await asyncio.sleep(0.05)
    await bus.publish(_evt(payload=payload))
    await _run_for(runner, seconds=0.2)

    # Two claim attempts — the lost-race path didn't pollute the dedupe set.
    assert lithos.task_claim.await_count == 2


async def test_runner_removes_work_dir_on_failure_when_not_retaining(
    tmp_path: Path,
) -> None:
    """retain_failed_workdirs=False must clean up failed runs too, not just
    successful ones. Regression: the prior implementation only cleaned the
    success branch, so failed/timeout/contract-violation paths leaked dirs.
    """
    bus = EventBus()
    plugin_runner = AsyncMock(
        return_value={
            "schema_version": 1,
            "task_id": "task-1",
            "status": "failed",
            "exit_code": 1,
            "error": {"category": "agent", "message": "nope"},
        }
    )
    lithos = AsyncMock()
    lithos.task_claim.return_value = "expires"
    runner = RouteRunner(
        route=_route(),
        bus=bus,
        lithos=lithos,
        agent_id="lithos-orchestrator-test",
        work_dir_base=tmp_path,
        renew_interval_seconds=3600,
        retain_failed_workdirs=False,
        plugin_runner=plugin_runner,
    )

    await bus.publish(_evt())
    await _run_for(runner, seconds=0.2)

    assert not (tmp_path / "task-1").exists()


async def test_runner_keeps_work_dir_on_failure_when_retaining(
    tmp_path: Path,
) -> None:
    """The default retain_failed_workdirs=True keeps failed dirs for inspection."""
    bus = EventBus()
    plugin_runner = AsyncMock(
        return_value={
            "schema_version": 1,
            "task_id": "task-1",
            "status": "failed",
            "exit_code": 1,
        }
    )
    runner, _ = _make_runner(bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner)
    # _make_runner uses default retain_failed_workdirs=True.

    await bus.publish(_evt())
    await _run_for(runner, seconds=0.2)

    assert (tmp_path / "task-1").exists()


async def test_runner_subscribes_to_created_released_and_updated(
    tmp_path: Path,
) -> None:
    """React to created + released + updated; ignore claimed/completed/cancelled.

    ``released`` is the re-claim trigger now that the source is the event
    stream. ``updated`` joined the set in issue #86 so that adding a route's
    trigger tag to an already-open task dispatches without a daemon restart
    (the bus matcher's tag filter still gates which updates actually run).
    Lifecycle observations (claimed/completed/cancelled) remain ignored.
    """
    bus = EventBus()

    # Ignored lifecycle event types never reach a claim.
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)
    for type_ in (
        "lithos.task.claimed",
        "lithos.task.completed",
        "lithos.task.cancelled",
    ):
        await bus.publish(_evt(type_=type_))
    await _run_for(runner)
    lithos.task_claim.assert_not_called()

    # A matching `updated` event *does* dispatch (fresh runner so the dedup
    # set is empty).
    bus2 = EventBus()
    runner2, lithos2 = _make_runner(bus=bus2, work_dir=tmp_path)
    await bus2.publish(_evt(type_="lithos.task.updated", payload=_payload("task-u")))
    await _run_for(runner2)
    assert lithos2.task_claim.await_count == 1


# ── Usage-limit re-dispatch (T10) ──────────────────────────────────────


def _interrupted_with_resume(
    resume_after: str = "2026-01-01T00:00:00+00:00",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_id": "task-1",
        "status": "interrupted",
        "exit_code": 30,
        "error": {
            "category": "usage_limited",
            "message": "coder usage-limited",
            "retriable": True,
        },
        "resume": {"resume_after": resume_after, "run_id": "r1"},
    }


async def test_runner_interrupted_with_resume_redispatches(tmp_path: Path) -> None:
    """A resume block on an interrupted result schedules a re-claim + re-run.

    resume_after in the past → the sleeper fires immediately; the runner
    re-checks the task is open, drops the dedup entry, re-claims, and the
    plugin's second run succeeds → task_complete.
    """
    bus = EventBus()
    plugin_runner = AsyncMock(
        side_effect=[
            _interrupted_with_resume(),
            {
                "schema_version": 1,
                "task_id": "task-1",
                "status": "succeeded",
                "exit_code": 0,
            },
        ]
    )
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )
    lithos.task_get.return_value = Task(
        id="task-1",
        title="t",
        status="open",
        tags=("trigger:story-implement",),
        metadata={},
        claims=(),
    )

    await bus.publish(_evt())
    await _run_for(runner, seconds=0.3)

    assert plugin_runner.await_count == 2
    assert lithos.task_claim.await_count == 2
    lithos.task_release.assert_awaited_once()
    lithos.task_complete.assert_awaited_once()


async def test_runner_resume_dropped_when_task_no_longer_open(
    tmp_path: Path,
) -> None:
    """At resume time a terminal task is dropped — no re-claim, no re-run."""
    bus = EventBus()
    plugin_runner = AsyncMock(side_effect=[_interrupted_with_resume()])
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )
    lithos.task_get.return_value = Task(
        id="task-1",
        title="t",
        status="completed",
        tags=("trigger:story-implement",),
        metadata={},
        claims=(),
    )

    await bus.publish(_evt())
    await _run_for(runner, seconds=0.3)

    assert plugin_runner.await_count == 1
    assert lithos.task_claim.await_count == 1


async def test_runner_resume_budget_exhausted_posts_friction(
    tmp_path: Path,
) -> None:
    """Beyond MAX_RESUMES_PER_TASK the task stays open with a [Friction] note."""
    from lithos_loom.subscriptions.route_runner import MAX_RESUMES_PER_TASK

    bus = EventBus()
    plugin_runner = AsyncMock(side_effect=[_interrupted_with_resume()])
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )
    runner._resume_counts["task-1"] = MAX_RESUMES_PER_TASK

    await bus.publish(_evt())
    await _run_for(runner, seconds=0.3)

    assert plugin_runner.await_count == 1
    assert not runner._resume_tasks
    lithos.finding_post.assert_awaited_once()
    summary = lithos.finding_post.await_args.kwargs["summary"]
    assert summary.startswith("[Friction]")
    assert "resume budget exhausted" in summary


async def test_runner_unparseable_resume_after_not_scheduled(
    tmp_path: Path,
) -> None:
    """Garbage resume_after → release only; no crash, no re-dispatch."""
    bus = EventBus()
    plugin_runner = AsyncMock(
        side_effect=[_interrupted_with_resume(resume_after="not-a-timestamp")]
    )
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )

    await bus.publish(_evt())
    await _run_for(runner, seconds=0.3)

    assert plugin_runner.await_count == 1
    assert not runner._resume_tasks
    lithos.task_release.assert_awaited_once()
    lithos.finding_post.assert_not_called()


async def test_rescheduled_resume_survives_old_sleepers_cleanup(
    tmp_path: Path,
) -> None:
    """A cancelled old sleeper's done-callback must not evict its replacement.

    Schedule the same task twice (far-future resume_after so neither fires):
    the first sleeper is cancelled and replaced; once its cancellation
    callback runs, _resume_tasks must still hold the SECOND sleeper.
    """
    bus = EventBus()
    runner, _ = _make_runner(bus=bus, work_dir=tmp_path)
    resume = {"resume_after": "2099-01-01T00:00:00+00:00"}

    await runner._maybe_schedule_resume("task-1", resume)
    first = runner._resume_tasks["task-1"]
    await runner._maybe_schedule_resume("task-1", resume)
    second = runner._resume_tasks["task-1"]
    assert first is not second

    # Let the first sleeper's cancellation + done-callback run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert runner._resume_tasks.get("task-1") is second
    assert not second.cancelled()
    second.cancel()


async def test_resume_redispatch_uses_fresh_task_content(tmp_path: Path) -> None:
    """The resumed run must develop against the task's CURRENT content.

    The operator edits the task body / metadata during the pause window;
    task_get returns the edited task; the second plugin run's task.json must
    carry the fresh title + body + metadata, not the interrupted snapshot.
    """
    import json as _json

    bus = EventBus()
    seen: list[dict[str, Any]] = []

    async def capturing_plugin(**kwargs: Any) -> dict[str, Any]:
        body = _json.loads(kwargs["task_json_path"].read_text())
        seen.append(body["task"])
        if len(seen) == 1:
            return _interrupted_with_resume()
        return {
            "schema_version": 1,
            "task_id": "task-1",
            "status": "succeeded",
            "exit_code": 0,
        }

    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=capturing_plugin
    )
    # The fresh snapshot the operator's edits produced.
    lithos.task_get.return_value = Task(
        id="task-1",
        title="EDITED title",
        status="open",
        tags=("trigger:story-implement",),
        metadata={"project": "loom", "acceptance_criteria": "NEW criteria"},
        claims=(),
        description="EDITED body",
    )

    # The interrupting event carried the STALE content.
    stale = _payload(task_id="task-1", metadata={"project": "loom"})
    await bus.publish(_evt(payload=stale))
    await _run_for(runner, seconds=0.3)

    assert len(seen) == 2
    second = seen[1]
    assert second["title"] == "EDITED title"
    assert second["description"] == "EDITED body"
    assert second["metadata"]["acceptance_criteria"] == "NEW criteria"


# ── {{repo}} token resolution (T10) ────────────────────────────────────


def _repo_route() -> RouteConfig:
    return _route(
        name="story-develop",
        tags=("trigger:story-develop",),
        command="run --repo {{repo}} --result-file {{result_file}}",
    )


async def test_repo_token_resolved_from_projects_map(tmp_path: Path) -> None:
    """{{repo}} expands to the task's project repo before the plugin runs."""
    bus = EventBus()
    captured: dict[str, Any] = {}

    async def capturing_plugin(**kwargs: Any) -> dict[str, Any]:
        captured["command"] = kwargs["command"]
        return {
            "schema_version": 1,
            "task_id": "task-1",
            "status": "succeeded",
            "exit_code": 0,
        }

    lithos = AsyncMock()
    lithos.task_claim.return_value = "2026-05-13T13:00:00Z"
    runner = RouteRunner(
        route=_repo_route(),
        bus=bus,
        lithos=lithos,
        agent_id="a",
        work_dir_base=tmp_path,
        renew_interval_seconds=3600,
        plugin_runner=capturing_plugin,
        project_repos={"loom": Path("/home/x/loom")},
    )
    payload = _payload(
        task_id="task-1",
        tags=("trigger:story-develop",),
        metadata={"project": "loom"},
    )
    await bus.publish(_evt(payload=payload))
    await _run_for(runner)

    assert "--repo /home/x/loom" in captured["command"]
    assert "{{repo}}" not in captured["command"]


async def test_repo_token_with_spaces_is_shell_quoted(tmp_path: Path) -> None:
    """A repo path with spaces survives shlex.split as ONE argv element.

    The resolved command is tokenised with shlex.split in plugin_runner, so
    an unquoted spaced path would split and truncate --repo. Assert the
    round-trip: shlex.split of the resolved command yields the full path.
    """
    import shlex

    bus = EventBus()
    captured: dict[str, Any] = {}

    async def capturing_plugin(**kwargs: Any) -> dict[str, Any]:
        captured["command"] = kwargs["command"]
        return {
            "schema_version": 1,
            "task_id": "task-1",
            "status": "succeeded",
            "exit_code": 0,
        }

    lithos = AsyncMock()
    lithos.task_claim.return_value = "2026-05-13T13:00:00Z"
    spaced = "/home/x/my projects/loom repo"
    runner = RouteRunner(
        route=_repo_route(),
        bus=bus,
        lithos=lithos,
        agent_id="a",
        work_dir_base=tmp_path,
        renew_interval_seconds=3600,
        plugin_runner=capturing_plugin,
        project_repos={"loom": Path(spaced)},
    )
    payload = _payload(
        task_id="task-1",
        tags=("trigger:story-develop",),
        metadata={"project": "loom"},
    )
    await bus.publish(_evt(payload=payload))
    await _run_for(runner)

    argv = shlex.split(captured["command"])
    assert spaced in argv  # one element, not split on the spaces
    assert argv[argv.index("--repo") + 1] == spaced


async def test_repo_token_without_project_releases_with_finding(
    tmp_path: Path,
) -> None:
    """A {{repo}} route + a task with no metadata.project is a config error:
    release with a finding, never run the plugin."""
    bus = EventBus()
    plugin = AsyncMock()
    lithos = AsyncMock()
    lithos.task_claim.return_value = "2026-05-13T13:00:00Z"
    runner = RouteRunner(
        route=_repo_route(),
        bus=bus,
        lithos=lithos,
        agent_id="a",
        work_dir_base=tmp_path,
        renew_interval_seconds=3600,
        plugin_runner=plugin,
        project_repos={"loom": Path("/home/x/loom")},
    )
    payload = _payload(task_id="task-1", tags=("trigger:story-develop",), metadata={})
    await bus.publish(_evt(payload=payload))
    await _run_for(runner)

    plugin.assert_not_called()
    lithos.task_release.assert_awaited_once()
    summary = lithos.finding_post.await_args.kwargs["summary"]
    assert "metadata.project" in summary


async def test_repo_token_unregistered_project_releases_with_finding(
    tmp_path: Path,
) -> None:
    """A {{repo}} route + a task whose project isn't in [projects.*]: same."""
    bus = EventBus()
    plugin = AsyncMock()
    lithos = AsyncMock()
    lithos.task_claim.return_value = "2026-05-13T13:00:00Z"
    runner = RouteRunner(
        route=_repo_route(),
        bus=bus,
        lithos=lithos,
        agent_id="a",
        work_dir_base=tmp_path,
        renew_interval_seconds=3600,
        plugin_runner=plugin,
        project_repos={"loom": Path("/home/x/loom")},
    )
    payload = _payload(
        task_id="task-1",
        tags=("trigger:story-develop",),
        metadata={"project": "unregistered"},
    )
    await bus.publish(_evt(payload=payload))
    await _run_for(runner)

    plugin.assert_not_called()
    lithos.task_release.assert_awaited_once()
    summary = lithos.finding_post.await_args.kwargs["summary"]
    assert "unregistered" in summary


async def test_no_repo_token_does_not_require_project(tmp_path: Path) -> None:
    """A route WITHOUT {{repo}} runs regardless of metadata.project."""
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)  # default echo route
    await bus.publish(_evt(payload=_payload(metadata={})))
    await _run_for(runner)
    lithos.task_complete.assert_awaited_once()


async def test_resume_dropped_when_task_retagged_out_of_route(
    tmp_path: Path,
) -> None:
    """If the task no longer carries the route's trigger tags at resume time
    (operator pulled the tag during the pause), the resumed run is dropped —
    the direct _handle call must not bypass the route's tag filter."""
    bus = EventBus()
    plugin_runner = AsyncMock(side_effect=[_interrupted_with_resume()])
    runner, lithos = _make_runner(
        bus=bus, work_dir=tmp_path, plugin_runner=plugin_runner
    )
    # Fresh snapshot: still open, but the trigger tag is gone.
    lithos.task_get.return_value = Task(
        id="task-1",
        title="t",
        status="open",
        tags=("some-other-tag",),
        metadata={},
        claims=(),
    )

    await bus.publish(_evt())
    await _run_for(runner, seconds=0.3)

    assert plugin_runner.await_count == 1  # only the first run; no re-dispatch
    assert lithos.task_claim.await_count == 1


# ── completes_task=false (PR-producing routes, #90) ────────────────────


def _develop_route(completes_task: bool = False) -> RouteConfig:
    return _route(
        name="story-develop",
        tags=("trigger:story-develop",),
        completes_task=completes_task,
    )


async def test_completes_task_false_leaves_open_and_marks_delivered(
    tmp_path: Path,
) -> None:
    """A completes_task=false route must NOT complete the task on success — it
    releases (leaves it open for human merge) and marks loom_delivered so a
    restart won't re-develop it. This is the #90 fix: an approved PR-producing
    run can no longer close an issue for unmerged work."""
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, route=_develop_route(), work_dir=tmp_path)

    await bus.publish(_evt(payload=_payload(tags=("trigger:story-develop",))))
    await _run_for(runner)

    lithos.task_complete.assert_not_called()
    lithos.task_release.assert_awaited_once()
    upd = lithos.task_update.await_args
    assert upd.kwargs["metadata"] == {"loom_delivered": True}


async def test_delivered_task_is_skipped_not_re_developed(tmp_path: Path) -> None:
    """A task already marked loom_delivered is skipped — the restart-safety
    guard: bootstrap re-emits open tasks as `created`, and a delivered task
    must not be re-claimed while its PR awaits merge."""
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, route=_develop_route(), work_dir=tmp_path)

    await bus.publish(
        _evt(
            payload=_payload(
                tags=("trigger:story-develop",),
                metadata={"loom_delivered": True},
            )
        )
    )
    await _run_for(runner)

    lithos.task_claim.assert_not_called()


async def test_completes_task_true_still_completes(tmp_path: Path) -> None:
    """Default routes (completes_task=True) are unchanged: success completes the
    task and writes no delivered marker."""
    bus = EventBus()
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)  # default completes

    await bus.publish(_evt())
    await _run_for(runner)

    lithos.task_complete.assert_awaited_once()
    lithos.task_update.assert_not_called()


async def test_delivered_marker_failure_posts_friction(tmp_path: Path) -> None:
    """If marking loom_delivered fails, the restart-re-develop risk is made
    visible as a [Friction] finding, not just logged (Copilot #95)."""
    bus = EventBus()
    lithos = AsyncMock()
    lithos.task_claim.return_value = "2026-05-13T13:00:00Z"
    lithos.task_update.side_effect = RuntimeError("lithos unavailable")
    runner, _ = _make_runner(
        bus=bus, route=_develop_route(), lithos=lithos, work_dir=tmp_path
    )

    await bus.publish(_evt(payload=_payload(tags=("trigger:story-develop",))))
    await _run_for(runner)

    lithos.task_release.assert_awaited_once()  # release still attempted
    summary = lithos.finding_post.await_args.kwargs["summary"]
    assert summary.startswith("[Friction]")
    assert "loom_delivered" in summary


async def test_completes_task_true_route_ignores_delivered_marker(
    tmp_path: Path,
) -> None:
    """loom_delivered is route-specific protection for completes_task=false
    routes. A completes_task=true route must still run an open task carrying the
    marker — e.g. a follow-on route re-tagged onto the still-open task (#95
    review). The guard is gated on `not completes_task`."""
    bus = EventBus()
    # default _make_runner route is completes_task=True
    runner, lithos = _make_runner(bus=bus, work_dir=tmp_path)

    await bus.publish(_evt(payload=_payload(metadata={"loom_delivered": True})))
    await _run_for(runner)

    lithos.task_claim.assert_awaited_once()
    lithos.task_complete.assert_awaited_once()
