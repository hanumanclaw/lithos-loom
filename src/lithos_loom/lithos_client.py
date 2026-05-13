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

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import CallToolResult

from lithos_loom.errors import LithosClientError

__all__ = ["LithosClient", "Task"]


@dataclass(frozen=True)
class Task:
    """A Lithos task as returned by ``lithos_task_list``.

    Field set covers what the poller diffs over plus what the route-runner
    needs to make claim/match decisions. The Lithos spec also returns
    ``description``, ``created_by``, and ``created_at``; those are ignored
    for slice 0 and can be added without breaking call sites.
    """

    id: str
    title: str
    status: str  # open | completed | cancelled
    tags: tuple[str, ...]
    metadata: Mapping[str, Any]
    claims: tuple[Mapping[str, Any], ...]


class LithosClient:
    """MCP-over-SSE client for the Lithos server.

    Usage::

        async with LithosClient("http://localhost:8765") as client:
            tasks = await client.task_list(with_claims=True)
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._sse_ctx: Any = None
        self._session_ctx: Any = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> LithosClient:
        sse_url = f"{self.base_url}/sse"
        self._sse_ctx = sse_client(sse_url)
        read, write = await self._sse_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        session = await self._session_ctx.__aenter__()
        await session.initialize()
        self._session = session
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
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

    async def task_list(
        self,
        *,
        status: str | None = None,
        with_claims: bool = False,
    ) -> list[Task]:
        """Return tasks matching the filters. Omitted ``status`` = all."""
        if self._session is None:
            raise LithosClientError(
                "client_not_initialised",
                "LithosClient not initialised; use 'async with LithosClient(...) as c'",
            )
        arguments: dict[str, Any] = {"with_claims": with_claims}
        if status is not None:
            arguments["status"] = status
        result = await self._session.call_tool("lithos_task_list", arguments=arguments)
        return _parse_task_list_response(result)

    async def task_status(self, *, task_id: str) -> Task | None:
        """Return the current status of a single task.

        Returns ``None`` when Lithos reports ``task_not_found`` (the task
        was deleted entirely). All other error codes propagate as
        :class:`LithosClientError`. The returned :class:`Task` carries the
        fields ``lithos_task_status`` provides — ``id``, ``title``,
        ``status``, ``claims`` — with empty ``tags`` and empty ``metadata``
        (those fields are not exposed by the status endpoint).
        """
        if self._session is None:
            raise LithosClientError(
                "client_not_initialised",
                "LithosClient not initialised; use 'async with LithosClient(...) as c'",
            )
        result = await self._session.call_tool(
            "lithos_task_status", arguments={"task_id": task_id}
        )
        try:
            tasks = _parse_task_list_response(result)
        except LithosClientError as exc:
            if exc.code == "task_not_found":
                return None
            raise
        return tasks[0] if tasks else None


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
        return Task(
            id=str(raw["id"]),
            title=str(raw["title"]),
            status=str(raw["status"]),
            tags=tuple(tags_raw),
            metadata=dict(raw.get("metadata") or {}),
            claims=tuple(dict(c) for c in claims_raw),
        )
    except KeyError as exc:
        raise LithosClientError(
            "invalid_response", f"task entry missing required field: {exc.args[0]}"
        ) from exc


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
