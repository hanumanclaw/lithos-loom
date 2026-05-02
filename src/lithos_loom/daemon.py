"""Daemon poll/claim/dispatch loop.

Stub — implements ``docs/prd/mvp.md`` US-5, US-7, US-29:

* Poll ``lithos_task_list(status='open')`` every ``poll_interval_seconds``
* Match against routes (tag-based, first match wins)
* Claim collision-safely via ``lithos_task_claim``
* Renew claims for long-running plugins (US-7)
* Handle SIGTERM gracefully — finish in-flight, exit (US-29)
* Release stale claims by own ``agent_id`` on startup
"""

from __future__ import annotations

from lithos_loom.config import LoomConfig


async def run(cfg: LoomConfig) -> None:
    """Run the daemon. Stub — implement per docs/prd/mvp.md US-5 / US-7 / US-29."""
    raise NotImplementedError(
        f"daemon.run — implement poll/claim loop using {cfg.orchestrator.lithos_url}"
    )
