"""Subscription registry: TOML config → bus subscriptions → retry runner.

* :class:`SubscriptionContext` — shared services injected into every handler
  invocation (Lithos client + scoped logger).
* :class:`SubscriptionRunner` — owns the consumer task that drains a single
  bus :class:`~lithos_loom.bus.Subscription` and dispatches each event to
  its handler with retry-and-friction semantics.
* :func:`build_runners` — turns a tuple of :class:`SubscriptionConfig`
  (parsed from TOML) into a list of ready-to-run
  :class:`SubscriptionRunner` instances. Resolves handler entry-point
  names, compiles ``where`` expressions with restricted globals, and
  registers each subscription on the bus.
* :func:`discover_handlers` — looks up handlers via the
  ``lithos_loom.subscriptions.handlers`` Python entry-point group.

A bundled ``noop`` handler (in :mod:`._noop`) is available for tests and
smoke checks. Real handlers are registered via the same entry-point group.

Idempotency is the handler's responsibility: the bus is fire-and-forget
with bounded buffers, and sources are re-authoritative on restart. The
runner assumes idempotency and never deduplicates events.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.config import RetryPolicy, SubscriptionConfig
from lithos_loom.errors import LithosLoomError

__all__ = [
    "Handler",
    "SubscriptionContext",
    "SubscriptionRunner",
    "build_runners",
    "discover_handlers",
]

_HANDLER_ENTRY_POINT_GROUP = "lithos_loom.subscriptions.handlers"


class Handler(Protocol):
    """Coroutine signature every subscription handler must satisfy."""

    async def __call__(self, event: Event, ctx: SubscriptionContext) -> None: ...


@dataclass
class SubscriptionContext:
    """Shared services injected into every handler invocation.

    ``agent_id`` is the Lithos agent identity the runner uses for
    ``finding_post`` calls (and that handlers can reuse for any other
    Lithos write that requires an ``agent`` field). It must be set
    explicitly so [Friction] posts carry a real agent and the call
    matches the Lithos spec for ``lithos_finding_post``.

    Carries the Lithos client + agent_id + a scoped logger. Additional
    fields can be added here as handlers need them.
    """

    lithos: Any  # LithosClient — Any avoids a heavy import-time cycle
    logger: logging.Logger
    agent_id: str


class SubscriptionRunner:
    """Drains one bus subscription, dispatching with retry + friction."""

    def __init__(
        self,
        spec: SubscriptionConfig,
        handler: Handler,
        subscription: Subscription,
        ctx: SubscriptionContext,
    ) -> None:
        self.spec = spec
        self.handler = handler
        self.subscription = subscription
        self.ctx = ctx

    async def run(self) -> None:
        """Loop forever consuming events. Cancellable."""
        while True:
            event = await self.subscription.queue.get()
            await self._dispatch_with_retry(event)

    async def _dispatch_with_retry(self, event: Event) -> None:
        last_exc: BaseException | None = None
        for attempt in range(self.spec.retry.attempts):
            try:
                await self.handler(event, self.ctx)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                self.ctx.logger.warning(
                    "subscription %s handler attempt %d/%d failed: %r",
                    self.spec.name,
                    attempt + 1,
                    self.spec.retry.attempts,
                    exc,
                )
                if attempt < self.spec.retry.attempts - 1:
                    await asyncio.sleep(_backoff_delay(self.spec.retry, attempt))
        await self._on_persistent_failure(event, last_exc)

    async def _on_persistent_failure(
        self, event: Event, last_exc: BaseException | None
    ) -> None:
        if self.spec.on_persistent_failure == "ignore":
            self.ctx.logger.debug(
                "subscription %s gave up on %s (ignore mode)",
                self.spec.name,
                event.type,
            )
            return

        summary = (
            f"[Friction] subscription {self.spec.name} failed after "
            f"{self.spec.retry.attempts} attempts on {event.type}: {last_exc!r}"
        )

        task_id = ""
        if isinstance(event.payload, Mapping):
            task_id = str(event.payload.get("id", "") or "")

        if not task_id:
            # Non-task event (e.g. obsidian.note.modified, lithos.note.*):
            # there is no Lithos task to scope the finding to. Log loudly so
            # the [Friction] signal is not silently lost. The Lithos spec
            # requires a real task_id for lithos_finding_post (see
            # docs/SPECIFICATION.md §5.4 lithos_finding_post).
            self.ctx.logger.warning("%s (no task_id in event payload)", summary)
            return

        try:
            await self.ctx.lithos.finding_post(
                task_id=task_id,
                agent=self.ctx.agent_id,
                summary=summary,
            )
        except Exception:
            self.ctx.logger.exception(
                "subscription %s: finding_post itself failed for [Friction] post",
                self.spec.name,
            )


def build_runners(
    *,
    bus: EventBus,
    specs: tuple[SubscriptionConfig, ...],
    handlers: Mapping[str, Handler],
    ctx: SubscriptionContext,
) -> list[SubscriptionRunner]:
    """Construct one :class:`SubscriptionRunner` per spec.

    Validates each spec's ``action`` against the supplied handler map and
    compiles each ``where`` expression up-front so misconfiguration fails
    at startup rather than on the first event.
    """
    runners: list[SubscriptionRunner] = []
    for spec in specs:
        handler = handlers.get(spec.action)
        if handler is None:
            available = sorted(handlers)
            raise LithosLoomError(
                f"subscription {spec.name!r} references unknown handler "
                f"{spec.action!r}; available: {available}"
            )
        where_callable: Callable[[Event], bool] | None = None
        if spec.where is not None:
            where_callable = _compile_where(spec.name, spec.where)
        bus_sub = bus.subscribe(
            event_types=spec.event_types,
            match=spec.match,
            where=where_callable,
            name=spec.name,
        )
        runners.append(
            SubscriptionRunner(
                spec=spec, handler=handler, subscription=bus_sub, ctx=ctx
            )
        )
    return runners


def discover_handlers() -> dict[str, Handler]:
    """Load handlers via the ``lithos_loom.subscriptions.handlers`` group."""
    out: dict[str, Handler] = {}
    for ep in importlib.metadata.entry_points(group=_HANDLER_ENTRY_POINT_GROUP):
        out[ep.name] = ep.load()
    return out


# ── where compilation ──────────────────────────────────────────────────


_ALLOWED_PREDICATE_HELPERS: dict[str, Any] = {}


def _compile_where(name: str, expr: str) -> Callable[[Event], bool]:
    try:
        code = compile(expr, f"<subscription {name} where>", "eval")
    except SyntaxError as exc:
        raise LithosLoomError(
            f"subscription {name!r}: where expression is not valid Python: {exc}"
        ) from exc

    globals_: dict[str, Any] = {"__builtins__": {}, **_ALLOWED_PREDICATE_HELPERS}

    def predicate(event: Event) -> bool:
        scope = {"event": event, "task": event.payload}
        return bool(eval(code, globals_, scope))  # noqa: S307 — sandboxed

    return predicate


# ── backoff ────────────────────────────────────────────────────────────


def _backoff_delay(retry: RetryPolicy, attempt: int) -> float:
    """Delay before the *next* attempt, given the just-failed ``attempt``."""
    if retry.backoff == "linear":
        delay = retry.initial_delay_seconds * (attempt + 1)
    else:  # exponential
        delay = retry.initial_delay_seconds * (2**attempt)
    return min(delay, retry.max_delay_seconds)
