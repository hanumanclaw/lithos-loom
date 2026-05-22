"""Tests for ``lithos_loom.subscriptions._obsidian_status_transition``
(Slice 2 US17).

The handler is stateless; tests just call ``handle(event, ctx)``
directly with synthetic events and assert on a mocked
``ctx.lithos`` (``AsyncMock``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._obsidian_status_transition import handle

# ── Helpers ────────────────────────────────────────────────────────────


def _ctx(
    lithos: Any | None = None,
    agent_id: str = "lithos-orchestrator-test",
) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos if lithos is not None else AsyncMock(),
        logger=logging.getLogger("test.obsidian_status_transition"),
        agent_id=agent_id,
    )


def _event(
    *,
    task_id: str = "abc",
    prior: str = "[ ]",
    new: str = "[x]",
    event_type: str = "obsidian.task.status_changed",
) -> Event:
    return Event(
        type=event_type,
        timestamp=datetime.now(UTC),
        payload={"task_id": task_id, "prior": prior, "new": new},
    )


# ── US17: [ ] → [x] → task_complete ────────────────────────────────────


async def test_open_to_done_calls_task_complete() -> None:
    """``[ ]`` → ``[x]`` for a known task → ``lithos.task_complete`` called
    with the task id and the context's agent id."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, agent_id="lithos-orchestrator-samsara")

    await handle(_event(task_id="abc", prior="[ ]", new="[x]"), ctx)

    lithos.task_complete.assert_awaited_once_with(
        task_id="abc", agent="lithos-orchestrator-samsara"
    )


async def test_open_to_done_logs_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The happy path emits an INFO log naming the task id so operators
    have a grep-able trail."""
    ctx = _ctx()
    with caplog.at_level(logging.INFO, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="abc123"), ctx)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("completed task abc123 via Obsidian tick" in m for m in info_msgs), (
        info_msgs
    )


# ── US20 side-effect: other transitions are silent no-ops ──────────────


@pytest.mark.parametrize(
    ("prior", "new"),
    [
        ("[ ]", "[-]"),  # cancel — US18 future
        ("[x]", "[ ]"),  # untick → reopen — US19 future
        ("[-]", "[ ]"),  # un-cancel — US19 family
        ("[ ]", "[/]"),  # in-progress — US20 no-op
        ("[ ]", "[>]"),  # rescheduled — US20 no-op
        ("[/]", "[>]"),  # arbitrary user marker transition
        ("[x]", "[-]"),  # done → cancelled (weird but possible)
        ("[ ]", "[ ]"),  # same-marker (won't actually fire from source, but safe)
    ],
)
async def test_other_transitions_are_silent_no_ops(prior: str, new: str) -> None:
    """Every transition not in the dispatch table must NOT call Lithos.

    Folds in US20's no-op-for-`[/]`/`[>]` requirement as a free side
    effect of the dispatch-table design — the formal US20 PR will
    just confirm this behaviour stays correct.
    """
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)

    await handle(_event(prior=prior, new=new), ctx)

    lithos.task_complete.assert_not_awaited()
    lithos.task_cancel.assert_not_awaited()
    lithos.finding_post.assert_not_awaited()


async def test_unknown_transition_logged_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Skipped transitions leave a DEBUG breadcrumb so operators can
    enable verbose logging to see what's flowing through."""
    ctx = _ctx()
    with caplog.at_level(logging.DEBUG, logger="test.obsidian_status_transition"):
        await handle(_event(task_id="xyz", prior="[ ]", new="[/]"), ctx)

    debug_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(
        "no handler for transition [ ]→[/] on task xyz" in m for m in debug_msgs
    ), debug_msgs


# ── Robustness ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_payload",
    [
        {},  # all keys missing
        {"task_id": "x"},  # missing prior + new
        {"prior": "[ ]", "new": "[x]"},  # missing task_id
        {"task_id": "x", "prior": "[ ]"},  # missing new
        {"task_id": "x", "new": "[x]"},  # missing prior
    ],
)
async def test_malformed_payload_warns_and_returns(
    bad_payload: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Missing payload keys → handler logs a warning, makes no Lithos
    calls, doesn't raise. Matches the silent-degradation contract the
    rest of the subscription layer follows for malformed bus events."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos)
    event = Event(
        type="obsidian.task.status_changed",
        timestamp=datetime.now(UTC),
        payload=bad_payload,
    )

    with caplog.at_level(logging.WARNING, logger="test.obsidian_status_transition"):
        await handle(event, ctx)  # must not raise

    lithos.task_complete.assert_not_awaited()
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("malformed payload" in m for m in warn_msgs), warn_msgs


async def test_lithos_error_propagates() -> None:
    """A ``LithosClientError`` (or any exception) from ``task_complete``
    must bubble up so the :class:`SubscriptionRunner` retry-with-backoff
    + on_persistent_failure=friction backstop can take over."""
    lithos = AsyncMock()
    lithos.task_complete.side_effect = RuntimeError("simulated lithos error")
    ctx = _ctx(lithos=lithos)

    with pytest.raises(RuntimeError, match="simulated lithos error"):
        await handle(_event(prior="[ ]", new="[x]"), ctx)


async def test_handler_uses_ctx_agent_id_not_hardcoded() -> None:
    """The agent passed to ``task_complete`` comes from ``ctx.agent_id``,
    not a hardcoded string — different deployments (samsara, mac-mini,
    test) must each pass their own identity through unchanged."""
    lithos = AsyncMock()
    ctx = _ctx(lithos=lithos, agent_id="lithos-orchestrator-mac-mini")

    await handle(_event(), ctx)

    lithos.task_complete.assert_awaited_once()
    assert lithos.task_complete.await_args.kwargs["agent"] == (
        "lithos-orchestrator-mac-mini"
    )
