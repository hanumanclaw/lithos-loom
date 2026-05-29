"""Thin async client over the Lithos MCP surface.

The Lithos server speaks MCP over SSE at ``<base_url>/sse`` (FastMCP
default). This module wraps the ``mcp`` SDK's ``sse_client`` +
``ClientSession`` into a lifetime-managed object that exposes only the
calls Loom actually needs.

Slice 0 surface (US3):

* :class:`Task` — frozen dataclass with the fields the poller reads.
* :class:`LithosClient` — async context manager owning an MCP session.
* :meth:`LithosClient.task_list` — minimum ``lithos_task_list`` call.

Story 5 (route-runner subscriber) will extend this with ``task_claim``,
``task_release``, ``task_renew``, ``task_complete``, ``task_update``, and
``finding_post``. Other tools land as they're needed.

Errors returned by Lithos as ``{status: "error", code, message}`` envelopes
surface as :class:`LithosClientError` so callers can switch on
``exc.code``. Unhealthy MCP transport surfaces are left to propagate as the
underlying ``mcp`` SDK exceptions — the daemon's outer loop logs and
continues on transient failures.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Final, Literal

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import CallToolResult

from lithos_loom.errors import LithosClientError

logger = logging.getLogger(__name__)

# Dead-session recovery (#43). When Lithos restarts, the long-lived
# MCP-over-SSE session held by the daemon's shared client goes dead and
# every subsequent call_tool fails. ``_invoke`` re-establishes the
# session and retries, bounded, so the daemon recovers without a restart.
_MAX_TRANSPORT_ATTEMPTS = 3
_RECONNECT_BACKOFF_SECONDS = 0.5


class _ShutdownSentinel:
    """Type-safe sentinel queued by :meth:`LithosClient.__aexit__` to tell
    the keeper to stop. Using a class (vs ``None``) keeps the queue's type
    annotation precise: ``Queue[_ReconnectRequest | _ShutdownSentinel]``.
    """


_SHUTDOWN: Final = _ShutdownSentinel()


_invoke_retried: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "lithos_client_invoke_retried", default=False
)
"""Per-task flag set True by :meth:`LithosClient._invoke` if the call needed
a transport retry (#43). Callers that need to disambiguate
"server-committed-but-response-lost" from "first-attempt failure" check
this immediately after ``_invoke`` returns or raises — currently only
:meth:`LithosClient.task_claim`, whose ``claim_failed`` ownership
re-check is gated on retry-occurred to avoid falsely treating an
unrelated same-agent collision as our own committed claim (PR #60
review, 2026-05-29).

Reset at the start of every ``_invoke`` call, so the flag only reflects
the most recent call's retry state. Read into a local *before* any
follow-up ``_invoke`` (e.g. ``task_status``), which would clobber it.
"""


@dataclass
class _ReconnectRequest:
    """One reconnect ask from a caller to the keeper.

    ``expected_gen`` is the session generation the caller observed before
    its ``call_tool`` failed. The keeper compares against the current
    generation under serial processing — if a peer already reconnected,
    this request is a no-op and just signals ``done`` so the caller can
    retry against the already-fresh session.
    """

    expected_gen: int
    done: asyncio.Event = field(default_factory=asyncio.Event)
    error: BaseException | None = None


__all__ = ["LithosClient", "Note", "NoteSummary", "Task", "WriteResult"]


WriteStatus = Literal[
    "created",
    "updated",
    "duplicate",
    "version_conflict",
    "slug_collision",
    "invalid_input",
    "content_too_large",
    "error",
]
"""All terminal status values ``lithos_write`` can return.

Mirrors the Lithos-side ``WriteOutcome`` enum (``lithos/src/lithos/intake.py``).
``created`` / ``updated`` are success paths; everything else means the
caller has to decide what to do. ``version_conflict`` and
``slug_collision`` are the two cases the bidirectional-sync path
(Slice 5) reacts to programmatically.
"""


@dataclass(frozen=True)
class Task:
    """A Lithos task as returned by ``lithos_task_list``,
    ``lithos_task_status``, and ``lithos_task_get`` (lithos#294).

    Field set mirrors the full Lithos task envelope so handlers can
    read any persisted field without a plumbing PR. ``resolved_at`` is
    the canonical Lithos timestamp for terminal-state transitions —
    written by both ``complete_task`` and ``cancel_task`` (lithos#286
    / PR #288, which also renamed it from ``completed_at`` server-side
    with no BC alias). The obsidian-projection handler uses it as the
    resolution-date anchor for ``✅``/``❌`` markers and TTL eviction
    (US13).

    ``description``, ``created_by``, ``created_at``, and ``outcome``
    were added in lithos#294 (full task record on status + new
    ``lithos_task_get`` tool). They default to falsy values so the
    parser stays backwards-compatible with pre-#294 servers that
    don't return them.
    """

    id: str
    title: str
    status: str  # open | completed | cancelled
    tags: tuple[str, ...]
    metadata: Mapping[str, Any]
    claims: tuple[Mapping[str, Any], ...]
    resolved_at: datetime | None = None
    description: str | None = None
    created_by: str = ""
    created_at: datetime | None = None
    outcome: str | None = None


@dataclass(frozen=True)
class Note:
    """A full Lithos KB document as returned by ``lithos_read``
    (Slice 4 + 5).

    Field set carries everything the projection layer needs to render
    a vault file with frontmatter: identity (``id``, ``path``,
    ``slug``), versioning (``version``, ``updated_at``), body, and
    the metadata fields the operator's queries rely on (``status``,
    ``tags``, ``note_type``). ``slug`` is derived server-side from
    the path's first segment under ``projects/`` and exposed here as
    a convenience so callers don't have to re-parse it.

    Frozen + Mapping-typed ``metadata.extra`` so subscription handlers
    can read additional persisted fields without a client plumbing PR
    (mirrors the :class:`Task` design).
    """

    id: str
    title: str
    body: str
    version: int
    updated_at: datetime | None
    tags: tuple[str, ...]
    status: str | None  # active | archived | quarantined | None
    note_type: str | None
    path: str  # e.g. "projects/lithos-loom/context.md"
    slug: str  # derived: first path segment after "projects/"


@dataclass(frozen=True)
class NoteSummary:
    """Lightweight ``Note`` projection returned by ``lithos_list``.

    Same identity + version + metadata fields as :class:`Note` but
    without the body — `lithos_list` doesn't return content by
    default and pulling it for an enumeration view would be wasteful.
    Use :meth:`LithosClient.note_read` to fetch the full body for a
    specific id once the caller has decided which docs to project.
    """

    id: str
    title: str
    version: int
    updated_at: datetime | None
    tags: tuple[str, ...]
    status: str | None
    note_type: str | None
    path: str
    slug: str


@dataclass(frozen=True)
class WriteResult:
    """Result envelope from :meth:`LithosClient.note_write`.

    Mirrors Lithos's ``WriteResult`` / ``WriteOutcome`` (see
    ``lithos/src/lithos/knowledge.py`` and ``intake.py``). The handler
    inspects ``.status`` to branch: ``"created"`` / ``"updated"`` are
    success paths; ``"version_conflict"`` carries ``current_version``
    so the caller can re-fetch and resolve; ``"slug_collision"``
    carries ``slug_collision_existing_id`` so the caller can surface
    the conflicting doc to the operator.

    Critically: a version_conflict response does NOT raise — the
    caller MUST check ``.status``. Raising would force every push-back
    site to try/except, masking the intent that a conflict is an
    expected branch of bidirectional sync.
    """

    status: WriteStatus
    note: Note | None = None
    """Set on ``"created"`` / ``"updated"`` (the persisted doc)."""
    current_version: int | None = None
    """Set on ``"version_conflict"`` — the version Lithos actually has,
    which the caller pulls + diffs against to resolve."""
    slug_collision_existing_id: str | None = None
    """Set on ``"slug_collision"`` — the id of the doc that already
    owns this slug, for operator surfacing."""
    message: str | None = None
    """Operator-readable message on non-success outcomes."""
    warnings: tuple[str, ...] = field(default_factory=tuple)


class LithosClient:
    """MCP-over-SSE client for the Lithos server.

    Usage::

        async with LithosClient("http://localhost:8765") as client:
            tasks = await client.task_list(with_claims=True)
    """

    def __init__(self, base_url: str, *, agent_id: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self._sse_ctx: Any = None
        self._session_ctx: Any = None
        self._session: ClientSession | None = None
        # Dead-session recovery (#43): the keeper task (spawned in
        # ``__aenter__``) owns session lifecycle (open / close) — the MCP
        # SDK's anyio cancel scopes are pinned to whichever task entered
        # them, so all teardown + re-establish MUST run in that task. Other
        # tasks dispatch reconnect requests via ``_reconnect_queue`` and
        # await an ``_ReconnectRequest.done`` event. The generation counter
        # makes concurrent requests single-flight (peer already reconnected
        # → skip).
        #
        # Normal ``call_tool`` calls still go direct from any caller task —
        # the MCP SDK's ClientSession multiplexes via request IDs, so
        # concurrent in-flight calls are safe; only the open/close edges
        # need the keeper.
        self._reconnect_lock = asyncio.Lock()
        self._session_generation = 0
        self._keeper_task: asyncio.Task[None] | None = None
        self._reconnect_queue: (
            asyncio.Queue[_ReconnectRequest | _ShutdownSentinel] | None
        ) = None
        self._ready_event: asyncio.Event | None = None
        self._startup_error: BaseException | None = None
        # True once ``__aenter__`` has been called — distinguishes the
        # test-bypass path (set ``_session`` directly, no keeper) from a
        # production client whose keeper has died (then we fail loudly
        # rather than silently fall back to the unsafe inline reconnect).
        self._aenter_called: bool = False

    async def __aenter__(self) -> LithosClient:
        self._aenter_called = True
        self._reconnect_queue = asyncio.Queue()
        self._ready_event = asyncio.Event()
        self._keeper_task = asyncio.create_task(
            self._keeper_loop(), name="lithos-client-keeper"
        )
        await self._ready_event.wait()
        if self._startup_error is not None:
            err = self._startup_error
            # Keeper has already exited; await it to surface any unraisable
            # warnings before propagating the original error.
            with contextlib.suppress(BaseException):
                await self._keeper_task
            self._keeper_task = None
            raise err
        return self

    async def _keeper_loop(self) -> None:
        """Own the MCP session for the client's lifetime.

        Runs the initial ``_establish`` (so the anyio cancel scopes opened
        by ``sse_client`` / ``ClientSession`` belong to *this* task), then
        services reconnect requests from :attr:`_reconnect_queue` one at a
        time. Each reconnect tears down the old session and opens a fresh
        one — both safe because we're back in the task that originally
        opened the scopes (see :meth:`_teardown_in_keeper` for why this
        matters).

        On shutdown sentinel: drain pending requests so blocked callers
        don't hang, then tear down the live session.
        """
        assert self._reconnect_queue is not None
        assert self._ready_event is not None
        try:
            await self._establish()
        except BaseException as exc:  # noqa: BLE001 — surface via startup_error
            self._startup_error = exc
            self._ready_event.set()
            return
        self._ready_event.set()
        try:
            while True:
                request = await self._reconnect_queue.get()
                if isinstance(request, _ShutdownSentinel):
                    break
                if self._session_generation != request.expected_gen:
                    # Peer already reconnected since this caller's call
                    # failed — no-op, caller will retry against the
                    # already-fresh session.
                    request.done.set()
                    continue
                try:
                    await self._teardown_in_keeper()
                    await self._establish()
                    self._session_generation += 1
                except BaseException as exc:  # noqa: BLE001 — surface via request
                    request.error = exc
                finally:
                    request.done.set()
        finally:
            self._drain_pending_reconnects()
            await self._teardown_in_keeper()

    def _drain_pending_reconnects(self) -> None:
        """Fail any reconnect requests still in the queue after shutdown so
        callers blocked on ``request.done.wait()`` don't hang forever."""
        if self._reconnect_queue is None:
            return
        shutdown_err = LithosClientError(
            "session_unavailable",
            "LithosClient keeper exited before processing reconnect",
        )
        while True:
            try:
                request = self._reconnect_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if isinstance(request, _ShutdownSentinel):
                continue
            request.error = shutdown_err
            request.done.set()

    async def _teardown_in_keeper(self) -> None:
        """Tear down the current session + SSE contexts. Safe because the
        keeper is the same task that opened them — calling ``__aexit__``
        from any other task would raise ``RuntimeError: Attempted to exit
        cancel scope in a different task than it was entered in`` (soak
        2026-05-29; see also :meth:`_keeper_loop` docstring).

        Errors during teardown are swallowed: the connection is already
        dead (caller wouldn't have requested a reconnect otherwise), and
        the fresh ``_establish`` that follows is what matters.
        """
        if self._session_ctx is not None:
            try:  # noqa: SIM105 — contextlib.suppress(Exception) misses BaseExceptionGroup
                await self._session_ctx.__aexit__(None, None, None)
            except* Exception:
                pass
            self._session_ctx = None
        self._session = None
        if self._sse_ctx is not None:
            try:  # noqa: SIM105 — contextlib.suppress(Exception) misses BaseExceptionGroup
                await self._sse_ctx.__aexit__(None, None, None)
            except* Exception:
                pass
            self._sse_ctx = None

    async def _establish(self) -> None:
        """Open a fresh MCP-over-SSE session, replacing any prior one.

        The connect → ``ClientSession`` → ``initialize()`` sequence, factored
        out of ``__aenter__`` so :meth:`_reconnect` can reuse it after a
        dead-session failure. Reassigns ``self._session`` in place, so a
        client shared across the daemon's subscription handlers recovers for
        all of them without re-wiring.

        On a partial failure (e.g. ``initialize()`` raises after the SSE
        stream opened) we tear down what we opened before re-raising, so a
        failed (re)connect never leaks the SSE context — important on the
        initial ``__aenter__`` path, where there's no retry loop to clean up
        after us.
        """
        try:
            sse_url = f"{self.base_url}/sse"
            self._sse_ctx = sse_client(sse_url)
            read, write = await self._sse_ctx.__aenter__()
            self._session_ctx = ClientSession(read, write)
            session = await self._session_ctx.__aenter__()
            await session.initialize()
            self._session = session
        except BaseExceptionGroup:
            # anyio may surface partial-connect failures inside an
            # ExceptionGroup. Handled separately from bare Exception so an
            # ordinary connect/init error keeps its original type —
            # wrapping it in a group via ``except*`` would degrade
            # caller-side diagnostics.
            await self._cleanup_partial_connect()
            raise
        except Exception:
            await self._cleanup_partial_connect()
            raise

    async def _cleanup_partial_connect(self) -> None:
        """Tear down a partially-opened session inside ``_establish``'s
        except branch.

        Unlike :meth:`_teardown_quietly` (which runs from a reconnect
        triggered by some *other* task's failed call), we're in the same
        task that opened these contexts, so calling ``__aexit__`` here is
        safe — the anyio cancel scopes the SDK opened live in our stack
        and exiting them won't cancel anyone else. Best-effort: errors
        during cleanup are subordinate to the original exception we're
        about to re-raise. ``except* Exception`` swallows Exception-derived
        errors (bare or in an ExceptionGroup) while letting a
        CancelledError-bearing residual subgroup propagate, so true
        cancellation still wins over the original re-raise.
        """
        if self._session_ctx is not None:
            try:  # noqa: SIM105 — contextlib.suppress(Exception) misses BaseExceptionGroup
                await self._session_ctx.__aexit__(None, None, None)
            except* Exception:
                pass
            self._session_ctx = None
        self._session = None
        if self._sse_ctx is not None:
            try:  # noqa: SIM105 — contextlib.suppress(Exception) misses BaseExceptionGroup
                await self._sse_ctx.__aexit__(None, None, None)
            except* Exception:
                pass
            self._sse_ctx = None

    async def _request_reconnect(self, *, expected_gen: int) -> None:
        """Request a reconnect.

        Production path (``__aenter__`` has run, keeper alive): queue a
        request to the keeper and wait — the keeper does the teardown +
        re-establish in its own task, where the anyio scopes belong.

        Test path (``__aenter__`` never called, ``_session`` injected
        directly): fall back to inline reconnect. Safe in tests because
        mock sessions don't have real anyio cancel scopes.

        Production with dead keeper: fail loudly rather than silently
        falling back to the inline path — the inline path is unsafe in
        production and would re-introduce the 2026-05-28 RuntimeError.
        """
        if self._keeper_task is not None and not self._keeper_task.done():
            assert self._reconnect_queue is not None
            request = _ReconnectRequest(expected_gen=expected_gen)
            await self._reconnect_queue.put(request)
            await request.done.wait()
            if request.error is not None:
                raise request.error
            return
        if self._aenter_called:
            raise LithosClientError(
                "session_unavailable",
                "LithosClient keeper has exited; client is no longer usable",
            )
        await self._reconnect_inline(expected_gen=expected_gen)

    async def _reconnect_inline(self, *, expected_gen: int) -> None:
        """Reconnect from the caller's task (TESTS ONLY).

        Used by the test-bypass path where ``__aenter__`` is skipped and
        ``_session`` is set directly. Drops refs (rather than calling
        ``__aexit__`` on real SDK contexts, which is the failure mode the
        keeper exists to avoid) and calls ``_establish``. Single-flight via
        :attr:`_reconnect_lock` so concurrent callers reconnect once.
        """
        async with self._reconnect_lock:
            if self._session_generation != expected_gen:
                return
            self._session_ctx = None
            self._session = None
            self._sse_ctx = None
            await self._establish()
            self._session_generation += 1

    async def _invoke(self, tool: str, arguments: dict[str, Any]) -> CallToolResult:
        """Single chokepoint for every MCP tool call.

        Wraps ``session.call_tool`` with dead-session recovery (#43): on a
        transport-level failure it re-establishes the session and retries,
        bounded by :data:`_MAX_TRANSPORT_ATTEMPTS` with a small backoff, then
        re-raises the last error. Callers layer their own response decoding
        on the returned :class:`CallToolResult` — domain ``{status:"error"}``
        envelopes live *in* the result and are raised by those decoders
        *after* this returns, so the only exceptions seen here are transport
        / protocol failures from the SDK.

        The catch is intentionally broad (any ``Exception`` except
        ``CancelledError``): the exact exception the MCP/anyio stack raises on
        a dropped SSE stream is version-dependent, and a too-narrow filter
        would silently fail to recover. Bounded retries + per-attempt WARNING
        logs + re-raise-on-exhaustion keep it from masking a persistent fault.

        At-least-once caveat: the dominant failure — the session died while
        idle and the next ``call_tool`` fails before the request is
        transmitted — is safe to retry for reads and writes alike. A write
        that committed server-side but lost its response to a mid-flight crash
        could double-apply on retry; Lithos writes are largely tolerant
        (``note_write`` is optimistic-version-locked, ``task_complete`` /
        ``task_cancel`` are idempotent, a duplicate ``task_create`` is a
        recoverable dup), so this isn't gated.
        """
        if self._session is None and not self._aenter_called:
            raise LithosClientError(
                "client_not_initialised",
                "LithosClient not initialised; use 'async with LithosClient(...) as c'",
            )
        # Reset the per-task retry flag so this call's reading of it reflects
        # only this call's retry state. See :data:`_invoke_retried` for the
        # rationale (task_claim ownership re-check disambiguation).
        _invoke_retried.set(False)
        last_exc: BaseException | None = None  # may be BaseExceptionGroup
        for attempt in range(_MAX_TRANSPORT_ATTEMPTS):
            session = self._session
            if session is None:
                # Transient: keeper is mid-reconnect (between teardown and
                # establish, both await points). Request a reconnect — the
                # keeper's generation guard makes this a near-no-op if the
                # reconnect is already in flight.
                await self._request_reconnect(expected_gen=self._session_generation)
                continue
            gen = self._session_generation
            try:
                return await session.call_tool(tool, arguments=arguments)
            except asyncio.CancelledError:
                raise
            except BaseExceptionGroup as group:
                # anyio (inside the MCP SDK) commonly wraps SSE-stream-closed
                # failures in a group. If a CancelledError is anywhere in the
                # tree, propagate the cancellation subgroup instead of
                # retrying. Otherwise treat the remainder as a transport
                # failure (soak 2026-05-28).
                cancel_subgroup, rest = group.split(asyncio.CancelledError)
                if cancel_subgroup is not None:
                    raise cancel_subgroup from group
                last_exc = rest if rest is not None else group
            except Exception as exc:  # noqa: BLE001 — see _invoke docstring
                last_exc = exc
            # Transport-failure handling (shared between the bare and grouped
            # paths): retry up to _MAX_TRANSPORT_ATTEMPTS, with reconnect.
            if attempt == _MAX_TRANSPORT_ATTEMPTS - 1:
                break
            logger.warning(
                "LithosClient: call_tool(%s) failed (%r); re-establishing "
                "session (attempt %d/%d)",
                tool,
                last_exc,
                attempt + 1,
                _MAX_TRANSPORT_ATTEMPTS,
            )
            await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
            await self._request_reconnect(expected_gen=gen)
            # Mark retry as having happened before the next call_tool attempt,
            # so a write that committed-but-response-lost (claim_failed et al.)
            # can be reconciled by the caller's idempotency check.
            _invoke_retried.set(True)
        if last_exc is not None:
            raise last_exc
        # Every attempt hit the transient-None path without ever landing a
        # live session — surface a clean error rather than looping forever.
        raise LithosClientError(
            "session_unavailable",
            "LithosClient could not re-establish a session after reconnect",
        )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Shut down the client.

        Production path (keeper alive): queue a shutdown sentinel and
        await the keeper — it tears down the session in its own task,
        where the anyio scopes belong. Calling ``__aexit__`` on the
        contexts from this task would raise ``RuntimeError: Attempted to
        exit cancel scope in a different task than it was entered in``
        (the keeper task opened them, not us).

        Test path (no keeper): fall back to direct ``__aexit__`` on
        whatever contexts are pinned — safe because mock contexts don't
        have real anyio scopes. Preserves backward-compat with tests that
        bypass ``__aenter__`` and set ``_session_ctx`` / ``_sse_ctx``
        directly.
        """
        if self._keeper_task is not None and not self._keeper_task.done():
            assert self._reconnect_queue is not None
            self._reconnect_queue.put_nowait(_SHUTDOWN)
            with contextlib.suppress(Exception, BaseExceptionGroup):
                await self._keeper_task  # keeper errors surface via request.error
            self._keeper_task = None
            return
        # Test / pre-init path — close any directly-installed contexts.
        try:
            if self._session_ctx is not None:
                await self._session_ctx.__aexit__(exc_type, exc, tb)
        finally:
            self._session_ctx = None
            self._session = None
            if self._sse_ctx is not None:
                try:
                    await self._sse_ctx.__aexit__(exc_type, exc, tb)
                finally:
                    self._sse_ctx = None

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Invoke an MCP tool, decode the FastMCP content envelope, raise on errors.

        Returns the decoded JSON payload (typically a ``dict``). Domain
        errors with a ``{status: "error", code, message}`` envelope raise
        :class:`LithosClientError`; transport-level failures from the
        ``mcp`` SDK are recovered (dead-session reconnect) by
        :meth:`_invoke`, or propagate if recovery is exhausted.
        """
        result = await self._invoke(tool, arguments)
        payload = _payload_from_result(result)
        if isinstance(payload, dict):
            _raise_if_error_envelope(payload)
        return payload

    async def task_list(
        self,
        *,
        status: str | None = None,
        with_claims: bool = False,
        resolved_since: datetime | None = None,
    ) -> list[Task]:
        """Return tasks matching the filters. Omitted ``status`` = all.

        ``resolved_since`` (lithos#286 / PR #288) is converted to an
        ISO-8601 string before passing through; Lithos compares it
        against ``tasks.resolved_at >= ?`` so open tasks (whose
        ``resolved_at`` is NULL) are excluded automatically. Omitting
        the parameter keeps the call wire-identical to the pre-#286
        contract so loom can roll out ahead of a staging Lithos that
        doesn't yet recognise the kwarg.
        """
        arguments: dict[str, Any] = {"with_claims": with_claims}
        if status is not None:
            arguments["status"] = status
        if resolved_since is not None:
            arguments["resolved_since"] = resolved_since.isoformat()
        result = await self._invoke("lithos_task_list", arguments)
        return _parse_task_list_response(result)

    async def finding_post(
        self,
        *,
        task_id: str,
        summary: str,
        agent: str | None = None,
        knowledge_id: str | None = None,
    ) -> str | None:
        """Post a finding to a task. Returns the new ``finding_id``.

        ``agent`` defaults to ``self.agent_id`` (set at client construction).
        Subscription handlers typically don't have a meaningful task_id for
        their own friction findings — pass an empty string for the
        ``[Friction]`` posting and Lithos will surface it cluster-wide via
        finding listings rather than per-task.
        """
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError(
                "missing_agent",
                "finding_post needs an agent id; pass agent= or set agent_id",
            )
        if not task_id:
            # Lithos requires a task_id; punt to a logged warning instead
            # of raising, so [Friction] postings without a task scope don't
            # crash the runner. Caller should avoid this when possible.
            return None
        arguments: dict[str, Any] = {
            "task_id": task_id,
            "agent": agent_id,
            "summary": summary,
        }
        if knowledge_id is not None:
            arguments["knowledge_id"] = knowledge_id
        result = await self._invoke("lithos_finding_post", arguments)
        payload = _payload_from_result(result)
        if isinstance(payload, dict):
            _raise_if_error_envelope(payload)
            finding_id = payload.get("finding_id")
            if isinstance(finding_id, str):
                return finding_id
        return None

    async def task_claim(
        self,
        *,
        task_id: str,
        aspect: str,
        ttl_minutes: int = 60,
        agent: str | None = None,
    ) -> str:
        """Claim ``aspect`` of ``task_id``. Returns the claim's ``expires_at``.

        Raises :class:`LithosClientError` with ``code="claim_failed"`` when
        the aspect is already claimed **by another agent** (or the task isn't
        open). Callers treat that as "skip — another runner won the race".

        Idempotent under the transport-retry layer (#43), but ONLY when
        ``_invoke`` actually retried this call. ``_invoke`` may re-issue
        this claim after a transport failure, and a claim that committed
        server-side before its response was lost would then come back
        ``claim_failed`` — *because we already hold it*. To avoid turning
        our own committed claim into a visible ``claim_failed`` (which would
        make RouteRunner silently skip work it owns until the TTL lapses),
        a retry-induced ``claim_failed`` is disambiguated against the
        task's actual claims: if **we** hold this aspect, the claim
        effectively succeeded and we return its expiry.

        **Critical**: the ownership re-check is gated on
        :data:`_invoke_retried`. On a *first-attempt* ``claim_failed`` we
        re-raise immediately, because the only way our ``agent_id`` could
        be the holder without us having retried is if a *different* Loom
        process is sharing this ``agent_id`` and already holds the claim
        — and silently "succeeding" in that case would let both processes
        run the plugin (PR #60 review, 2026-05-29). Operators are still
        free to use process-unique ``agent_id`` values to avoid the
        ambiguity entirely; this gate just removes the regression where
        the retry-recovery path made the shared-``agent_id`` mistake more
        dangerous than it was without #43.
        """
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_claim needs an agent id")
        try:
            payload = await self._call(
                "lithos_task_claim",
                {
                    "task_id": task_id,
                    "aspect": aspect,
                    "agent": agent_id,
                    "ttl_minutes": ttl_minutes,
                },
            )
        except LithosClientError as exc:
            if exc.code != "claim_failed":
                raise
            # Capture the retry flag BEFORE the next ``_invoke`` (via
            # ``_claim_expiry_if_held`` → ``task_status``) resets it.
            retried = _invoke_retried.get()
            if not retried:
                # First-attempt claim_failed: a same-``agent_id`` holder must
                # be a *different* process, since we never sent a prior
                # request that could have committed and lost its response.
                # Propagate as a real collision.
                raise
            held = await self._claim_expiry_if_held(task_id, aspect, agent_id)
            if held is not None:
                logger.info(
                    "task_claim: %s aspect %r reported claim_failed on retry "
                    "but is already held by %s (treating as success — likely a "
                    "retried claim whose first response was lost)",
                    task_id,
                    aspect,
                    agent_id,
                )
                return held
            raise
        expires = payload.get("expires_at") if isinstance(payload, dict) else None
        if not isinstance(expires, str):
            raise LithosClientError(
                "invalid_response", "task_claim response missing expires_at"
            )
        return expires

    async def _claim_expiry_if_held(
        self, task_id: str, aspect: str, agent_id: str
    ) -> str | None:
        """Return our own claim's ``expires_at`` for ``aspect`` if ``agent_id``
        already holds it on ``task_id``, else ``None``.

        Disambiguates a ``claim_failed`` that is actually our own
        committed-but-response-lost claim (#43). Returns ``""`` when we hold
        the claim but Lithos omitted ``expires_at`` (we still own it — the
        caller only needs to know the claim is ours). A missing task or any
        non-matching/other-agent claim returns ``None`` → genuine failure.
        """
        task = await self.task_status(task_id=task_id)
        if task is None:
            return None
        for claim in task.claims:
            if claim.get("agent") == agent_id and claim.get("aspect") == aspect:
                expires = claim.get("expires_at")
                return expires if isinstance(expires, str) else ""
        return None

    async def task_renew(
        self,
        *,
        task_id: str,
        aspect: str,
        ttl_minutes: int = 60,
        agent: str | None = None,
    ) -> str:
        """Extend an existing claim. Returns the new ``expires_at``."""
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_renew needs an agent id")
        payload = await self._call(
            "lithos_task_renew",
            {
                "task_id": task_id,
                "aspect": aspect,
                "agent": agent_id,
                "ttl_minutes": ttl_minutes,
            },
        )
        expires = payload.get("new_expires_at") if isinstance(payload, dict) else None
        if not isinstance(expires, str):
            raise LithosClientError(
                "invalid_response", "task_renew response missing new_expires_at"
            )
        return expires

    async def task_release(
        self,
        *,
        task_id: str,
        aspect: str,
        agent: str | None = None,
    ) -> None:
        """Release a claim. ``code="claim_not_found"`` is folded into a no-op."""
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_release needs an agent id")
        try:
            await self._call(
                "lithos_task_release",
                {"task_id": task_id, "aspect": aspect, "agent": agent_id},
            )
        except LithosClientError as exc:
            if exc.code == "claim_not_found":
                return
            raise

    async def task_create(
        self,
        *,
        title: str,
        agent: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a coordination task. Returns the new task's ``id``.

        ``metadata`` (lithos#295) is a one-shot initial set — there's
        no merge to think about because the task doesn't exist yet.
        Omitted when ``None``, matching :meth:`task_update`'s
        omit-when-default pattern so old/strict Lithos servers don't
        reject an unexpected key.

        Wraps the ``lithos_task_create`` MCP tool's
        ``{task_id: string}`` response shape. Domain errors
        (``invalid_input`` etc.) raise :class:`LithosClientError`.
        """
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_create needs an agent id")
        arguments: dict[str, Any] = {"title": title, "agent": agent_id}
        if description is not None:
            arguments["description"] = description
        if tags is not None:
            arguments["tags"] = tags
        if metadata is not None:
            arguments["metadata"] = metadata
        payload = await self._call("lithos_task_create", arguments)
        task_id = payload.get("task_id") if isinstance(payload, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise LithosClientError(
                "invalid_response", "task_create response missing task_id"
            )
        return task_id

    async def task_complete(
        self,
        *,
        task_id: str,
        agent: str | None = None,
    ) -> None:
        """Mark a task as completed. Releases all claims as a side effect."""
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_complete needs an agent id")
        await self._call(
            "lithos_task_complete", {"task_id": task_id, "agent": agent_id}
        )

    async def task_cancel(
        self,
        *,
        task_id: str,
        agent: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Cancel a task and release all claims.

        Mirrors :meth:`task_complete` — both terminal transitions
        populate ``tasks.resolved_at`` upstream (lithos#286). ``reason``
        is accepted by the MCP surface but per the Lithos spec is not
        persisted in SQLite; pass a short breadcrumb so the origin
        surfaces in MCP-level logs/traces. Omit it from the MCP
        arguments when ``None`` so old/strict Lithos servers don't
        choke on an explicit-null (mirrors the ``resolved_since``
        pattern from :meth:`task_list`).
        """
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_cancel needs an agent id")
        arguments: dict[str, Any] = {"task_id": task_id, "agent": agent_id}
        if reason is not None:
            arguments["reason"] = reason
        await self._call("lithos_task_cancel", arguments)

    async def task_update(
        self,
        *,
        task_id: str,
        agent: str | None = None,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Update mutable task fields.

        At least one of ``title`` / ``description`` / ``tags`` /
        ``metadata`` must be provided.

        ``metadata`` (Lithos #290) is applied as an **additive per-key
        merge**: keys with non-null values overwrite, keys with the
        literal Python ``None`` (JSON ``null``) delete the existing
        key, and keys not mentioned are preserved. ``metadata={}``
        passes through to Lithos as a no-op; if you want to skip
        sending metadata at all, leave the kwarg at its default
        ``None``.
        """
        if title is None and description is None and tags is None and metadata is None:
            raise LithosClientError(
                "invalid_input",
                "task_update requires at least one of title/description/tags/metadata",
            )
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "task_update needs an agent id")
        arguments: dict[str, Any] = {"task_id": task_id, "agent": agent_id}
        if title is not None:
            arguments["title"] = title
        if description is not None:
            arguments["description"] = description
        if tags is not None:
            arguments["tags"] = tags
        if metadata is not None:
            arguments["metadata"] = metadata
        await self._call("lithos_task_update", arguments)

    async def task_status(self, *, task_id: str) -> Task | None:
        """Return the full record of a single task, including its
        active claims.

        Post-lithos#294 the response envelope is the full task record
        (``id, title, description, status, created_by, created_at,
        resolved_at, tags, metadata, outcome``) wrapped in a single-
        element ``tasks`` list, plus the embedded ``claims`` array.
        Returns ``None`` when Lithos reports the task as not found
        (the historical ``{tasks: []}`` shape).

        Prefer :meth:`task_get` when you don't need claims — it
        returns the same record without the list wrapper or the
        claim serialization cost, and uses an explicit
        ``task_not_found`` error envelope instead of an empty list.
        """
        result = await self._invoke("lithos_task_status", {"task_id": task_id})
        try:
            tasks = _parse_task_list_response(result)
        except LithosClientError as exc:
            if exc.code == "task_not_found":
                return None
            raise
        return tasks[0] if tasks else None

    async def task_get(self, *, task_id: str) -> Task | None:
        """Return the full record of a single task without its claims.

        Added in lithos#294 as the lightweight counterpart to
        :meth:`task_status`: same task envelope, no claims, single-
        object response shape (``{task: {...}}``), and an explicit
        ``task_not_found`` error envelope on miss (mapped here to
        ``None`` to match the :meth:`task_status` convention).

        Use this for pre-checks where only the persisted task fields
        matter — dependency resolution, idempotency gates,
        ``metadata.priority`` comparisons — and reserve
        :meth:`task_status` for callers that need claims.
        """
        result = await self._invoke("lithos_task_get", {"task_id": task_id})
        try:
            return _parse_task_get_response(result)
        except LithosClientError as exc:
            if exc.code == "task_not_found":
                return None
            raise

    # ── KB-doc surface (Slice 4 + 5) ─────────────────────────────────

    async def note_read(
        self,
        *,
        id: str | None = None,
        path: str | None = None,
    ) -> Note | None:
        """Fetch a full Lithos KB document by ``id`` or ``path``.

        Returns ``None`` if Lithos reports the doc as missing
        (``code="doc_not_found"`` envelope), so callers can treat
        deleted docs as a no-op rather than try/except.

        Exactly one of ``id`` / ``path`` should be passed — Lithos
        accepts either but the convention here is "use id when you
        have one, path otherwise" (the projection layer always has
        the id; the doctor may have only the path).
        """
        if id is None and path is None:
            raise LithosClientError(
                "invalid_input", "note_read requires one of id= or path="
            )
        arguments: dict[str, Any] = {}
        if id is not None:
            arguments["id"] = id
        if path is not None:
            arguments["path"] = path
        result = await self._invoke("lithos_read", arguments)
        try:
            return _parse_note_read_response(result)
        except LithosClientError as exc:
            if exc.code == "doc_not_found":
                return None
            raise

    async def note_write(
        self,
        *,
        agent: str | None = None,
        title: str,
        content: str,
        tags: list[str] | None = None,
        note_type: str = "concept",
        path: str | None = None,
        id: str | None = None,
        expected_version: int | None = None,
        status: str | None = None,
    ) -> WriteResult:
        """Create or update a Lithos KB doc; returns a :class:`WriteResult`.

        **Does not raise on ``version_conflict`` or ``slug_collision``** —
        the caller MUST inspect ``WriteResult.status`` and branch.
        This is deliberate: bidirectional sync needs both outcomes to
        be expected branches, not exceptions. Other domain errors
        (``invalid_input``, ``content_too_large``) also come back as
        ``WriteResult`` envelopes with the corresponding status;
        unexpected transport errors propagate as raised
        :class:`LithosClientError`.

        ``id`` triggers update semantics; ``path`` + no ``id`` creates.
        ``expected_version`` is only meaningful on update — Lithos
        compares against the canonical version and returns
        ``status="version_conflict"`` with ``current_version`` populated
        if the operator's view is stale.

        Defaults ``note_type`` to ``"concept"`` to match the D14
        convention for project context docs (which use the ``concept``
        enum value + a ``project-context`` tag, since the enum doesn't
        have a dedicated value).
        """
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "note_write needs an agent id")
        arguments: dict[str, Any] = {
            "title": title,
            "content": content,
            "agent": agent_id,
            "note_type": note_type,
        }
        if tags is not None:
            arguments["tags"] = tags
        if path is not None:
            arguments["path"] = path
        if id is not None:
            arguments["id"] = id
        if expected_version is not None:
            arguments["expected_version"] = expected_version
        if status is not None:
            arguments["status"] = status
        payload = await self._call_for_write_result("lithos_write", arguments)
        result = _parse_write_result(payload)
        # Real Lithos's success envelope is top-level
        # ``{status, id, path, version, warnings}`` (see
        # lithos/src/lithos/server.py:1327) — NOT the ``{document: {...}}``
        # shape ``_parse_write_result`` looks for. Without this fix-up,
        # ``WriteResult.note`` is always ``None`` in production despite
        # a successful write, and callers that rely on ``result.note.id``
        # silently get empty strings (PR #45 and PR #46 reviewer
        # findings).
        #
        # The parser stays pure — it parses only what's in the payload.
        # Here we stitch in the request inputs (title, content, tags,
        # status, note_type) plus the response's id/path/version to
        # construct a complete Note. ``updated_at`` stays ``None`` —
        # the response doesn't carry it; callers that need byte-stable
        # ``lithos_updated_at`` frontmatter (note-push handler)
        # re-fetch via ``note_read``.
        if (
            result.status in ("created", "updated")
            and result.note is None
            and isinstance(payload.get("id"), str)
            and isinstance(payload.get("version"), int)
        ):
            doc_id = str(payload["id"])
            doc_version = int(payload["version"])
            doc_path = str(payload.get("path") or "")
            result = dataclasses.replace(
                result,
                note=Note(
                    id=doc_id,
                    title=title,
                    body=content,
                    version=doc_version,
                    updated_at=None,
                    tags=tuple(tags or ()),
                    status=status,
                    note_type=note_type,
                    path=doc_path,
                    slug=_slug_from_path(doc_path),
                ),
            )
        return result

    async def note_list(
        self,
        *,
        path_prefix: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
    ) -> list[NoteSummary]:
        """Enumerate KB docs matching the filters. Returns a list of
        lightweight :class:`NoteSummary` (no body).

        ``path_prefix`` is the cheapest server-side filter for
        directory-scoped enumeration (``"projects/"`` for Slice 4).
        ``tags`` narrows further (e.g. ``["project-context"]``).

        ``limit`` is forwarded as-is; the projection bootstrap caps it
        at 100 by default, which comfortably exceeds the user's 20-ish
        project count. If your call site might exceed the limit,
        implement pagination at the call site — this method
        intentionally does NOT auto-page because the projection layer
        works in single-batch semantics today and adding hidden
        pagination would change observability without changing
        contract.
        """
        arguments: dict[str, Any] = {"limit": limit}
        if path_prefix is not None:
            arguments["path_prefix"] = path_prefix
        if tags is not None:
            arguments["tags"] = tags
        result = await self._invoke("lithos_list", arguments)
        return _parse_note_list_response(result)

    async def note_delete(
        self,
        *,
        id: str,
        agent: str | None = None,
    ) -> bool:
        """Delete a Lithos KB doc by id; returns ``True`` if deleted,
        ``False`` if Lithos reports it as already gone.

        ``doc_not_found`` is folded to ``False`` (not raised) so
        cleanup loops can call this idempotently — the common
        soak/test pattern is "delete these N docs whether or not
        they exist". Callers that need the distinction can call
        :meth:`note_read` first.

        ``agent`` defaults to ``self.agent_id`` and is **required**
        by the Lithos server (audit trail). Without an agent the
        bare ``lithos_delete`` MCP call fails with a pydantic
        "missing_argument" error that's hard to spot when only the
        message is rendered — this wrapper raises a typed
        :class:`LithosClientError` instead so the failure mode is
        obvious at the call site.

        Other domain errors (transport failures, permission
        errors) propagate as raised
        :class:`LithosClientError` — only ``doc_not_found`` is
        folded.
        """
        agent_id = agent or self.agent_id
        if not agent_id:
            raise LithosClientError("missing_agent", "note_delete needs an agent id")
        try:
            payload = await self._call("lithos_delete", {"id": id, "agent": agent_id})
        except LithosClientError as exc:
            if exc.code == "doc_not_found":
                return False
            raise
        # Lithos's contract is ``{"success": True}`` on delete
        # (lithos/src/lithos/server.py:1434). Be strict about the
        # success envelope rather than treating any non-error
        # response as success — without this, a server-side drift
        # (``{}``, ``{"success": false}``, a non-dict payload)
        # would silently report a successful delete and leave the
        # stale doc behind. Raising on shape divergence makes the
        # contract break visible at the call site.
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise LithosClientError(
                "invalid_response",
                f"lithos_delete returned non-success payload: {payload!r}",
            )
        return True

    async def _call_for_write_result(
        self,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Like :meth:`_call` but does NOT raise on
        ``version_conflict`` / ``slug_collision`` envelopes — the
        caller (:meth:`note_write`) needs to see them as data. Other
        error envelopes still raise."""
        result = await self._invoke(tool, arguments)
        payload = _payload_from_result(result)
        if not isinstance(payload, dict):
            raise LithosClientError(
                "invalid_response",
                f"expected dict response from {tool}, got {type(payload).__name__}",
            )
        # Lithos's write surface returns "status" on every response —
        # success ("created", "updated") AND domain failures
        # ("version_conflict", "slug_collision", "invalid_input",
        # etc.). Re-raise only the truly exceptional envelope
        # (``status == "error"``); the rest are returned for the
        # caller to inspect.
        if payload.get("status") == "error":
            _raise_if_error_envelope(payload)
        return payload


# ── Pure parse helpers (heavily unit-tested) ───────────────────────────


def _parse_task_list_response(result: CallToolResult) -> list[Task]:
    payload = _payload_from_result(result)
    if not isinstance(payload, dict):
        raise LithosClientError(
            "invalid_response",
            f"expected dict response, got {type(payload).__name__}",
        )
    _raise_if_error_envelope(payload)
    if "tasks" not in payload:
        raise LithosClientError(
            "invalid_response", "missing 'tasks' key in lithos_task_list response"
        )
    raw_tasks = payload["tasks"]
    if not isinstance(raw_tasks, list):
        raise LithosClientError(
            "invalid_response", "'tasks' must be a list in lithos_task_list response"
        )
    return [_parse_task(t) for t in raw_tasks]


def _parse_task(raw: Any) -> Task:
    if not isinstance(raw, dict):
        raise LithosClientError(
            "invalid_response", f"task entry must be a dict, got {type(raw).__name__}"
        )
    try:
        tags_raw = raw.get("tags") or []
        claims_raw = raw.get("claims") or []
        description_raw = raw.get("description")
        outcome_raw = raw.get("outcome")
        return Task(
            id=str(raw["id"]),
            title=str(raw["title"]),
            status=str(raw["status"]),
            tags=tuple(tags_raw),
            metadata=dict(raw.get("metadata") or {}),
            claims=tuple(dict(c) for c in claims_raw),
            resolved_at=_parse_iso_datetime(raw.get("resolved_at")),
            description=str(description_raw) if description_raw is not None else None,
            created_by=str(raw.get("created_by") or ""),
            created_at=_parse_iso_datetime(raw.get("created_at")),
            outcome=str(outcome_raw) if outcome_raw is not None else None,
        )
    except KeyError as exc:
        raise LithosClientError(
            "invalid_response", f"task entry missing required field: {exc.args[0]}"
        ) from exc


def _parse_task_get_response(result: CallToolResult) -> Task:
    """Parse the ``lithos_task_get`` single-object envelope (lithos#294).

    Shape: ``{"task": {...}}`` on success;
    ``{"status": "error", "code": "task_not_found", ...}`` when the
    task doesn't exist (handled here by re-raising — the caller
    method maps it to ``None``).
    """
    payload = _payload_from_result(result)
    if not isinstance(payload, dict):
        raise LithosClientError(
            "invalid_response",
            f"expected dict response, got {type(payload).__name__}",
        )
    _raise_if_error_envelope(payload)
    if "task" not in payload:
        raise LithosClientError(
            "invalid_response", "missing 'task' key in lithos_task_get response"
        )
    return _parse_task(payload["task"])


def _parse_iso_datetime(value: Any) -> datetime | None:
    """Best-effort ISO-8601 datetime parse. Returns ``None`` for absent
    or unparseable values so a Lithos schema drift on optional fields
    doesn't crash the client (US13 only needs ``resolved_at`` for
    terminal-state rendering; missing values fall back to the bus event
    timestamp at the projection layer)."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _payload_from_result(result: CallToolResult) -> Any:
    """Extract the JSON payload from a FastMCP-shaped ``CallToolResult``.

    FastMCP wraps tool returns in a list of content blocks; the first
    text block carries the JSON-serialised return value. ``isError=True``
    surfaces as an exception regardless of payload shape.
    """
    if not result.content:
        if result.isError:
            raise LithosClientError("tool_error", "tool returned isError with no body")
        raise LithosClientError("invalid_response", "tool returned empty content list")
    block = result.content[0]
    text = getattr(block, "text", None)
    if not isinstance(text, str):
        raise LithosClientError(
            "invalid_response",
            f"first content block has no text payload (type={type(block).__name__})",
        )
    if result.isError:
        raise LithosClientError("tool_error", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LithosClientError(
            "invalid_response", f"tool returned non-JSON text: {exc}"
        ) from exc


def _raise_if_error_envelope(payload: Mapping[str, Any]) -> None:
    """Raise ``LithosClientError`` if ``payload`` is an error envelope."""
    if payload.get("status") != "error":
        return
    raise LithosClientError(
        code=str(payload.get("code") or "error"),
        message=str(payload.get("message") or ""),
    )


# ── Note parse helpers (Slice 4 + 5) ───────────────────────────────────


def _slug_from_path(path: str) -> str:
    """Extract the project slug from a Lithos doc path.

    For ``projects/<slug>/<filename>.md`` returns ``<slug>``. For
    paths that don't sit under ``projects/`` (e.g. ``observations/...``)
    returns the first segment as a best-effort fallback so the
    field is always populated.

    Empty path → empty slug. The caller is responsible for filtering
    docs that shouldn't be projected (the projection subscription's
    ``path_prefix`` filter does this).
    """
    if not path:
        return ""
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "projects":
        return parts[1]
    return parts[0]


def _parse_note(raw: Any, *, body_required: bool) -> Note:
    """Parse a Lithos KB-doc envelope into a :class:`Note`.

    ``body_required=True`` (for ``note_read`` responses) raises if
    ``content`` is missing; ``False`` is used by callers that wrap
    list-shaped responses where bodies are absent by design.
    """
    if not isinstance(raw, dict):
        raise LithosClientError(
            "invalid_response", f"note entry must be a dict, got {type(raw).__name__}"
        )
    try:
        doc_id = str(raw["id"])
        title = str(raw["title"])
        path = str(raw.get("path") or "")
        metadata = raw.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        tags_raw = metadata.get("tags") or raw.get("tags") or []
        version_raw = metadata.get("version") or raw.get("version") or 0
        body_raw = raw.get("content")
        if body_required and body_raw is None:
            raise LithosClientError(
                "invalid_response", "note response missing 'content'"
            )
        return Note(
            id=doc_id,
            title=title,
            body=str(body_raw or ""),
            version=int(version_raw),
            updated_at=_parse_iso_datetime(
                metadata.get("updated_at") or raw.get("updated_at")
            ),
            tags=tuple(str(t) for t in tags_raw),
            status=_optional_str(metadata.get("status") or raw.get("status")),
            note_type=_optional_str(metadata.get("note_type") or raw.get("note_type")),
            path=path,
            slug=_slug_from_path(path),
        )
    except KeyError as exc:
        raise LithosClientError(
            "invalid_response", f"note entry missing required field: {exc.args[0]}"
        ) from exc
    except (TypeError, ValueError) as exc:
        # Catches coercion failures from ``int(version_raw)`` /
        # ``str(...)`` / ``tuple(str(t) for t in tags_raw)`` when the
        # server returns malformed field types (``version="abc"``,
        # ``tags={...}`` instead of a list, etc.). Without this the
        # raw conversion exception would escape, breaking the
        # otherwise-consistent "bad server shape → typed
        # LithosClientError" contract the rest of the client uses.
        raise LithosClientError(
            "invalid_response", f"malformed note entry: {exc}"
        ) from exc


def _parse_note_summary(raw: Any) -> NoteSummary:
    """Parse a list-view note entry. Same shape as :func:`_parse_note`
    but without the body and with no ``body_required`` check."""
    note = _parse_note(raw, body_required=False)
    return NoteSummary(
        id=note.id,
        title=note.title,
        version=note.version,
        updated_at=note.updated_at,
        tags=note.tags,
        status=note.status,
        note_type=note.note_type,
        path=note.path,
        slug=note.slug,
    )


def _parse_note_read_response(result: CallToolResult) -> Note:
    payload = _payload_from_result(result)
    if not isinstance(payload, dict):
        raise LithosClientError(
            "invalid_response",
            f"expected dict response, got {type(payload).__name__}",
        )
    _raise_if_error_envelope(payload)
    # ``lithos_read`` returns the doc fields at the top level (not
    # wrapped in a ``"document"`` key) — see lithos/server.py:1337.
    return _parse_note(payload, body_required=True)


def _parse_note_list_response(result: CallToolResult) -> list[NoteSummary]:
    payload = _payload_from_result(result)
    if not isinstance(payload, dict):
        raise LithosClientError(
            "invalid_response",
            f"expected dict response, got {type(payload).__name__}",
        )
    _raise_if_error_envelope(payload)
    items = payload.get("items")
    if items is None:
        # Some Lithos versions return ``results`` instead — accept both.
        items = payload.get("results")
    if items is None:
        raise LithosClientError(
            "invalid_response",
            "missing 'items' (or 'results') key in lithos_list response",
        )
    if not isinstance(items, list):
        raise LithosClientError(
            "invalid_response", "'items' must be a list in lithos_list response"
        )
    return [_parse_note_summary(item) for item in items]


def _parse_write_result(payload: dict[str, Any]) -> WriteResult:
    """Parse a ``lithos_write`` envelope into a :class:`WriteResult`.

    Trusts the caller (:meth:`LithosClient._call_for_write_result`)
    to have already raised on the truly exceptional ``status=="error"``
    envelope; all other statuses come through here as data.
    """
    status = str(payload.get("status") or "")
    if status not in (
        "created",
        "updated",
        "duplicate",
        "version_conflict",
        "slug_collision",
        "invalid_input",
        "content_too_large",
    ):
        raise LithosClientError(
            "invalid_response",
            f"unexpected status in lithos_write response: {status!r}",
        )
    document = payload.get("document")
    note = (
        _parse_note(document, body_required=False)
        if isinstance(document, dict) and document
        else None
    )
    warnings_raw = payload.get("warnings") or []
    return WriteResult(
        status=status,  # type: ignore[arg-type]
        note=note,
        current_version=(
            int(payload["current_version"])
            if isinstance(payload.get("current_version"), int)
            else None
        ),
        slug_collision_existing_id=_optional_str(
            payload.get("slug_collision_existing_id")
        ),
        message=_optional_str(payload.get("message")),
        warnings=tuple(str(w) for w in warnings_raw),
    )


def _optional_str(value: Any) -> str | None:
    """Coerce a value to ``str`` only when truthy; ``None`` / empty
    string collapse to ``None`` so consumers don't have to disambiguate
    'absent' from 'empty string'."""
    if value is None:
        return None
    s = str(value)
    return s if s else None
