"""Subprocess child that hosts the obsidian-sync runtime (Slice 1 US7+).

Spawned by the :class:`~lithos_loom.supervisor.Supervisor` per the
``obsidian-sync`` :class:`~lithos_loom.supervisor.CategorySpec` whenever
the loaded config carries an ``[obsidian_sync]`` section. The supervisor
gate is the presence test; this child is responsible for everything
below that line.

US7 ships a stub: the child loads its config, logs operator-visible
startup detail, and parks on SIGTERM. US8 onwards will replace the park
with the bus + ``obsidian-projection`` subscription (and Slice 2+ adds
the filesystem watcher and push subscriptions).

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
from pathlib import Path

from lithos_loom.config import LogLevel, LoomConfig, load_config

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

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
            installed.append(sig)
    try:
        await stop_event.wait()
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
