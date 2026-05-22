"""Subprocess child that hosts the obsidian-sync runtime (Slice 1 US7+).

Spawned by the :class:`~lithos_loom.supervisor.Supervisor` per the
``obsidian-sync`` :class:`~lithos_loom.supervisor.CategorySpec` whenever
the loaded config carries an ``[obsidian_sync]`` section. The supervisor
gate is the presence test; this child is responsible for everything
below that line.

US7 shipped a stub that parked on SIGTERM. US8 replaced the park with
the actual projection: a bus, a Lithos event-stream source, and a
:class:`~lithos_loom.subscriptions.SubscriptionRunner` for each
configured subscription whose ``action`` is in the child's allow-list.

Slice 2 extends the allow-list. The actions this child hosts (see
:data:`_CHILD_ACTIONS`) are now:

* ``"obsidian-projection"`` — renders projected task lines into
  ``_lithos/tasks.md`` (Slice 1 US8+).
* ``"obsidian-status-transition"`` — consumes the fs watcher's
  ``obsidian.task.status_changed`` events and pushes the
  corresponding action to Lithos (Slice 2 US17+).

Subscription actions outside the allow-list (e.g. generic ``noop``)
are silently skipped here — they're routed to a different child in a
future story.

Invocation contract (set by the supervisor):

    python -m lithos_loom.children.obsidian_sync --config <path>
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from lithos_loom.bus import EventBus
from lithos_loom.config import LogLevel, LoomConfig, SubscriptionConfig, load_config
from lithos_loom.lithos_client import LithosClient
from lithos_loom.sources.lithos_event_stream import LithosEventStream
from lithos_loom.sources.obsidian_fs_watcher import ObsidianFsWatcher
from lithos_loom.subscriptions import (
    Handler,
    SubscriptionContext,
    build_runners,
)
from lithos_loom.subscriptions._obsidian_projection import (
    make_handler as make_obsidian_projection_handler,
)
from lithos_loom.subscriptions._obsidian_status_transition import (
    handle as handle_obsidian_status_transition,
)
from lithos_loom.sync_state import ProjectionSyncState

# Actions this child is willing to host. Subscriptions whose ``action``
# is outside this set are silently skipped — some other child's job
# (route-runner for routes, a future generic subscription-runner for
# things like ``noop``).
_CHILD_ACTIONS: frozenset[str] = frozenset(
    {"obsidian-projection", "obsidian-status-transition"}
)

_LEVEL_MAP: dict[LogLevel, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

logger = logging.getLogger(__name__)


def _configure_logging(level: LogLevel) -> None:
    logging.basicConfig(
        level=_LEVEL_MAP[level],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lithos_loom.children.obsidian_sync")
    parser.add_argument("--config", type=Path, default=None)
    return parser.parse_args(argv)


async def _amain(cfg: LoomConfig) -> int:
    if cfg.obsidian_sync is None:
        # Defensive: the supervisor's spawn gate is `cfg.obsidian_sync
        # is not None`, so reaching here means a config reload removed
        # the section underneath us, or someone invoked the module
        # directly with the wrong config. Exit non-zero so the
        # supervisor sees the discrepancy rather than us silently
        # parking.
        logger.error("obsidian-sync spawned without [obsidian_sync] config; exiting")
        return 1

    obs = cfg.obsidian_sync
    logger.info(
        "obsidian-sync child started; vault=%s tasks_file=%s "
        "resolved_ttl_days=%d include_blocked=%s exclude_tags=%s",
        obs.vault_path,
        obs.tasks_file,
        obs.resolved_ttl_days,
        obs.include_blocked,
        list(obs.exclude_tags) or "[]",
    )

    # Filter cfg.subscriptions to the actions this child is willing to
    # host. Other actions are some other child's job (route-runner for
    # routes; a future subscription-runner child for generic actions
    # like `noop`).
    child_specs = tuple(s for s in cfg.subscriptions if s.action in _CHILD_ACTIONS)
    # Fail fast on duplicate specs of the same action. The projection
    # handler is stateful (per-handler state dict + per-handler
    # tasks_file path) so two specs would race on the same file. The
    # status-transition handler is stateless but two specs would still
    # mean duplicate Lithos calls per event, which is never what the
    # operator wanted.
    by_action: dict[str, list[SubscriptionConfig]] = {}
    for spec in child_specs:
        by_action.setdefault(spec.action, []).append(spec)
    for action, specs in by_action.items():
        if len(specs) > 1:
            names = ", ".join(s.name for s in specs)
            logger.error(
                "obsidian-sync: refusing to wire %d %s subscriptions (%s); "
                "only one instance per action is supported per child",
                len(specs),
                action,
                names,
            )
            return 1

    projection_specs = by_action.get("obsidian-projection", [])
    status_transition_specs = by_action.get("obsidian-status-transition", [])
    projection_spec = projection_specs[0] if projection_specs else None
    status_transition_spec = (
        status_transition_specs[0] if status_transition_specs else None
    )

    if status_transition_spec is not None and projection_spec is None:
        # The status-transition handler needs the projection's
        # ``sync_state.task_status_markers`` to be populated for the
        # fs watcher to emit any events at all (see
        # ObsidianFsWatcher.poll_once — tasks with ``prior is None``
        # are silently skipped). The configuration is permitted but
        # inert; call that out at startup rather than letting the
        # operator wonder why ticks don't push.
        logger.warning(
            "obsidian-sync: %r is configured but no obsidian-projection "
            "subscription is present. The handler will load but never "
            "fire, because the projection is what populates the marker "
            "baseline the fs watcher reads against.",
            status_transition_spec.name,
        )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
            installed.append(sig)

    try:
        # Slice 2 US16/US23 wire the fs watcher as a first-class source
        # whose lifecycle is gated on ``[obsidian_sync]`` alone, not on
        # whether a projection subscription is present. Without
        # projection state to compare against, the watcher's per-task
        # transition check never fires (every parsed task has
        # ``prior is None``) and no events are published — runtime
        # behaviour is identical to the previous "idle and warn" path,
        # but the source itself is now independently spawnable so a
        # future story (e.g. Slice 3's capture macro populating
        # ``sync_state`` by another route) doesn't have to re-plumb
        # the spawn gate.
        sync_state = ProjectionSyncState()
        bus = EventBus()
        fs_watcher = ObsidianFsWatcher(
            bus=bus,
            tasks_path=obs.vault_path / obs.tasks_file,
            sync_state=sync_state,
        )
        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(fs_watcher.run(), name="obsidian-fs-watcher"),
        ]

        if not child_specs:
            logger.warning(
                "obsidian-sync: no obsidian-projection or "
                "obsidian-status-transition subscription configured; "
                "fs watcher runs but emits nothing without projection "
                "state. Add a [[subscriptions]] block with "
                "action='obsidian-projection' to populate it."
            )
            try:
                await stop_event.wait()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            return 0

        my_handlers: dict[str, Handler] = {}
        if projection_spec is not None:
            logger.info("obsidian-sync: wiring subscription %r", projection_spec.name)
            # 50ms debounce coalesces bursts of bus events (especially
            # the source's bootstrap, which can fire dozens of created
            # events in quick succession) into a single flush at
            # quiescence. The disk-seeded content-hash check then turns
            # a quiet-KB restart into zero on-disk writes (US14
            # "idempotent re-runs").
            my_handlers["obsidian-projection"] = make_obsidian_projection_handler(
                cfg, debounce_seconds=0.05, sync_state=sync_state
            )
        if status_transition_spec is not None:
            logger.info(
                "obsidian-sync: wiring subscription %r",
                status_transition_spec.name,
            )
            my_handlers["obsidian-status-transition"] = (
                handle_obsidian_status_transition
            )

        # LithosClient is needed for both: the projection wires through
        # to LithosEventStream for upstream events; the status-transition
        # handler calls task_complete / task_cancel / finding_post.
        # LithosEventStream only spawns when projection is configured —
        # status-transition consumes obsidian-side events only.
        need_event_stream = projection_spec is not None
        async with LithosClient(
            cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
        ) as lithos:
            ctx = SubscriptionContext(
                lithos=lithos,
                logger=logging.getLogger("lithos_loom.subscriptions"),
                agent_id=cfg.orchestrator.agent_id,
            )
            runners = build_runners(
                bus=bus,
                specs=child_specs,
                handlers=my_handlers,
                ctx=ctx,
            )

            if need_event_stream:
                events_url = cfg.orchestrator.lithos_url.rstrip("/") + "/events"
                # Pull resolved-task history into the bootstrap so the
                # US13 TTL-lingering window survives daemon restart
                # (PR #21 review issue 1). The source fetches
                # completed + cancelled tasks at bootstrap via Lithos's
                # server-side resolved_since filter (lithos#286) before
                # publishing them as terminal events.
                source = LithosEventStream(
                    client=lithos,
                    bus=bus,
                    events_url=events_url,
                    bootstrap_resolved_window=timedelta(days=obs.resolved_ttl_days),
                )
                tasks.append(
                    asyncio.create_task(source.run(), name="lithos-event-stream")
                )
            tasks.extend(
                asyncio.create_task(r.run(), name=f"sub-{r.spec.name}") for r in runners
            )
            try:
                await stop_event.wait()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # Mirror the supervisor's install/uninstall pair so the test
        # process's event loop isn't left with handlers attached after
        # _amain returns (Copilot review on #16).
        for sig in installed:
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(sig)
        logger.info("obsidian-sync child stopping")
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
