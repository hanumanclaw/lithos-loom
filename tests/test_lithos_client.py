"""Tests for ``lithos_loom.lithos_client`` (Slice 0 US3).

The slice-0 surface is intentionally narrow: only ``task_list`` plus the
envelope-decoding helpers. The MCP-over-SSE transport is exercised through
``LithosClient`` itself, but the wire-format unit tests target the pure
parse helpers so we don't have to spin up a real Lithos to verify shape.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from lithos_loom import lithos_client as lithos_client_mod
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import (
    LithosClient,
    Note,
    NoteSummary,
    Task,
    WriteResult,
    _parse_note_list_response,
    _parse_note_read_response,
    _parse_task_list_response,
    _parse_write_result,
    _slug_from_path,
)

# ── _parse_task_list_response (pure helper) ────────────────────────────


def _content(data: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data))])


def test_parse_task_list_returns_typed_tasks() -> None:
    result = _content(
        {
            "tasks": [
                {
                    "id": "abc",
                    "title": "Build it",
                    "status": "open",
                    "tags": ["trigger:story-implement"],
                    "metadata": {"project": "lithos-loom"},
                    "claims": [],
                },
            ]
        }
    )
    tasks = _parse_task_list_response(result)
    assert len(tasks) == 1
    t = tasks[0]
    assert isinstance(t, Task)
    assert t.id == "abc"
    assert t.title == "Build it"
    assert t.status == "open"
    assert t.tags == ("trigger:story-implement",)
    assert t.metadata == {"project": "lithos-loom"}
    assert t.claims == ()


def test_parse_task_list_preserves_claims_when_with_claims_true() -> None:
    result = _content(
        {
            "tasks": [
                {
                    "id": "abc",
                    "title": "x",
                    "status": "open",
                    "tags": [],
                    "metadata": {},
                    "claims": [
                        {
                            "agent": "claude-code-1",
                            "aspect": "implementation",
                            "expires_at": "2026-05-15T12:00:00Z",
                        }
                    ],
                },
            ]
        }
    )
    tasks = _parse_task_list_response(result)
    assert len(tasks[0].claims) == 1
    assert tasks[0].claims[0]["agent"] == "claude-code-1"


def test_parse_task_list_returns_empty_list_for_empty_envelope() -> None:
    result = _content({"tasks": []})
    assert _parse_task_list_response(result) == []


def test_parse_task_list_raises_on_error_envelope() -> None:
    result = _content(
        {"status": "error", "code": "invalid_input", "message": "bad status filter"}
    )
    with pytest.raises(LithosClientError) as exc:
        _parse_task_list_response(result)
    assert exc.value.code == "invalid_input"
    assert "bad status filter" in str(exc.value)


def test_parse_task_list_raises_when_result_is_marked_error() -> None:
    """A FastMCP-side isError=True must surface as LithosClientError."""
    err_result = CallToolResult(
        content=[TextContent(type="text", text="upstream blew up")],
        isError=True,
    )
    with pytest.raises(LithosClientError):
        _parse_task_list_response(err_result)


def test_parse_task_list_raises_on_missing_tasks_key() -> None:
    result = _content({"unexpected": "shape"})
    with pytest.raises(LithosClientError, match="missing 'tasks'"):
        _parse_task_list_response(result)


def test_parse_task_list_tolerates_missing_optional_fields() -> None:
    """Some tasks may lack `tags` or `metadata` or `claims` keys."""
    result = _content({"tasks": [{"id": "x", "title": "t", "status": "open"}]})
    tasks = _parse_task_list_response(result)
    assert tasks[0].tags == ()
    assert tasks[0].metadata == {}
    assert tasks[0].claims == ()


# ── LithosClient.task_list (through-the-SDK happy-path) ────────────────


async def test_lithos_client_task_list_calls_correct_tool() -> None:
    """``task_list`` posts the right MCP tool name + arguments."""
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content({"tasks": []})
    client._session = fake_session  # type: ignore[assignment]

    await client.task_list(status="open", with_claims=True)

    fake_session.call_tool.assert_awaited_once_with(
        "lithos_task_list", arguments={"with_claims": True, "status": "open"}
    )


async def test_lithos_client_task_list_omits_none_filters() -> None:
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content({"tasks": []})
    client._session = fake_session  # type: ignore[assignment]

    await client.task_list()

    fake_session.call_tool.assert_awaited_once_with(
        "lithos_task_list", arguments={"with_claims": False}
    )


async def test_lithos_client_task_list_returns_parsed_tasks() -> None:
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content(
        {
            "tasks": [
                {
                    "id": "abc",
                    "title": "t",
                    "status": "open",
                    "tags": ["x"],
                    "metadata": {},
                    "claims": [],
                },
            ]
        }
    )
    client._session = fake_session  # type: ignore[assignment]

    tasks = await client.task_list()
    assert len(tasks) == 1
    assert tasks[0].id == "abc"


async def test_lithos_client_task_list_raises_when_not_initialized() -> None:
    client = LithosClient(base_url="http://example.test:8765")
    with pytest.raises(LithosClientError, match="not initialised"):
        await client.task_list()


async def test_lithos_client_task_list_passes_resolved_since_as_iso_string() -> None:
    """lithos#286: server-side resolved_since filter is sent as an
    ISO-8601 datetime string. Loom converts the datetime arg at the
    boundary so callers stay in Python time."""
    from datetime import UTC, datetime

    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content({"tasks": []})
    client._session = fake_session  # type: ignore[assignment]

    cutoff = datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)
    await client.task_list(status="completed", resolved_since=cutoff)

    fake_session.call_tool.assert_awaited_once_with(
        "lithos_task_list",
        arguments={
            "with_claims": False,
            "status": "completed",
            "resolved_since": cutoff.isoformat(),
        },
    )


async def test_lithos_client_task_list_omits_resolved_since_when_none() -> None:
    """Wire-identical to the pre-#286 contract when the new kwarg is
    not used — important during the staging→prod rollout window so an
    old Lithos doesn't trip on an unknown parameter."""
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content({"tasks": []})
    client._session = fake_session  # type: ignore[assignment]

    await client.task_list(status="open")

    fake_session.call_tool.assert_awaited_once_with(
        "lithos_task_list", arguments={"with_claims": False, "status": "open"}
    )


# ── _parse_task resolved_at handling ───────────────────────────────────


def test_parse_task_reads_resolved_at_field() -> None:
    """lithos#286 renamed the column to resolved_at; loom reads the new
    payload key into Task.resolved_at as a parsed datetime."""
    from datetime import datetime

    result = _content(
        {
            "tasks": [
                {
                    "id": "abc",
                    "title": "t",
                    "status": "completed",
                    "resolved_at": "2026-05-21T10:00:00+00:00",
                }
            ]
        }
    )
    tasks = _parse_task_list_response(result)
    assert tasks[0].resolved_at == datetime.fromisoformat("2026-05-21T10:00:00+00:00")


def test_parse_task_resolved_at_absent_is_none() -> None:
    """Open tasks (no resolved_at) parse to Task.resolved_at == None."""
    result = _content(
        {
            "tasks": [
                {"id": "x", "title": "t", "status": "open"},
            ]
        }
    )
    tasks = _parse_task_list_response(result)
    assert tasks[0].resolved_at is None


def test_parse_task_ignores_legacy_completed_at_key() -> None:
    """Defence in depth: an old Lithos server emitting completed_at
    instead of resolved_at must not crash; the field stays None and the
    projection layer falls back to event.timestamp. (Loom can roll out
    against a still-old server during staging → prod transitions.)"""
    result = _content(
        {
            "tasks": [
                {
                    "id": "x",
                    "title": "t",
                    "status": "completed",
                    "completed_at": "2026-05-21T10:00:00+00:00",
                }
            ]
        }
    )
    tasks = _parse_task_list_response(result)
    assert tasks[0].resolved_at is None


# ── LithosClient.task_status ──────────────────────────────────────────


async def test_lithos_client_task_status_returns_parsed_task() -> None:
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content(
        {
            "tasks": [
                {
                    "id": "abc",
                    "title": "t",
                    "status": "completed",
                    "claims": [],
                }
            ]
        }
    )
    client._session = fake_session  # type: ignore[assignment]

    task = await client.task_status(task_id="abc")
    assert task is not None
    assert task.id == "abc"
    assert task.status == "completed"
    fake_session.call_tool.assert_awaited_once_with(
        "lithos_task_status", arguments={"task_id": "abc"}
    )


async def test_lithos_client_task_status_returns_none_when_task_not_found() -> None:
    """``task_not_found`` is a routine outcome, not an exception."""
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content(
        {"status": "error", "code": "task_not_found", "message": "no such task"}
    )
    client._session = fake_session  # type: ignore[assignment]

    assert await client.task_status(task_id="missing") is None


async def test_lithos_client_task_status_propagates_other_errors() -> None:
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content(
        {"status": "error", "code": "invalid_input", "message": "bad id"}
    )
    client._session = fake_session  # type: ignore[assignment]

    with pytest.raises(LithosClientError) as exc:
        await client.task_status(task_id="x")
    assert exc.value.code == "invalid_input"


async def test_lithos_client_task_status_parses_full_envelope_post_lithos_294() -> None:
    """Post-lithos#294 the status envelope carries the full task record
    plus claims. Each new field surfaces on the returned :class:`Task`."""
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content(
        {
            "tasks": [
                {
                    "id": "abc",
                    "title": "Review PR",
                    "description": "Look it over carefully.",
                    "status": "completed",
                    "created_by": "alice",
                    "created_at": "2026-05-20T09:00:00+00:00",
                    "resolved_at": "2026-05-21T10:00:00+00:00",
                    "tags": ["code-review", "priority:high"],
                    "metadata": {"priority": "high", "depends_on": ["dep1"]},
                    "outcome": "approved",
                    "claims": [
                        {
                            "agent": "agent-a",
                            "aspect": "work",
                            "expires_at": "2026-05-22T00:00:00+00:00",
                        }
                    ],
                }
            ]
        }
    )
    client._session = fake_session  # type: ignore[assignment]

    task = await client.task_status(task_id="abc")
    assert task is not None
    assert task.id == "abc"
    assert task.title == "Review PR"
    assert task.description == "Look it over carefully."
    assert task.status == "completed"
    assert task.created_by == "alice"
    assert task.created_at is not None
    assert task.tags == ("code-review", "priority:high")
    assert task.metadata == {"priority": "high", "depends_on": ["dep1"]}
    assert task.outcome == "approved"
    assert task.resolved_at is not None
    assert len(task.claims) == 1
    assert task.claims[0]["agent"] == "agent-a"


# ── LithosClient.task_get (lithos#294) ────────────────────────────────


async def test_lithos_client_task_get_returns_parsed_task() -> None:
    """``task_get`` parses the single-object ``{task: {...}}`` envelope
    introduced in lithos#294 — no list wrapper, no claims."""
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content(
        {
            "task": {
                "id": "abc",
                "title": "Task",
                "description": "desc",
                "status": "open",
                "created_by": "agent",
                "created_at": "2026-05-20T09:00:00+00:00",
                "resolved_at": None,
                "tags": ["a"],
                "metadata": {"priority": "high"},
                "outcome": None,
            }
        }
    )
    client._session = fake_session  # type: ignore[assignment]

    task = await client.task_get(task_id="abc")
    assert task is not None
    assert task.id == "abc"
    assert task.status == "open"
    assert task.metadata == {"priority": "high"}
    # Claims default to an empty tuple — task_get never returns them.
    assert task.claims == ()
    fake_session.call_tool.assert_awaited_once_with(
        "lithos_task_get", arguments={"task_id": "abc"}
    )


async def test_lithos_client_task_get_returns_none_when_task_not_found() -> None:
    """``task_not_found`` is a routine outcome — mapped to ``None`` to
    match the :meth:`task_status` convention."""
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content(
        {"status": "error", "code": "task_not_found", "message": "no such task"}
    )
    client._session = fake_session  # type: ignore[assignment]

    assert await client.task_get(task_id="missing") is None


async def test_lithos_client_task_get_propagates_other_errors() -> None:
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content(
        {"status": "error", "code": "invalid_input", "message": "bad id"}
    )
    client._session = fake_session  # type: ignore[assignment]

    with pytest.raises(LithosClientError) as exc:
        await client.task_get(task_id="x")
    assert exc.value.code == "invalid_input"


async def test_lithos_client_task_get_handles_minimal_envelope() -> None:
    """Defensive: a server that returns only the required fields
    (``id, title, status``) should still parse without error.
    Backwards-compat in case future Lithos trims optional keys or a
    test server stubs a minimal response."""
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content(
        {"task": {"id": "abc", "title": "t", "status": "open"}}
    )
    client._session = fake_session  # type: ignore[assignment]

    task = await client.task_get(task_id="abc")
    assert task is not None
    assert task.metadata == {}
    assert task.tags == ()
    assert task.description is None
    assert task.created_by == ""
    assert task.created_at is None
    assert task.outcome is None


async def test_lithos_client_task_get_rejects_missing_task_key() -> None:
    """Malformed envelope (no 'task' key, no error envelope) is an
    invalid_response, not a silent success with defaults."""
    client = LithosClient(base_url="http://example.test:8765")
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = _content({"unexpected": "shape"})
    client._session = fake_session  # type: ignore[assignment]

    with pytest.raises(LithosClientError) as exc:
        await client.task_get(task_id="x")
    assert exc.value.code == "invalid_response"


# ── task_claim / task_renew / task_release / task_complete / task_update ─


def _client_with_session(response: Any) -> tuple[LithosClient, AsyncMock]:
    client = LithosClient(
        base_url="http://example.test:8765", agent_id="lithos-orchestrator-test"
    )
    fake_session = AsyncMock()
    fake_session.call_tool.return_value = response
    client._session = fake_session  # type: ignore[assignment]
    return client, fake_session


def _client_with_router(by_tool: dict[str, Any]) -> tuple[LithosClient, AsyncMock]:
    """A client whose ``call_tool`` returns a different result per tool name —
    needed once a method makes more than one tool call (e.g. ``task_claim``'s
    ``claim_failed`` ownership re-check fetches ``lithos_task_status``)."""
    client = LithosClient(
        base_url="http://example.test:8765", agent_id="lithos-orchestrator-test"
    )
    fake_session = AsyncMock()

    async def _route(tool: str, *, arguments: dict[str, Any] | None = None) -> Any:
        if tool not in by_tool:
            raise AssertionError(f"unexpected tool call: {tool!r}")
        return by_tool[tool]

    fake_session.call_tool.side_effect = _route
    client._session = fake_session  # type: ignore[assignment]
    return client, fake_session


def _task_with_claims(claims: list[dict[str, Any]]) -> CallToolResult:
    return _content(
        {
            "tasks": [
                {
                    "id": "t-1",
                    "title": "x",
                    "status": "open",
                    "tags": [],
                    "metadata": {},
                    "claims": claims,
                }
            ]
        }
    )


_CLAIM_FAILED = {"status": "error", "code": "claim_failed", "message": "aspect taken"}


async def test_task_claim_returns_expires_at_and_passes_arguments() -> None:
    client, session = _client_with_session(
        _content({"success": True, "expires_at": "2026-05-13T12:00:00Z"})
    )
    expires = await client.task_claim(task_id="t-1", aspect="impl", ttl_minutes=30)
    assert expires == "2026-05-13T12:00:00Z"
    session.call_tool.assert_awaited_once_with(
        "lithos_task_claim",
        arguments={
            "task_id": "t-1",
            "aspect": "impl",
            "agent": "lithos-orchestrator-test",
            "ttl_minutes": 30,
        },
    )


async def test_task_claim_reraises_when_another_agent_holds_it() -> None:
    """Genuine race-loss: the aspect is held by a DIFFERENT agent, so the
    ownership re-check confirms it's not ours → claim_failed propagates."""
    client, _ = _client_with_router(
        {
            "lithos_task_claim": _content(_CLAIM_FAILED),
            "lithos_task_status": _task_with_claims(
                [
                    {
                        "agent": "other-runner",
                        "aspect": "impl",
                        "expires_at": "2026-05-20",
                    }
                ]
            ),
        }
    )
    with pytest.raises(LithosClientError) as exc:
        await client.task_claim(task_id="t-1", aspect="impl")
    assert exc.value.code == "claim_failed"


async def test_task_claim_propagates_first_attempt_self_held_claim_failed() -> None:
    """PR #60 review (2026-05-29): on a *first-attempt* claim_failed, the
    ownership re-check is **skipped** — a same-``agent_id`` holder must be
    a different Loom process (no retry → no committed-but-response-lost
    request of ours). Silently treating it as success would let both
    processes proceed past task_claim and run the plugin in parallel."""
    client, _ = _client_with_router(
        {
            "lithos_task_claim": _content(_CLAIM_FAILED),
            # Re-check would happily report our agent as the holder — proving
            # the re-check ISN'T consulted on first-attempt failure.
            "lithos_task_status": _task_with_claims(
                [
                    {
                        "agent": "lithos-orchestrator-test",
                        "aspect": "impl",
                        "expires_at": "2026-05-20T12:00:00Z",
                    }
                ]
            ),
        }
    )
    with pytest.raises(LithosClientError) as exc:
        await client.task_claim(task_id="t-1", aspect="impl")
    assert exc.value.code == "claim_failed"


async def test_task_claim_treats_self_held_claim_failed_as_success_on_retry() -> None:
    """#43 + PR #60 review (2026-05-29): the ownership re-check IS consulted
    when ``_invoke`` actually retried this call. This is the "we claimed,
    response was lost mid-flight, we retried, server reports claim_failed
    because we already hold it" path — return the held expiry rather than
    visibly failing (which would make RouteRunner skip work it owns).

    Simulated by raising a transport error on the first ``call_tool`` so
    ``_invoke`` reconnects, then returning ``claim_failed`` on the retry."""
    client = LithosClient(
        base_url="http://example.test:8765", agent_id="lithos-orchestrator-test"
    )
    fake_session = AsyncMock()
    claim_attempts = 0

    async def _route(tool: str, *, arguments: dict[str, Any] | None = None) -> Any:
        nonlocal claim_attempts
        if tool == "lithos_task_claim":
            claim_attempts += 1
            if claim_attempts == 1:
                raise _DeadError("sse stream closed mid-claim")
            return _content(_CLAIM_FAILED)
        if tool == "lithos_task_status":
            return _task_with_claims(
                [
                    {
                        "agent": "lithos-orchestrator-test",
                        "aspect": "impl",
                        "expires_at": "2026-05-20T12:00:00Z",
                    }
                ]
            )
        raise AssertionError(f"unexpected tool call: {tool!r}")

    fake_session.call_tool.side_effect = _route
    client._session = fake_session  # type: ignore[assignment]
    # _invoke needs a way to reconnect; install a no-op _establish that
    # keeps the same session installed (the reconnect itself is a side
    # effect we don't care about for this test).
    _patch_establish(client, [fake_session])

    expires = await client.task_claim(task_id="t-1", aspect="impl")

    assert expires == "2026-05-20T12:00:00Z"
    assert claim_attempts == 2  # first call transport-failed, retry got claim_failed


async def test_task_claim_reraises_when_we_hold_a_different_aspect() -> None:
    """We hold a claim, but on a different aspect → not ours for THIS aspect
    → claim_failed propagates (no false success)."""
    client, _ = _client_with_router(
        {
            "lithos_task_claim": _content(_CLAIM_FAILED),
            "lithos_task_status": _task_with_claims(
                [
                    {
                        "agent": "lithos-orchestrator-test",
                        "aspect": "review-human",
                        "expires_at": "2026-05-20",
                    }
                ]
            ),
        }
    )
    with pytest.raises(LithosClientError) as exc:
        await client.task_claim(task_id="t-1", aspect="impl")
    assert exc.value.code == "claim_failed"


async def test_task_claim_reraises_claim_failed_when_task_gone() -> None:
    """claim_failed + the task no longer exists (task_status → not found) →
    nothing of ours to find → claim_failed propagates."""
    client, _ = _client_with_router(
        {
            "lithos_task_claim": _content(_CLAIM_FAILED),
            "lithos_task_status": _content(
                {"status": "error", "code": "task_not_found", "message": "gone"}
            ),
        }
    )
    with pytest.raises(LithosClientError) as exc:
        await client.task_claim(task_id="t-1", aspect="impl")
    assert exc.value.code == "claim_failed"


async def test_task_renew_returns_new_expires_at() -> None:
    client, _ = _client_with_session(
        _content({"success": True, "new_expires_at": "2026-05-13T13:00:00Z"})
    )
    expires = await client.task_renew(task_id="t-1", aspect="impl", ttl_minutes=15)
    assert expires == "2026-05-13T13:00:00Z"


async def test_task_release_treats_claim_not_found_as_noop() -> None:
    """Routine outcome — a missing claim on release is not an error."""
    client, _ = _client_with_session(
        _content({"status": "error", "code": "claim_not_found", "message": "no claim"})
    )
    # Must not raise.
    await client.task_release(task_id="t-1", aspect="impl")


async def test_task_release_propagates_other_errors() -> None:
    client, _ = _client_with_session(
        _content({"status": "error", "code": "task_not_found", "message": "x"})
    )
    with pytest.raises(LithosClientError):
        await client.task_release(task_id="t-1", aspect="impl")


async def test_task_complete_invokes_correct_tool() -> None:
    client, session = _client_with_session(_content({"success": True}))
    await client.task_complete(task_id="t-1")
    session.call_tool.assert_awaited_once_with(
        "lithos_task_complete",
        arguments={"task_id": "t-1", "agent": "lithos-orchestrator-test"},
    )


async def test_task_cancel_invokes_correct_tool() -> None:
    """``task_cancel(task_id=...)`` with no explicit agent or reason
    sends just ``{task_id, agent: <client default>}`` to the MCP tool."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_cancel(task_id="t-1")
    session.call_tool.assert_awaited_once_with(
        "lithos_task_cancel",
        arguments={"task_id": "t-1", "agent": "lithos-orchestrator-test"},
    )


async def test_task_cancel_passes_reason_when_provided() -> None:
    """Explicit ``reason`` is forwarded to Lithos so MCP-level logs
    carry the breadcrumb (Lithos doesn't persist it but accepts it)."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_cancel(task_id="t-1", reason="user request")
    session.call_tool.assert_awaited_once_with(
        "lithos_task_cancel",
        arguments={
            "task_id": "t-1",
            "agent": "lithos-orchestrator-test",
            "reason": "user request",
        },
    )


async def test_task_cancel_omits_reason_when_none() -> None:
    """``reason=None`` (the default) must NOT add a ``"reason": None``
    key — older/strict Lithos servers shouldn't see the field at all.
    Mirrors the ``resolved_since``-omit-when-none pattern in ``task_list``."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_cancel(task_id="t-1", reason=None)
    args = session.call_tool.await_args.kwargs["arguments"]
    assert "reason" not in args, args


async def test_task_cancel_uses_explicit_agent_over_default() -> None:
    """Explicit ``agent=`` overrides the client's default ``agent_id``."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_cancel(task_id="t-1", agent="alt-agent")
    args = session.call_tool.await_args.kwargs["arguments"]
    assert args["agent"] == "alt-agent"


async def test_task_cancel_raises_when_no_agent_anywhere() -> None:
    """Client with no ``agent_id`` AND no explicit agent arg → raises."""
    client = LithosClient(base_url="http://example.test:8765")  # no agent_id
    fake_session = AsyncMock()
    client._session = fake_session  # type: ignore[assignment]
    with pytest.raises(LithosClientError, match="agent"):
        await client.task_cancel(task_id="t-1")


async def test_task_update_omits_unset_fields() -> None:
    client, session = _client_with_session(_content({"success": True}))
    await client.task_update(task_id="t-1", tags=["a", "b"])
    session.call_tool.assert_awaited_once_with(
        "lithos_task_update",
        arguments={
            "task_id": "t-1",
            "agent": "lithos-orchestrator-test",
            "tags": ["a", "b"],
        },
    )


async def test_task_update_rejects_empty_call() -> None:
    """Lithos requires at least one of title/description/tags/metadata
    (post-#290 adds metadata to the at-least-one list)."""
    client, _ = _client_with_session(_content({"success": True}))
    with pytest.raises(LithosClientError, match="at least one"):
        await client.task_update(task_id="t-1")


async def test_task_update_passes_metadata_when_provided() -> None:
    """``metadata`` kwarg (Lithos #290) is forwarded as the
    per-key merge patch on the MCP call."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_update(task_id="t-1", metadata={"priority": "high"})
    session.call_tool.assert_awaited_once_with(
        "lithos_task_update",
        arguments={
            "task_id": "t-1",
            "agent": "lithos-orchestrator-test",
            "metadata": {"priority": "high"},
        },
    )


async def test_task_update_metadata_with_none_value_passes_through() -> None:
    """A ``None`` value inside the metadata dict (Python ``None`` →
    JSON ``null``) is preserved on the wire. Lithos's merge
    semantics interpret null as "delete this key" — the client
    doesn't filter it out."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_update(task_id="t-1", metadata={"priority": None})
    args = session.call_tool.await_args.kwargs["arguments"]
    assert args["metadata"] == {"priority": None}


async def test_task_update_omits_metadata_arg_when_none() -> None:
    """``metadata=None`` (default) → no ``"metadata"`` key in the MCP
    args. Distinct from ``metadata={}`` (which Lithos treats as a
    no-op patch) or ``metadata={"k": None}`` (delete the key).
    Mirrors the pattern other optional args use."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_update(task_id="t-1", tags=["x"])  # no metadata
    args = session.call_tool.await_args.kwargs["arguments"]
    assert "metadata" not in args


async def test_task_update_metadata_alone_satisfies_at_least_one() -> None:
    """Per Lithos #290, the at-least-one constraint now accepts
    metadata as the satisfier — title/description/tags can all be
    omitted if metadata is provided."""
    client, session = _client_with_session(_content({"success": True}))
    await client.task_update(task_id="t-1", metadata={"priority": "low"})
    session.call_tool.assert_awaited_once()


async def test_task_lifecycle_methods_require_agent_id() -> None:
    client = LithosClient(base_url="http://example.test:8765")  # no agent_id
    fake_session = AsyncMock()
    client._session = fake_session  # type: ignore[assignment]
    with pytest.raises(LithosClientError, match="agent"):
        await client.task_claim(task_id="t-1", aspect="impl")
    with pytest.raises(LithosClientError, match="agent"):
        await client.task_complete(task_id="t-1")
    with pytest.raises(LithosClientError, match="agent"):
        await client.task_create(title="t")


# ── LithosClient.task_create (lithos#295) ─────────────────────────────


async def test_task_create_returns_task_id_and_passes_arguments() -> None:
    """Happy path: passes title + agent (defaults to client's
    agent_id), parses the ``{task_id: ...}`` response envelope."""
    client, session = _client_with_session(_content({"task_id": "new-1"}))

    task_id = await client.task_create(title="Review PR")

    assert task_id == "new-1"
    session.call_tool.assert_awaited_once_with(
        "lithos_task_create",
        arguments={"title": "Review PR", "agent": "lithos-orchestrator-test"},
    )


async def test_task_create_forwards_description_tags_metadata() -> None:
    """Optional fields are forwarded verbatim when provided. The
    post-lithos#295 metadata argument is the load-bearing one for
    Slice 3 ("born projected" lines need metadata.project /
    .priority / .scheduled_for set at create time)."""
    client, session = _client_with_session(_content({"task_id": "x"}))

    await client.task_create(
        title="t",
        description="brief",
        tags=["a", "b"],
        metadata={"project": "lithos-loom", "priority": "high"},
    )

    args = session.call_tool.await_args.kwargs["arguments"]
    assert args["title"] == "t"
    assert args["description"] == "brief"
    assert args["tags"] == ["a", "b"]
    assert args["metadata"] == {"project": "lithos-loom", "priority": "high"}


async def test_task_create_omits_optional_args_when_none() -> None:
    """``None`` defaults are omitted from the MCP arguments dict so
    old/strict Lithos servers don't choke on unexpected keys."""
    client, session = _client_with_session(_content({"task_id": "x"}))

    await client.task_create(title="t")  # only required field

    args = session.call_tool.await_args.kwargs["arguments"]
    assert set(args.keys()) == {"title", "agent"}


async def test_task_create_uses_explicit_agent_when_provided() -> None:
    """An explicit ``agent=`` overrides the client-level default."""
    client, session = _client_with_session(_content({"task_id": "x"}))

    await client.task_create(title="t", agent="lithos-orchestrator-mac-mini")

    args = session.call_tool.await_args.kwargs["arguments"]
    assert args["agent"] == "lithos-orchestrator-mac-mini"


async def test_task_create_raises_when_response_missing_task_id() -> None:
    """Defensive: a malformed response (no ``task_id`` key) surfaces
    as a typed ``invalid_response`` error rather than a silent
    ``None`` return."""
    client, _ = _client_with_session(_content({"unexpected": "shape"}))

    with pytest.raises(LithosClientError) as exc:
        await client.task_create(title="t")
    assert exc.value.code == "invalid_response"


async def test_task_create_propagates_lithos_error_envelope() -> None:
    """``invalid_input`` (or any other domain error) propagates as
    ``LithosClientError``, lining up with the rest of the surface."""
    client, _ = _client_with_session(
        _content({"status": "error", "code": "invalid_input", "message": "no title"})
    )

    with pytest.raises(LithosClientError) as exc:
        await client.task_create(title="")
    assert exc.value.code == "invalid_input"


# ── KB-doc surface (Slice 4 + 5) ──────────────────────────────────────


# Pure helper: slug extraction.


def test_slug_from_path_extracts_first_segment_under_projects() -> None:
    assert _slug_from_path("projects/lithos-loom/context.md") == "lithos-loom"
    assert _slug_from_path("projects/influx/context.md") == "influx"


def test_slug_from_path_handles_deep_paths() -> None:
    """Slug is the FIRST path segment after ``projects/``; nested
    docs under the same slug still resolve to the same slug."""
    assert (
        _slug_from_path("projects/lithos-loom/architecture/design.md") == "lithos-loom"
    )


def test_slug_from_path_falls_back_to_first_segment_for_non_projects() -> None:
    """Docs outside ``projects/`` still get a slug (best-effort) so
    the field is always populated. Projection's subscription filter
    rejects them by path-prefix before this matters in practice."""
    assert _slug_from_path("observations/inbox/foo.md") == "observations"


def test_slug_from_path_empty_string() -> None:
    assert _slug_from_path("") == ""


# Pure helper: note read response parsing.


def test_parse_note_read_returns_typed_note() -> None:
    result = _content(
        {
            "id": "doc-1",
            "title": "Lithos Loom",
            "content": "# Lithos Loom\n\nBody here.",
            "path": "projects/lithos-loom/context.md",
            "metadata": {
                "version": 12,
                "updated_at": "2026-05-24T14:30:00Z",
                "tags": ["project-context", "track-1"],
                "status": "active",
                "note_type": "concept",
            },
        }
    )
    note = _parse_note_read_response(result)
    assert isinstance(note, Note)
    assert note.id == "doc-1"
    assert note.title == "Lithos Loom"
    assert note.body == "# Lithos Loom\n\nBody here."
    assert note.version == 12
    assert note.tags == ("project-context", "track-1")
    assert note.status == "active"
    assert note.note_type == "concept"
    assert note.path == "projects/lithos-loom/context.md"
    assert note.slug == "lithos-loom"


def test_parse_note_read_raises_when_content_missing() -> None:
    """``note_read`` is the body-required path — missing ``content``
    is a server-side bug that should surface, not pass silently."""
    result = _content(
        {
            "id": "doc-1",
            "title": "x",
            "path": "projects/foo/context.md",
            "metadata": {"version": 1},
        }
    )
    with pytest.raises(LithosClientError, match="missing 'content'"):
        _parse_note_read_response(result)


def test_parse_note_read_tolerates_missing_optional_metadata_fields() -> None:
    """Tags / status / note_type / updated_at may all be absent on a
    freshly-created doc — the parser fills them with safe defaults."""
    result = _content(
        {
            "id": "doc-1",
            "title": "x",
            "content": "body",
            "path": "projects/foo/context.md",
            "metadata": {"version": 1},
        }
    )
    note = _parse_note_read_response(result)
    assert note.tags == ()
    assert note.status is None
    assert note.note_type is None
    assert note.updated_at is None


def test_parse_note_read_raises_on_error_envelope() -> None:
    result = _content(
        {"status": "error", "code": "doc_not_found", "message": "no such doc"}
    )
    with pytest.raises(LithosClientError) as exc:
        _parse_note_read_response(result)
    assert exc.value.code == "doc_not_found"


def test_parse_note_read_wraps_malformed_version_as_invalid_response() -> None:
    """A non-numeric ``metadata.version`` (e.g. server returned a
    string by mistake) must surface as
    ``LithosClientError("invalid_response", ...)``, not as a bare
    ``ValueError`` from ``int(...)``. The parser is the boundary
    between Lithos's loose JSON shape and the client's typed
    surface — coercion failures must wear the client's envelope so
    callers can ``except LithosClientError`` uniformly."""
    result = _content(
        {
            "id": "doc-1",
            "title": "x",
            "content": "body",
            "path": "projects/foo/context.md",
            "metadata": {"version": "abc"},  # bad type
        }
    )
    with pytest.raises(LithosClientError) as exc:
        _parse_note_read_response(result)
    assert exc.value.code == "invalid_response"


def test_parse_note_read_wraps_malformed_version_list_as_invalid_response() -> None:
    """Same as above but with ``version`` as a non-empty list —
    exercises the ``TypeError`` branch of the catch (lists don't
    coerce to int). Empty list falls back to 0 via the ``or`` chain,
    which is the degenerate-but-harmless case; non-empty list is
    the genuinely malformed shape that must surface clean."""
    result = _content(
        {
            "id": "doc-1",
            "title": "x",
            "content": "body",
            "path": "projects/foo/context.md",
            "metadata": {"version": [1, 2]},
        }
    )
    with pytest.raises(LithosClientError) as exc:
        _parse_note_read_response(result)
    assert exc.value.code == "invalid_response"


# Pure helper: note list response parsing.


def test_parse_note_list_returns_typed_summaries() -> None:
    result = _content(
        {
            "items": [
                {
                    "id": "doc-1",
                    "title": "Lithos Loom",
                    "path": "projects/lithos-loom/context.md",
                    "metadata": {
                        "version": 12,
                        "tags": ["project-context"],
                        "status": "active",
                        "note_type": "concept",
                    },
                },
                {
                    "id": "doc-2",
                    "title": "Influx",
                    "path": "projects/influx/context.md",
                    "metadata": {
                        "version": 3,
                        "tags": ["project-context"],
                        "status": "active",
                        "note_type": "concept",
                    },
                },
            ]
        }
    )
    summaries = _parse_note_list_response(result)
    assert len(summaries) == 2
    assert all(isinstance(s, NoteSummary) for s in summaries)
    assert summaries[0].slug == "lithos-loom"
    assert summaries[1].slug == "influx"


def test_parse_note_list_accepts_results_alias() -> None:
    """Some Lithos versions wrap the list as ``results`` instead of
    ``items``. Both shapes are tolerated."""
    result = _content(
        {
            "results": [
                {
                    "id": "doc-1",
                    "title": "x",
                    "path": "projects/foo/context.md",
                    "metadata": {"version": 1},
                }
            ]
        }
    )
    summaries = _parse_note_list_response(result)
    assert len(summaries) == 1


def test_parse_note_list_returns_empty_list_when_no_items() -> None:
    result = _content({"items": []})
    assert _parse_note_list_response(result) == []


def test_parse_note_list_raises_on_missing_items_key() -> None:
    result = _content({"unexpected": "shape"})
    with pytest.raises(LithosClientError, match="missing 'items'"):
        _parse_note_list_response(result)


def test_parse_note_list_raises_on_error_envelope() -> None:
    result = _content(
        {"status": "error", "code": "invalid_input", "message": "bad prefix"}
    )
    with pytest.raises(LithosClientError) as exc:
        _parse_note_list_response(result)
    assert exc.value.code == "invalid_input"


# Pure helper: write result parsing.


def test_parse_write_result_created() -> None:
    payload = {
        "status": "created",
        "document": {
            "id": "doc-new",
            "title": "x",
            "path": "projects/x/context.md",
            "metadata": {"version": 1},
        },
    }
    wr = _parse_write_result(payload)
    assert wr.status == "created"
    assert wr.note is not None
    assert wr.note.id == "doc-new"


def test_parse_write_result_version_conflict_carries_current_version() -> None:
    """The conflict envelope must carry ``current_version`` so the
    bidirectional-sync caller can pull + diff against it."""
    payload = {
        "status": "version_conflict",
        "message": "expected 11, got 12",
        "current_version": 12,
    }
    wr = _parse_write_result(payload)
    assert wr.status == "version_conflict"
    assert wr.current_version == 12
    assert wr.note is None
    assert "expected 11" in (wr.message or "")


def test_parse_write_result_slug_collision_carries_existing_id() -> None:
    payload = {
        "status": "slug_collision",
        "message": "slug 'foo' already used by doc-42",
        "slug_collision_existing_id": "doc-42",
    }
    wr = _parse_write_result(payload)
    assert wr.status == "slug_collision"
    assert wr.slug_collision_existing_id == "doc-42"


def test_parse_write_result_rejects_unknown_status() -> None:
    """A status value Lithos doesn't define is a server-side bug
    that should surface, not pass silently."""
    payload = {"status": "future_outcome_we_dont_know_about"}
    with pytest.raises(LithosClientError, match="unexpected status"):
        _parse_write_result(payload)


def test_parse_write_result_top_level_shape_returns_none_note() -> None:
    """Real Lithos returns ``{status, id, path, version, warnings}`` at
    the top level — NO ``document`` key. ``_parse_write_result`` is
    pure (only parses what's in the payload), so it returns
    ``note=None`` for this shape; ``note_write`` is what enriches the
    result with request-side fields (title/tags/etc) to construct a
    complete Note. This test pins the parser's contract — anyone
    refactoring it must keep the parser pure."""
    payload = {
        "status": "created",
        "id": "doc-real-server-id",
        "path": "projects/foo/foo-project-context.md",
        "version": 1,
        "warnings": [],
    }
    wr = _parse_write_result(payload)
    assert wr.status == "created"
    assert wr.note is None  # parser doesn't see request inputs, can't build a full Note


# Async method tests.


async def test_note_read_returns_note_when_found() -> None:
    client, session = _client_with_session(
        _content(
            {
                "id": "doc-1",
                "title": "Loom",
                "content": "body",
                "path": "projects/loom/context.md",
                "metadata": {"version": 1, "tags": ["project-context"]},
            }
        )
    )
    note = await client.note_read(id="doc-1")
    assert note is not None
    assert note.id == "doc-1"
    assert note.slug == "loom"
    session.call_tool.assert_awaited_once_with("lithos_read", arguments={"id": "doc-1"})


async def test_note_read_returns_none_on_doc_not_found() -> None:
    """``doc_not_found`` is folded into ``None`` so handlers can
    treat deleted docs as a no-op rather than try/except."""
    client, _ = _client_with_session(
        _content({"status": "error", "code": "doc_not_found", "message": "no"})
    )
    assert await client.note_read(id="ghost") is None


async def test_note_read_requires_id_or_path() -> None:
    client, _ = _client_with_session(_content({}))
    with pytest.raises(LithosClientError, match="one of id"):
        await client.note_read()


async def test_note_read_propagates_other_errors() -> None:
    client, _ = _client_with_session(
        _content({"status": "error", "code": "transport_failure", "message": "down"})
    )
    with pytest.raises(LithosClientError) as exc:
        await client.note_read(id="doc-1")
    assert exc.value.code == "transport_failure"


async def test_note_write_enriches_result_note_from_top_level_response() -> None:
    """Real Lithos returns ``{status, id, path, version, warnings}`` at
    the top level — no ``document`` field. ``note_write`` stitches the
    response's id/path/version with the request's title/tags/etc to
    construct a complete Note, so ``WriteResult.note`` is populated
    against a real server (PR #46 reviewer finding regression).

    This test pins the production behaviour end-to-end through the
    MCP session stub. Without the fix-up in ``note_write``,
    ``result.note`` would be None and the project-create CLI's
    ``--format json`` would emit ``"id": ""``."""
    client, session = _client_with_session(
        _content(
            {
                "status": "created",
                "id": "doc-real-canonical",
                "path": "projects/loom/loom-project-context.md",
                "version": 1,
                "warnings": [],
            }
        )
    )
    wr = await client.note_write(
        title="Loom",
        content="body content",
        tags=["project-context", "track-1"],
        path="projects/loom/loom-project-context.md",
        status="active",
    )
    assert wr.status == "created"
    assert wr.note is not None
    # Response-side fields.
    assert wr.note.id == "doc-real-canonical"
    assert wr.note.path == "projects/loom/loom-project-context.md"
    assert wr.note.version == 1
    assert wr.note.slug == "loom"  # derived from path
    # Request-side fields stitched in.
    assert wr.note.title == "Loom"
    assert wr.note.body == "body content"
    assert wr.note.tags == ("project-context", "track-1")
    assert wr.note.status == "active"
    assert wr.note.note_type == "concept"
    # updated_at NOT populated — response doesn't carry it.
    # Callers that need byte-stable lithos_updated_at must re-fetch.
    assert wr.note.updated_at is None


async def test_note_write_top_level_updated_response_also_enriches() -> None:
    """Same stitching for ``updated`` status."""
    client, _ = _client_with_session(
        _content(
            {
                "status": "updated",
                "id": "doc-1",
                "path": "projects/x/x-project-context.md",
                "version": 13,
                "warnings": [],
            }
        )
    )
    wr = await client.note_write(
        id="doc-1",
        title="X",
        content="new body",
        tags=["project-context"],
        expected_version=12,
    )
    assert wr.status == "updated"
    assert wr.note is not None
    assert wr.note.id == "doc-1"
    assert wr.note.version == 13
    assert wr.note.title == "X"
    assert wr.note.body == "new body"


async def test_note_write_create_passes_arguments() -> None:
    client, session = _client_with_session(
        _content(
            {
                "status": "created",
                "document": {
                    "id": "doc-new",
                    "title": "Loom",
                    "path": "projects/loom/context.md",
                    "metadata": {"version": 1},
                },
            }
        )
    )
    wr = await client.note_write(
        title="Loom",
        content="body",
        tags=["project-context"],
        path="projects/loom/context.md",
    )
    assert wr.status == "created"
    assert wr.note is not None
    session.call_tool.assert_awaited_once_with(
        "lithos_write",
        arguments={
            "title": "Loom",
            "content": "body",
            "agent": "lithos-orchestrator-test",
            "note_type": "concept",
            "tags": ["project-context"],
            "path": "projects/loom/context.md",
        },
    )


async def test_note_write_update_passes_expected_version() -> None:
    """The update path includes ``expected_version`` for optimistic
    locking — the Lithos surface compares this against the canonical
    version and returns ``version_conflict`` on mismatch."""
    client, session = _client_with_session(
        _content(
            {
                "status": "updated",
                "document": {
                    "id": "doc-1",
                    "title": "Loom",
                    "path": "projects/loom/context.md",
                    "metadata": {"version": 13},
                },
            }
        )
    )
    wr = await client.note_write(
        id="doc-1",
        title="Loom",
        content="new body",
        expected_version=12,
    )
    assert wr.status == "updated"
    args = session.call_tool.await_args.kwargs["arguments"]
    assert args["id"] == "doc-1"
    assert args["expected_version"] == 12


async def test_note_write_version_conflict_does_not_raise() -> None:
    """Version conflicts come back as data, not exceptions —
    bidirectional sync needs this branch to be expected, not a
    try/except site."""
    client, _ = _client_with_session(
        _content(
            {
                "status": "version_conflict",
                "message": "expected 11, got 12",
                "current_version": 12,
            }
        )
    )
    wr = await client.note_write(
        id="doc-1", title="x", content="y", expected_version=11
    )
    assert wr.status == "version_conflict"
    assert wr.current_version == 12


async def test_note_write_slug_collision_does_not_raise() -> None:
    """Same shape as version_conflict — slug collisions are operator-
    actionable data, not exceptional control flow."""
    client, _ = _client_with_session(
        _content(
            {
                "status": "slug_collision",
                "message": "taken",
                "slug_collision_existing_id": "doc-42",
            }
        )
    )
    wr = await client.note_write(title="x", content="y", path="projects/foo/context.md")
    assert wr.status == "slug_collision"
    assert wr.slug_collision_existing_id == "doc-42"


async def test_note_write_error_envelope_still_raises() -> None:
    """``status: "error"`` (the truly exceptional envelope) DOES
    raise — distinct from domain failures like version_conflict."""
    client, _ = _client_with_session(
        _content({"status": "error", "code": "transport_failure", "message": "down"})
    )
    with pytest.raises(LithosClientError) as exc:
        await client.note_write(title="x", content="y", path="projects/foo/context.md")
    assert exc.value.code == "transport_failure"


async def test_note_write_requires_agent_id() -> None:
    client = LithosClient(base_url="http://example.test:8765")  # no agent_id
    fake_session = AsyncMock()
    client._session = fake_session  # type: ignore[assignment]
    with pytest.raises(LithosClientError, match="agent"):
        await client.note_write(title="x", content="y", path="projects/foo/context.md")


async def test_note_write_omits_optional_args_when_none() -> None:
    """``id``, ``path``, ``expected_version``, ``status``, ``tags``
    are all omitted when None — strict Lithos servers don't reject
    unexpected keys, and absent keys are cleaner on the wire."""
    client, session = _client_with_session(
        _content(
            {
                "status": "created",
                "document": {
                    "id": "doc-new",
                    "title": "x",
                    "path": "",
                    "metadata": {"version": 1},
                },
            }
        )
    )
    await client.note_write(title="x", content="y")
    args = session.call_tool.await_args.kwargs["arguments"]
    assert "id" not in args
    assert "path" not in args
    assert "expected_version" not in args
    assert "status" not in args
    assert "tags" not in args


async def test_note_list_passes_filters_and_returns_summaries() -> None:
    client, session = _client_with_session(
        _content(
            {
                "items": [
                    {
                        "id": "doc-1",
                        "title": "Loom",
                        "path": "projects/loom/context.md",
                        "metadata": {"version": 1, "tags": ["project-context"]},
                    }
                ]
            }
        )
    )
    summaries = await client.note_list(
        path_prefix="projects/", tags=["project-context"]
    )
    assert len(summaries) == 1
    assert summaries[0].slug == "loom"
    session.call_tool.assert_awaited_once_with(
        "lithos_list",
        arguments={
            "limit": 100,
            "path_prefix": "projects/",
            "tags": ["project-context"],
        },
    )


async def test_note_list_default_limit_is_100() -> None:
    """Documented in the docstring; pin so a future change to a
    smaller default doesn't silently break the obsidian-sync child's
    bootstrap (~20 projects in current use)."""
    client, session = _client_with_session(_content({"items": []}))
    await client.note_list()
    args = session.call_tool.await_args.kwargs["arguments"]
    assert args["limit"] == 100


async def test_note_list_omits_filters_when_none() -> None:
    client, session = _client_with_session(_content({"items": []}))
    await client.note_list()
    args = session.call_tool.await_args.kwargs["arguments"]
    assert "path_prefix" not in args
    assert "tags" not in args


# ── LithosClient.note_delete ───────────────────────────────────────────


async def test_note_delete_returns_true_on_success() -> None:
    """Happy path: Lithos returns ``{success: True}`` → method returns True."""
    client, session = _client_with_session(_content({"success": True}))
    deleted = await client.note_delete(id="doc-1")
    assert deleted is True
    session.call_tool.assert_awaited_once_with(
        "lithos_delete",
        arguments={"id": "doc-1", "agent": "lithos-orchestrator-test"},
    )


async def test_note_delete_returns_false_on_doc_not_found() -> None:
    """``doc_not_found`` is folded to False so cleanup loops can
    call this idempotently — equivalent to ``rm -f``. The common
    use case is "delete these N test docs whether or not they
    exist", which would otherwise need try/except at every call
    site (the trap the user hit during soak 2026-05-24 before
    this wrapper existed)."""
    client, _ = _client_with_session(
        _content({"status": "error", "code": "doc_not_found", "message": "no"})
    )
    deleted = await client.note_delete(id="ghost")
    assert deleted is False


async def test_note_delete_propagates_other_errors() -> None:
    """Only ``doc_not_found`` is folded; other domain errors
    (permission, transport, internal) still raise so the caller
    knows their cleanup didn't actually run."""
    client, _ = _client_with_session(
        _content({"status": "error", "code": "permission_denied", "message": "no"})
    )
    with pytest.raises(LithosClientError) as exc:
        await client.note_delete(id="doc-1")
    assert exc.value.code == "permission_denied"


async def test_note_delete_uses_explicit_agent_when_provided() -> None:
    """Explicit ``agent=`` overrides the client's default — same
    shape as :meth:`note_write` / :meth:`finding_post`."""
    client, session = _client_with_session(_content({"success": True}))
    await client.note_delete(id="doc-1", agent="cleanup-bot")
    session.call_tool.assert_awaited_once_with(
        "lithos_delete",
        arguments={"id": "doc-1", "agent": "cleanup-bot"},
    )


async def test_note_delete_raises_on_missing_success_field() -> None:
    """The Lithos contract is ``{"success": True}`` on delete
    (lithos/src/lithos/server.py:1434). If the response is empty
    (``{}``) — e.g. server-side drift, a regression on the Lithos
    side, or a future API change — we MUST raise rather than
    silently report success. Otherwise a failed cleanup would
    leave the stale doc in place while Loom told the caller
    everything was fine."""
    client, _ = _client_with_session(_content({}))
    with pytest.raises(LithosClientError) as exc:
        await client.note_delete(id="doc-1")
    assert exc.value.code == "invalid_response"


async def test_note_delete_raises_on_success_false() -> None:
    """Explicit ``{"success": False}`` is the unambiguous "I tried
    and it didn't work" signal — must surface as an error rather
    than be folded to True (which would mask the cleanup failure)
    or False (which is reserved for the doc_not_found idempotent
    path). Treat shape divergence as a typed error."""
    client, _ = _client_with_session(_content({"success": False}))
    with pytest.raises(LithosClientError) as exc:
        await client.note_delete(id="doc-1")
    assert exc.value.code == "invalid_response"


async def test_note_delete_raises_on_non_dict_payload() -> None:
    """A non-dict response (string, list, etc.) is server-side
    drift — refuse to interpret it as success."""
    # Build the CallToolResult directly here — the shared `_content`
    # helper is typed to `dict` (which is what every other test
    # needs); the non-dict shape is the regression we're pinning.
    non_dict_result = CallToolResult(
        content=[TextContent(type="text", text=json.dumps("ok"))]
    )
    client, _ = _client_with_session(non_dict_result)
    with pytest.raises(LithosClientError) as exc:
        await client.note_delete(id="doc-1")
    assert exc.value.code == "invalid_response"


async def test_note_delete_requires_agent_id() -> None:
    """Lithos requires ``agent`` for audit-trail purposes. Without
    an explicit kwarg AND no ``agent_id`` on the client, raise a
    typed error rather than letting Lithos respond with a pydantic
    "missing_argument" message that the operator has to debug.
    Soak regression: this exact failure mode bit the user when they
    were using ``_call`` directly without the wrapper."""
    client = LithosClient(base_url="http://example.test:8765")  # no agent_id
    fake_session = AsyncMock()
    client._session = fake_session  # type: ignore[assignment]
    with pytest.raises(LithosClientError, match="agent"):
        await client.note_delete(id="doc-1")
    fake_session.call_tool.assert_not_awaited()


def test_write_result_default_warnings_is_empty_tuple() -> None:
    """Regression: ``warnings`` default must be the canonical empty
    tuple (not None, not a fresh list each construction) so handler
    code can ``if not wr.warnings:`` test cleanly."""
    wr = WriteResult(status="created")
    assert wr.warnings == ()


# ── Dead-session recovery (#43) ────────────────────────────────────────


class _DeadError(Exception):
    """Stand-in for the transport/protocol error the MCP SDK raises when
    the SSE stream is dead (the exact type is version-dependent; _invoke
    catches broadly)."""


@pytest.fixture(autouse=True)
def _zero_reconnect_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the reconnect tests snappy — no real backoff sleep."""
    monkeypatch.setattr(lithos_client_mod, "_RECONNECT_BACKOFF_SECONDS", 0)


def _live_session(payload: dict[str, Any]) -> AsyncMock:
    session = AsyncMock()
    session.call_tool.return_value = _content(payload)
    return session


def _dead_session() -> AsyncMock:
    session = AsyncMock()
    session.call_tool.side_effect = _DeadError("still dead")
    return session


def _patch_establish(client: LithosClient, sessions: list[AsyncMock]) -> list[int]:
    """Replace ``client._establish`` so each reconnect installs the next
    queued session. Returns a list that records each establish call."""
    calls: list[int] = []

    async def _establish() -> None:
        calls.append(1)
        client._session = sessions.pop(0)  # type: ignore[assignment]

    client._establish = _establish  # type: ignore[method-assign]
    return calls


async def test_invoke_reconnects_then_succeeds_through_bypass_method() -> None:
    """A dead session on a method that bypasses _call (note_read) is
    transparently re-established and the call retried."""
    client = LithosClient(base_url="http://example.test:8765")
    client._session = _dead_session()  # type: ignore[assignment]
    fresh = _live_session(
        {
            "id": "d1",
            "title": "Doc One",
            "content": "# Doc One\n\nBody.",
            "path": "projects/x/x.md",
            "metadata": {"version": 3},
        }
    )
    establish_calls = _patch_establish(client, [fresh])

    note = await client.note_read(id="d1")

    assert note is not None and note.id == "d1"
    assert len(establish_calls) == 1  # reconnected exactly once
    fresh.call_tool.assert_awaited_once()  # retried on the fresh session


async def test_invoke_reraises_after_exhausting_attempts() -> None:
    """Persistent transport failure → the last error propagates after
    _MAX_TRANSPORT_ATTEMPTS, no infinite loop."""
    client = LithosClient(base_url="http://example.test:8765")
    client._session = _dead_session()  # type: ignore[assignment]
    # Every reconnect installs another dead session.
    establish_calls = _patch_establish(client, [_dead_session() for _ in range(5)])

    with pytest.raises(_DeadError, match="still dead"):
        await client.task_list()

    # 3 attempts total → 2 reconnects between them.
    assert len(establish_calls) == 2


async def test_invoke_single_flight_reconnect_under_concurrency() -> None:
    """Two concurrent calls that BOTH fail mid-flight against the same dead
    session re-establish only once (lock + generation guard), then both
    succeed on the fresh session."""
    client = LithosClient(base_url="http://example.test:8765")

    dead = AsyncMock()

    async def _yield_then_die(*_a: Any, **_k: Any) -> Any:
        # Yield so both gathered tasks enter call_tool and are in-flight
        # before either raises — this exercises the genuine concurrent
        # failure path (both capture the same generation, then race to
        # reconnect), which a synchronously-raising mock would not.
        await asyncio.sleep(0)
        raise _DeadError("dead")

    dead.call_tool.side_effect = _yield_then_die
    client._session = dead  # type: ignore[assignment]
    fresh = _live_session({"tasks": []})
    # Only one fresh session is queued — if reconnect fired twice the
    # second establish would IndexError on the empty list.
    establish_calls = _patch_establish(client, [fresh])

    results = await asyncio.gather(client.task_list(), client.task_list())

    assert results == [[], []]
    assert len(establish_calls) == 1


async def test_aexit_closes_contexts_after_reconnect() -> None:
    """``__aexit__`` closes whatever session/SSE contexts are current —
    including ones installed by a reconnect — and clears the handles."""
    client = LithosClient(base_url="http://example.test:8765")
    session_ctx = AsyncMock()
    sse_ctx = AsyncMock()
    # Simulate post-reconnect state (a fresh _establish would have set these).
    client._session_ctx = session_ctx
    client._sse_ctx = sse_ctx
    client._session = AsyncMock()  # type: ignore[assignment]

    await client.__aexit__(None, None, None)

    session_ctx.__aexit__.assert_awaited_once()
    sse_ctx.__aexit__.assert_awaited_once()
    assert client._session is None
    assert client._session_ctx is None
    assert client._sse_ctx is None


async def test_invoke_does_not_swallow_cancellation() -> None:
    """A cancelled call_tool propagates without attempting a reconnect."""
    client = LithosClient(base_url="http://example.test:8765")
    cancelling = AsyncMock()
    cancelling.call_tool.side_effect = asyncio.CancelledError()
    client._session = cancelling  # type: ignore[assignment]
    establish_calls = _patch_establish(client, [])

    with pytest.raises(asyncio.CancelledError):
        await client.task_list()

    assert establish_calls == []  # no reconnect on cancellation


async def test_invoke_raises_when_never_initialised() -> None:
    """Calling before ``async with`` (no session) raises the
    client_not_initialised envelope, not a reconnect attempt."""
    client = LithosClient(base_url="http://example.test:8765")
    with pytest.raises(LithosClientError) as ei:
        await client.task_list()
    assert ei.value.code == "client_not_initialised"


async def test_invoke_recovers_when_call_tool_raises_exception_group() -> None:
    """Soak 2026-05-28: the MCP SDK's anyio internals wrap SSE-stream-closed
    failures in an ``ExceptionGroup`` (or ``BaseExceptionGroup``). Without
    special handling, a bare ``except Exception`` would let the group escape
    (BaseExceptionGroup is a BaseException), killing the daemon child. The
    transport-failure catch must recover from grouped errors just like bare
    ones."""
    client = LithosClient(base_url="http://example.test:8765")
    dead = AsyncMock()
    dead.call_tool.side_effect = ExceptionGroup(
        "anyio task group", [_DeadError("sse stream closed")]
    )
    client._session = dead  # type: ignore[assignment]
    fresh = _live_session({"tasks": []})
    establish_calls = _patch_establish(client, [fresh])

    tasks = await client.task_list()

    assert tasks == []
    assert len(establish_calls) == 1  # reconnected once, then retried


async def test_invoke_propagates_cancellation_nested_in_exception_group() -> None:
    """A ``BaseExceptionGroup`` containing a ``CancelledError`` must propagate
    the cancellation (don't swallow it as a 'transport failure')."""
    client = LithosClient(base_url="http://example.test:8765")
    mixed = AsyncMock()
    mixed.call_tool.side_effect = BaseExceptionGroup(
        "shutdown", [asyncio.CancelledError(), _DeadError("partial")]
    )
    client._session = mixed  # type: ignore[assignment]
    establish_calls = _patch_establish(client, [])

    with pytest.raises(BaseExceptionGroup):
        await client.task_list()

    assert establish_calls == []  # no reconnect — cancellation propagates


async def test_reconnect_inline_does_not_aexit_dead_contexts() -> None:
    """Soak 2026-05-29 (a): the test-bypass inline reconnect path must
    NOT call ``__aexit__`` on dead MCP contexts. In production this would
    raise ``RuntimeError: Attempted to exit cancel scope in a different
    task ...`` because the scopes were opened in a different task. The
    keeper (production) handles teardown in the right task; the inline
    path is for tests only and just drops refs to mirror the safe shape.
    """
    client = LithosClient(base_url="http://example.test:8765")
    session_ctx = AsyncMock()
    sse_ctx = AsyncMock()
    client._session_ctx = session_ctx
    client._sse_ctx = sse_ctx
    client._session = AsyncMock()  # type: ignore[assignment]
    # Patch _establish so the inline path completes (it gets a fresh session).
    fresh = _live_session({"tasks": []})
    _patch_establish(client, [fresh])

    await client._reconnect_inline(expected_gen=0)

    session_ctx.__aexit__.assert_not_awaited()
    sse_ctx.__aexit__.assert_not_awaited()
    assert client._session is fresh


async def test_keeper_lifecycle_open_call_close() -> None:
    """Production path: ``async with`` spawns the keeper, ``_invoke`` calls
    against the keeper-owned session, ``__aexit__`` shuts the keeper down
    cleanly. Stand-in for the soak scenario — we mock the SDK rather than
    talk to a real Lithos, but the keeper task and queue plumbing are real.
    """
    client = LithosClient(base_url="http://example.test:8765")
    session_ctx = AsyncMock()
    sse_ctx = AsyncMock()
    fresh = _live_session({"tasks": []})

    async def fake_establish() -> None:
        client._session_ctx = session_ctx
        client._sse_ctx = sse_ctx
        client._session = fresh

    client._establish = fake_establish  # type: ignore[method-assign]

    async with client:
        assert client._keeper_task is not None
        assert not client._keeper_task.done()
        result = await client.task_list()
        assert result == []

    # After shutdown the keeper exited cleanly and tore down the session
    # contexts in its own task (where the scopes were opened).
    assert client._keeper_task is None
    session_ctx.__aexit__.assert_awaited_once_with(None, None, None)
    sse_ctx.__aexit__.assert_awaited_once_with(None, None, None)


async def test_keeper_handles_reconnect_request_in_keeper_task() -> None:
    """When ``_invoke`` fails, it queues a reconnect request and waits for
    the keeper to tear down + re-establish in the keeper's own task —
    the only task that can safely exit the SDK's anyio scopes.
    """
    client = LithosClient(base_url="http://example.test:8765")
    # Three sessions: initial (dead), reconnect target (live).
    dead = _dead_session()
    fresh = _live_session({"tasks": []})
    sessions = [dead, fresh]
    teardown_calls: list[asyncio.Task[object] | None] = []
    establish_tasks: list[asyncio.Task[object] | None] = []

    async def fake_establish() -> None:
        establish_tasks.append(asyncio.current_task())
        client._session = sessions.pop(0)

    async def fake_teardown() -> None:
        teardown_calls.append(asyncio.current_task())
        client._session = None

    client._establish = fake_establish  # type: ignore[method-assign]
    client._teardown_in_keeper = fake_teardown  # type: ignore[method-assign]

    async with client:
        # First call against `dead` fails → keeper reconnects to `fresh`.
        result = await client.task_list()
        assert result == []

        keeper_task = client._keeper_task
        # All teardown + establish work happened in the keeper task.
        assert all(t is keeper_task for t in teardown_calls)
        assert all(t is keeper_task for t in establish_tasks)
        # Two establishes: initial + one reconnect.
        assert len(establish_tasks) == 2
        # One teardown: between the two establishes (initial connect has
        # no prior session to tear down).
        assert len(teardown_calls) == 1


async def test_request_reconnect_fails_loudly_when_keeper_dead_in_production() -> None:
    """After ``__aenter__``, if the keeper task has exited unexpectedly, a
    new reconnect request must fail with ``session_unavailable`` rather
    than silently falling back to the unsafe inline path (which would
    re-introduce the 2026-05-29 RuntimeError in production)."""
    client = LithosClient(base_url="http://example.test:8765")
    fresh = _live_session({"tasks": []})

    async def fake_establish() -> None:
        client._session = fresh

    client._establish = fake_establish  # type: ignore[method-assign]

    async with client:
        # Forcibly mark the keeper as done (simulating an unexpected exit
        # without going through the queue).
        keeper = client._keeper_task
        assert keeper is not None
        keeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keeper

        with pytest.raises(LithosClientError) as ei:
            await client._request_reconnect(expected_gen=client._session_generation)
        assert ei.value.code == "session_unavailable"
