"""Subprocess child that hosts the obsidian-sync runtime (Slice 1 US7+).

Spawned by the :class:`~lithos_loom.supervisor.Supervisor` per the
``obsidian-sync`` :class:`~lithos_loom.supervisor.CategorySpec` whenever
the loaded config carries an ``[obsidian_sync]`` section. The supervisor
gate is the presence test; this child is responsible for everything
below that line.

US7 shipped a stub that parked on SIGTERM. US8 replaces the park with
the actual projection: a bus, a Lithos event-stream source, and a
:class:`~lithos_loom.subscriptions.SubscriptionRunner` for each
configured subscription whose ``action`` is in the child's allow-list
(currently just ``"obsidian-projection"``). Subscription actions
outside the allow-list (e.g. generic ``noop``) are silently skipped
here — they're routed to a different child in a future story.

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
from lithos_loom.config import LogLevel, LoomConfig, load_config
from lithos_loom.lithos_client import LithosClient
from lithos_loom.sources.lithos_event_stream import LithosEventStream
from lithos_loom.subscriptions import (
    Handler,
    SubscriptionContext,
    build_runners,
)
from lithos_loom.subscriptions._obsidian_projection import (
    make_handler as make_obsidian_projection_handler,
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
    obsidian_specs = tuple(
        s for s in cfg.subscriptions if s.action == "obsidian-projection"
    )
    # Fail fast on duplicates (Copilot review on #17): the handler is
    # stateful (per-handler state dict + per-handler tasks_file path);
    # two subscriptions pointing at the same handler would merge their
    # projections into one in-memory state and race on the same file.
    if len(obsidian_specs) > 1:
        names = ", ".join(s.name for s in obsidian_specs)
        logger.error(
            "obsidian-sync: refusing to wire %d obsidian-projection "
            "subscriptions (%s); the handler is stateful and only one "
            "instance is supported per child",
            len(obsidian_specs),
            names,
        )
        return 1

    # Short-circuit when there's nothing to wire (Copilot review on
    # #17): no point opening a LithosClient or running the SSE source
    # if no obsidian subscription is configured. Just install signal
    # handlers and park.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
            installed.append(sig)

    try:
        if not obsidian_specs:
            logger.warning(
                "obsidian-sync: no obsidian-projection subscription "
                "configured; child will idle until SIGTERM. Add a "
                "[[subscriptions]] block with action='obsidian-projection' "
                "to enable projection."
            )
            await stop_event.wait()
            return 0

        spec = obsidian_specs[0]
        logger.info("obsidian-sync: wiring subscription %r", spec.name)
        my_handlers: dict[str, Handler] = {
            "obsidian-projection": make_obsidian_projection_handler(cfg),
        }

        async with LithosClient(
            cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
        ) as lithos:
            events_url = cfg.orchestrator.lithos_url.rstrip("/") + "/events"
            # Pull resolved-task history into the bootstrap so the
            # US13 TTL-lingering window survives daemon restart (PR #21
            # review issue 1). The source over-fetches completed +
            # cancelled tasks at bootstrap and filters by completed_at
            # >= now - window before publishing them as terminal events.
            source = LithosEventStream(
                client=lithos,
                bus=EventBus(),
                events_url=events_url,
                bootstrap_resolved_window=timedelta(days=obs.resolved_ttl_days),
            )
            # Re-bind the source's bus locally so the runners share it.
            bus = source.bus
            ctx = SubscriptionContext(
                lithos=lithos,
                logger=logging.getLogger("lithos_loom.subscriptions"),
                agent_id=cfg.orchestrator.agent_id,
            )
            runners = build_runners(
                bus=bus,
                specs=obsidian_specs,
                handlers=my_handlers,
                ctx=ctx,
            )

            tasks: list[asyncio.Task[None]] = [
                asyncio.create_task(source.run(), name="lithos-event-stream"),
                *(
                    asyncio.create_task(r.run(), name=f"sub-{r.spec.name}")
                    for r in runners
                ),
            ]
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
