"""Subprocess child that hosts the github-issue-watcher runtime.

Spawned by the :class:`~lithos_loom.supervisor.Supervisor` whenever
the loaded config carries a ``[github_watcher]`` table with
``enabled = true``. The supervisor gate is presence + enabled; this
child is responsible for everything below that line.

Single source + single subscription action. No allow-list filtering
because the watcher subscription is auto-wired here (not declared in
``[[subscriptions]]``) — the operator just flips the gate on and the
child sources, subscribes, and runs.

Invocation contract (set by the supervisor)::

    python -m lithos_loom.children.github_watcher --config <path>
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx

from lithos_loom.bus import Event, EventBus
from lithos_loom.config import LogLevel, LoomConfig, load_config
from lithos_loom.github_client import GitHubClient
from lithos_loom.lithos_client import LithosClient
from lithos_loom.sources.github_issue_watcher import GitHubIssueWatcher
from lithos_loom.sources.lithos_event_stream import LithosEventStream
from lithos_loom.sources.lithos_note_stream import LithosNoteStream
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._github_issue_push import (
    EVENT_TYPES as LITHOS_TASK_EVENT_TYPES,
)
from lithos_loom.subscriptions._github_issue_push import (
    make_handler as make_github_issue_push_handler,
)
from lithos_loom.subscriptions._github_issue_sync import (
    EVENT_TYPE as GITHUB_ISSUE_EVENT_TYPE,  # noqa: F401 — re-exported for the child wiring smoke test
)
from lithos_loom.subscriptions._github_issue_sync import (
    make_handler as make_github_issue_sync_handler,
)

_LEVEL_MAP: dict[LogLevel, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

# Mirror route_runner: httpx logs every HTTP request at INFO — every
# Lithos MCP POST AND every GitHub API GET/PATCH — which drowns out the
# watcher's own per-cycle progress messages. At ``debug`` the operator
# asked for the firehose; otherwise pin to WARNING.
_NOISY_LIBRARY_LOGGERS = ("httpx", "httpx_sse")

logger = logging.getLogger(__name__)


def _configure_logging(level: LogLevel) -> None:
    logging.basicConfig(
        level=_LEVEL_MAP[level],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if level == "debug":
        for name in _NOISY_LIBRARY_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)
    else:
        for name in _NOISY_LIBRARY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
    # Same noise suppression as obsidian-sync — the MCP SDK logs a
    # full traceback every Lithos reconnect.
    logging.getLogger("mcp.client.sse").setLevel(logging.CRITICAL)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lithos_loom.children.github_watcher")
    parser.add_argument("--config", type=Path, default=None)
    return parser.parse_args(argv)


async def _run_reconcile_pass(
    *,
    lithos: LithosClient,
    push_handler: object,
    ctx: SubscriptionContext,
    resolved_window: timedelta | None,
) -> None:
    """Single pass of the periodic Lithos→GH reconciliation sweep.

    Surfaces:

    - **Open** tasks (always): dispatches a synthetic ``task.updated``
      so title drift is mirrored.
    - **Terminal** tasks (completed + cancelled) resolved within
      ``resolved_window``: dispatches ``task.updated`` (title drift)
      *and* the matching close event so a long outage that lost both
      a rename and a completion still gets both halves mirrored.
      Skipped entirely when ``resolved_window`` is ``None`` (operator
      set ``resolved_replay_days = 0`` to opt out of resolved replay
      — PR-review finding 1, round 6, 2026-05-30: a ``None`` window
      previously meant "no time bound" and the sweep walked every
      terminal task ever, growing unboundedly with each cycle).

    The push handler is idempotent (re-fetches GH before each PATCH)
    so the sweep is a no-op when everything is already in sync.
    """
    handler = cast("Callable[[Event, SubscriptionContext], Any]", push_handler)
    now = datetime.now(UTC)

    async def _dispatch_one(task: Any, event_type: str) -> None:
        event = Event(
            type=event_type,
            timestamp=now,
            payload={
                "id": task.id,
                "title": task.title,
                "status": task.status,
                "tags": list(task.tags),
                "metadata": dict(task.metadata),
                "claims": [dict(c) for c in task.claims],
                "resolved_at": (
                    task.resolved_at.isoformat()
                    if task.resolved_at is not None
                    else None
                ),
            },
        )
        try:
            await handler(event, ctx)
        except Exception as exc:
            logger.warning(
                "[Friction] github-watcher: reconcile-sweep dispatch for "
                "task %s (%s) failed: %s: %s",
                task.id,
                event_type,
                type(exc).__name__,
                exc,
            )

    counts = {"open": 0, "completed": 0, "cancelled": 0}
    open_tasks = await lithos.task_list(status="open")
    for task in open_tasks:
        if task.metadata.get("github_issue_url"):
            counts["open"] += 1
            await _dispatch_one(task, "lithos.task.updated")

    if resolved_window is None:
        logger.info(
            "github-watcher: reconcile sweep replayed %d open GH-linked "
            "task(s); resolved-task sweep disabled (resolved_replay_days=0)",
            counts["open"],
        )
        return

    since = now - resolved_window
    for terminal_status, event_type in (
        ("completed", "lithos.task.completed"),
        ("cancelled", "lithos.task.cancelled"),
    ):
        tasks = await lithos.task_list(status=terminal_status, resolved_since=since)
        for task in tasks:
            if task.metadata.get("github_issue_url"):
                counts[terminal_status] += 1
                # PR-review finding 2, round 6, 2026-05-30: dispatch
                # ``task.updated`` *before* the close event so title
                # drift on a terminal task is reconciled even when the
                # original rename was dropped during a long outage.
                # The handler is idempotent for either event in
                # isolation; running both pays one extra get_issue per
                # task in steady state but closes the title-drift gap.
                await _dispatch_one(task, "lithos.task.updated")
                await _dispatch_one(task, event_type)

    logger.info(
        "github-watcher: reconcile sweep replayed %d open / %d completed / "
        "%d cancelled GH-linked task(s)",
        counts["open"],
        counts["completed"],
        counts["cancelled"],
    )


async def _amain(cfg: LoomConfig) -> int:
    """Body of the child. Returns the exit code."""
    if cfg.github_watcher is None or not cfg.github_watcher.enabled:
        # Defensive: the supervisor gate is the same condition. If we
        # land here, config drift removed the gate underneath us.
        logger.error(
            "github-watcher spawned without [github_watcher] enabled=true; exiting"
        )
        return 1

    gh_cfg = cfg.github_watcher
    logger.info(
        "github-watcher child started; poll_interval=%ds coord_doc=%s",
        gh_cfg.poll_interval_seconds,
        gh_cfg.coord_doc_path,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
            installed.append(sig)

    try:
        bus = EventBus()
        events_url = cfg.orchestrator.lithos_url.rstrip("/") + "/events"
        async with (
            httpx.AsyncClient(timeout=30.0) as http,
            LithosClient(
                cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
            ) as lithos,
        ):
            github = await GitHubClient.create(http=http)
            ctx = SubscriptionContext(
                lithos=lithos,
                logger=logging.getLogger("lithos_loom.subscriptions"),
                agent_id=cfg.orchestrator.agent_id,
            )
            sync_handler = make_github_issue_sync_handler(github)

            async def dispatch_to_sync_handler(event: object) -> None:
                """Inline dispatch path for the GH→Lithos sync handler.

                Bypasses the bus so a queue-full drop can't lose work and
                the watcher source can hold its cursor at the last
                successfully reconciled issue (PR-review finding 1,
                2026-05-30). The handler still posts [Friction] logs
                internally for recoverable problems; any unhandled
                exception bubbles up, the watcher logs it, and the
                cursor freezes — the next poll re-fetches from the
                stuck boundary.
                """
                from lithos_loom.bus import Event as _Event

                assert isinstance(event, _Event)
                await sync_handler(event, ctx)

            watcher = GitHubIssueWatcher(
                github=github,
                lithos=lithos,
                bus=bus,
                poll_interval_seconds=gh_cfg.poll_interval_seconds,
                coord_doc_path=gh_cfg.coord_doc_path,
                agent_id=cfg.orchestrator.agent_id,
                dispatch=dispatch_to_sync_handler,
            )

            # LithosNoteStream feeds the watcher's _refresh_loop so an
            # operator running `project enable-github <slug>` takes
            # effect without a daemon restart.
            note_stream = LithosNoteStream(
                client=lithos,
                bus=bus,
                events_url=events_url,
            )

            # LithosEventStream is the push half: it surfaces
            # task.{completed,cancelled,updated} onto the in-process bus
            # so the push handler can mirror those into the linked GH
            # issue. Replay recently-resolved tasks at bootstrap so a
            # Lithos task that closed (or got renamed) while the watcher
            # was down still gets mirrored to GH on restart. The GH→Lithos
            # polling direction is one-way (it can detect a Lithos task
            # gone terminal but doesn't itself close the GH issue — it
            # only posts a [ReopenRequested] finding on the reverse). The
            # push handler is idempotent (refetches before PATCH), so a
            # too-large window only costs harmless re-checks.
            replay_window = (
                timedelta(days=gh_cfg.resolved_replay_days)
                if gh_cfg.resolved_replay_days > 0
                else None
            )
            event_stream = LithosEventStream(
                client=lithos,
                bus=bus,
                events_url=events_url,
                bootstrap_resolved_window=replay_window,
            )

            push_handler = make_github_issue_push_handler(github)
            # PR-review finding 3 (round 3, 2026-05-30): the previous
            # 512-slot queue would drop events deterministically on a
            # large terminal-event burst (bootstrap of a busy KB). 8192
            # is generous enough to absorb realistic bursts while staying
            # well under the bus's bounded-queue invariant. Pair with
            # transient-vs-permanent error classification + retry below
            # so transient GH failures (5xx, network blips) don't get
            # discarded.
            push_sub = bus.subscribe(
                event_types=LITHOS_TASK_EVENT_TYPES,
                name="github-issue-push",
                queue_size=8192,
            )

            async def consume_push() -> None:
                """Drain the Lithos→GH subscription with transient-error retry.

                Push handler raises on transient GH errors (5xx, network,
                rate-limit exhausted). Permanent errors (auth, 404) are
                logged + swallowed inside the handler. Here we retry
                transient failures with exponential backoff capped at
                ``max_delay_seconds``. With 8 attempts and a 60s cap the
                inter-attempt waits are 2, 4, 8, 16, 32, 60, 60s
                (182s ≈ 3 minutes total before drop) — wide enough to
                absorb realistic short outages and GH 5xx flares
                (PR-review finding 3, round 4, 2026-05-30: round-3's
                3-attempt cap discarded events after only ~6 seconds,
                which lost work to anything longer than a transient
                hiccup). Outages longer than this fall through to the
                periodic reconciliation sweep + ``LithosEventStream``
                bootstrap replay window on daemon restart.
                """
                max_attempts = 8
                max_delay_seconds = 60
                while True:
                    event = await push_sub.queue.get()
                    for attempt in range(1, max_attempts + 1):
                        try:
                            await push_handler(event, ctx)
                            break
                        except Exception as exc:
                            if attempt >= max_attempts:
                                logger.warning(
                                    "[Friction] github-watcher: push handler "
                                    "exhausted %d attempts on %s "
                                    "(%s: %s); dropping (outage outlasts "
                                    "retry budget — periodic reconcile "
                                    "sweep within reconcile_interval_minutes "
                                    "or next daemon restart within "
                                    "resolved_replay_days will replay it)",
                                    max_attempts,
                                    event.type,
                                    type(exc).__name__,
                                    exc,
                                )
                                break
                            delay = min(2**attempt, max_delay_seconds)
                            logger.info(
                                "github-watcher: push handler attempt %d/%d "
                                "failed (%s); retrying in %ds",
                                attempt,
                                max_attempts,
                                type(exc).__name__,
                                delay,
                            )
                            await asyncio.sleep(delay)

            reconcile_seconds = gh_cfg.reconcile_interval_minutes * 60

            async def periodic_reconcile() -> None:
                """Re-dispatch every GH-linked Lithos task through the push handler.

                PR-review finding 4 (round 5, 2026-05-30): closes the
                "outage outlasts the in-memory retry budget" gap. The
                push consumer's 8-attempt / ~3-minute backoff (waits
                2/4/8/16/32/60/60 s ≈ 182 s total) drops events that
                survive longer outages; without this loop they only
                recover on next daemon restart inside the
                ``resolved_replay_days`` window. Every
                ``reconcile_interval_minutes`` the loop scans Lithos
                for open + recently-resolved tasks carrying
                ``metadata.github_issue_url`` and replays each one
                through the push handler. The handler is idempotent
                (re-fetches GH before PATCH) so the sweep is a no-op
                when everything is already in sync.

                Skipped entirely when ``reconcile_interval_minutes`` is
                zero (operator opted out).
                """
                if reconcile_seconds <= 0:
                    logger.info(
                        "github-watcher: periodic reconcile disabled "
                        "(reconcile_interval_minutes=0)"
                    )
                    return
                # First fire: wait a full interval so the bootstrap
                # replay from LithosEventStream has time to settle
                # before we redundantly re-scan everything.
                await asyncio.sleep(reconcile_seconds)
                window = (
                    timedelta(days=gh_cfg.resolved_replay_days)
                    if gh_cfg.resolved_replay_days > 0
                    else None
                )
                while True:
                    try:
                        await _run_reconcile_pass(
                            lithos=lithos,
                            push_handler=push_handler,
                            ctx=ctx,
                            resolved_window=window,
                        )
                    except Exception:
                        logger.exception(
                            "github-watcher: periodic reconcile pass raised"
                        )
                    await asyncio.sleep(reconcile_seconds)

            tasks: list[asyncio.Task[None]] = [
                asyncio.create_task(note_stream.run(), name="lithos-note-stream"),
                asyncio.create_task(event_stream.run(), name="lithos-event-stream"),
                asyncio.create_task(watcher.run(), name="github-issue-watcher"),
                asyncio.create_task(consume_push(), name="github-issue-push-consumer"),
                asyncio.create_task(periodic_reconcile(), name="github-push-reconcile"),
            ]
            try:
                await stop_event.wait()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for sig in installed:
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(sig)
        logger.info("github-watcher child stopping")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = load_config(args.config)
    _configure_logging(cfg.orchestrator.log_level)
    try:
        return asyncio.run(_amain(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
