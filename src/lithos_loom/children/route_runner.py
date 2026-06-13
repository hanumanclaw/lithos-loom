"""Subprocess child that runs the bus + LithosEventStream + RouteRunners.

Spawned by the :class:`~lithos_loom.supervisor.Supervisor` per the
``route-runner`` :class:`~lithos_loom.supervisor.CategorySpec`. Owns one
:class:`~lithos_loom.bus.EventBus`, one
:class:`~lithos_loom.sources.lithos_event_stream.LithosEventStream`
consuming Lithos's ``/events`` SSE channel, and one
:class:`~lithos_loom.subscriptions.route_runner.RouteRunner` per
configured route. Runs until SIGTERM/SIGINT.

Invocation contract (set by the supervisor):

    python -m lithos_loom.children.route_runner --config <path>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from lithos_loom.bus import EventBus
from lithos_loom.config import LogLevel, LoomConfig, load_config
from lithos_loom.cursor_store import CursorStore
from lithos_loom.lithos_client import LithosClient
from lithos_loom.sources.lithos_event_stream import LithosEventStream
from lithos_loom.subscriptions.route_runner import RouteRunner

_LEVEL_MAP: dict[LogLevel, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

# Loggers we demote unless the operator has explicitly asked for debug
# output. httpx logs every HTTP request at INFO — one POST per MCP tool
# call plus the SSE GET — which drowns out the source + subscriber
# lifecycle. At debug level we leave them alone so the operator gets the
# full picture.
_NOISY_LIBRARY_LOGGERS = ("httpx", "httpx_sse")


def _configure_logging(level: LogLevel) -> None:
    """Set up root logging at the configured level and silence noisy libs.

    When ``level`` is ``"debug"`` the library loggers are left at the
    root level so every HTTP request surfaces — operators asking for
    debug want the full firehose. At any other level the library logs
    are pinned to WARNING so they don't drown the application logs.
    """
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
    # The MCP SDK's SSE reader (``mcp.client.sse.sse_reader``) logs a
    # full ERROR-level traceback whenever its persistent session is
    # torn down — e.g. when Lithos restarts. The route-runner holds
    # its own long-lived LithosClient (line ~87 below), so the same
    # SDK traceback noise would fire here too without this pin. Our
    # reconnect loops and subscription retry policy own actual
    # recovery; the SDK trace is just noise. CRITICAL so real
    # auth/protocol failures still surface. Mirrors the matching
    # pin in :mod:`lithos_loom.children.obsidian_sync`.
    logging.getLogger("mcp.client.sse").setLevel(logging.CRITICAL)


logger = logging.getLogger(__name__)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lithos_loom.children.route_runner")
    parser.add_argument("--config", type=Path, default=None)
    return parser.parse_args(argv)


async def _amain(cfg: LoomConfig) -> int:
    if not cfg.routes:
        logger.info("route-runner child: no routes configured; exiting cleanly")
        return 0

    bus = EventBus()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    cursor_store = CursorStore(
        cfg.orchestrator.work_dir / "route-runner" / "sse_cursors.json"
    )

    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as lithos:
        events_url = cfg.orchestrator.lithos_url.rstrip("/") + "/events"
        source = LithosEventStream(
            client=lithos,
            bus=bus,
            events_url=events_url,
            cursor_store=cursor_store,
            cursor_name="task-events",
        )
        project_repos = {slug: pc.repo for slug, pc in cfg.projects.items()}
        runners = [
            RouteRunner(
                route=route,
                bus=bus,
                lithos=lithos,
                agent_id=cfg.orchestrator.agent_id,
                work_dir_base=cfg.orchestrator.work_dir,
                retain_failed_workdirs=cfg.orchestrator.retain_failed_workdirs,
                project_repos=project_repos,
            )
            for route in cfg.routes
        ]
        logger.info(
            "route-runner child: starting event-stream + %d route runners (%s)",
            len(runners),
            ", ".join(r.route.name for r in runners),
        )

        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(source.run(), name="lithos-event-stream"),
            *(
                asyncio.create_task(r.run(), name=f"route-{r.route.name}")
                for r in runners
            ),
        ]

        try:
            await stop_event.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    # Load config first so we know what level to configure. Any
    # ConfigError that escapes here surfaces via Python's default
    # last-resort stderr handler before logging is up.
    # NB: the manual-testing branch (now on main) did basicConfig + httpx
    # silencing inline here; that's superseded by _configure_logging
    # below, which honours cfg.orchestrator.log_level and additionally
    # silences httpx_sse.
    cfg = load_config(args.config)
    _configure_logging(cfg.orchestrator.log_level)
    try:
        return asyncio.run(_amain(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
