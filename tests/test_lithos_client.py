"""Tests for ``lithos_loom.lithos_client`` (Slice 0 US3).

The slice-0 surface is intentionally narrow: only ``task_list`` plus the
envelope-decoding helpers. The MCP-over-SSE transport is exercised through
``LithosClient`` itself, but the wire-format unit tests target the pure
parse helpers so we don't have to spin up a real Lithos to verify shape.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import (
    LithosClient,
    Task,
    _parse_task_list_response,
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
