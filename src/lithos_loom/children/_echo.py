"""Test-only echo child.

Sleeps until SIGTERM; the supervisor's behavioural tests use this as a
stand-in for real category children. Flags exist purely to drive
failure-path tests:

* ``--config PATH`` — accepted to match the supervisor's invocation contract;
  unused.
* ``--crash-after FLOAT`` — exit non-zero after N seconds (simulate crash).
* ``--ignore-sigterm-for FLOAT`` — install a no-op SIGTERM handler for N
  seconds before re-installing the default handler (simulate a slow-to-stop
  child the supervisor must wait on).
* ``--echo-argv`` — write ``sys.argv`` to stderr on startup for argv tests.

This module deliberately lives under a leading-underscore name so it is
clearly a private testing artefact, not a public child category.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lithos_loom.children._echo")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--crash-after", type=float, default=None)
    parser.add_argument("--ignore-sigterm-for", type=float, default=0.0)
    parser.add_argument("--echo-argv", action="store_true")
    return parser.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    if args.echo_argv:
        print(" ".join(sys.argv), file=sys.stderr, flush=True)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    if args.ignore_sigterm_for > 0:
        # On first SIGTERM, defer the actual stop by ``ignore_sigterm_for``
        # seconds. Subsequent SIGTERMs are no-ops. This simulates a child
        # that takes a moment to honour the signal — used by the supervisor's
        # patient-shutdown and force-kill tests.
        def _defer_stop() -> None:
            loop.call_later(args.ignore_sigterm_for, stop_event.set)
            loop.add_signal_handler(signal.SIGTERM, lambda: None)

        loop.add_signal_handler(signal.SIGTERM, _defer_stop)
    else:
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    loop.add_signal_handler(signal.SIGINT, stop_event.set)

    if args.crash_after is not None:

        async def _crash() -> None:
            await asyncio.sleep(args.crash_after)
            sys.exit(2)

        asyncio.create_task(_crash())

    await stop_event.wait()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
