"""Bundled noop handler for tests and entry-point smoke.

Real handlers — ``obsidian-projection``, ``obsidian-status-transition``,
etc. — are registered via the ``lithos_loom.subscriptions.handlers``
entry-point group.
"""

from __future__ import annotations

from lithos_loom.bus import Event
from lithos_loom.subscriptions import SubscriptionContext


async def handle(event: Event, ctx: SubscriptionContext) -> None:
    """Accept every event. Used by the entry-point discovery test + smoke."""
    ctx.logger.debug("noop: received %s payload=%s", event.type, dict(event.payload))
