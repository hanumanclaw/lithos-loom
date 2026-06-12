"""Tests for the story-develop Lithos round-trip (T8).

The MCP client is faked at the module seam (``lithos_io.LithosClient``) — no
server needed. The fake records calls so posting behaviour is assertable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lithos_loom.lithos_client import Task
from lithos_loom.plugins.story_develop import lithos_io
from lithos_loom.plugins.story_develop.develop import DevelopResult, ReviewOutcome
from lithos_loom.plugins.story_develop.handoff import Finding


def _task(**overrides) -> Task:
    base: dict[str, Any] = dict(
        id="task-1",
        title="Add a flag",
        status="open",
        tags=(),
        metadata={},
        claims=(),
        description="Body text.",
    )
    base.update(overrides)
    return Task(**base)


class _FakeClient:
    """Stands in for LithosClient: async context manager + recorded calls."""

    task: Task | None = None
    raise_on_post: bool = False
    findings: list[str] = []
    metadata_updates: list[dict] = []

    def __init__(self, url: str, *, agent_id: str | None = None) -> None:
        self.url = url
        self.agent_id = agent_id

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def task_get(self, *, task_id: str) -> Task | None:
        return type(self).task

    async def finding_post(self, *, task_id: str, summary: str, **kw) -> str:
        if type(self).raise_on_post:
            raise RuntimeError("lithos down")
        type(self).findings.append(summary)
        return "finding-1"

    async def task_update(self, *, task_id: str, metadata=None, **kw) -> None:
        if type(self).raise_on_post:
            raise RuntimeError("lithos down")
        type(self).metadata_updates.append(dict(metadata or {}))


@pytest.fixture(autouse=True)
def fake_client(monkeypatch: pytest.MonkeyPatch):
    _FakeClient.task = _task()
    _FakeClient.raise_on_post = False
    _FakeClient.findings = []
    _FakeClient.metadata_updates = []
    monkeypatch.setattr(lithos_io, "LithosClient", _FakeClient)
    return _FakeClient


def _result(status: str = "approved", **overrides) -> DevelopResult:
    base: dict[str, Any] = dict(
        status=status,
        run_id="abcd1234",
        worktree=Path("/tmp/wt"),
        branch="my-branch",
        base_sha="0" * 40,
        commits=["a" * 40],
        rounds=2,
        handoff_present=True,
        coder_cost_usd=0.5,
        review_cost_usd=0.25,
        message="approved by [cq]=LGTM(pass) in 2 round(s)",
        reviews=(
            ReviewOutcome(
                reviewer="cq",
                status="FINDINGS",
                passed=True,
                max_severity=None,
                findings=[
                    Finding(
                        finding_id="f-001",
                        severity="minor",
                        status="open",
                        rationale="tighten the type",
                    ),
                    Finding(finding_id="f-002", severity="major", status="fixed"),
                ],
            ),
        ),
        conversation_log=Path("/tmp/run/conversation.md"),
    )
    base.update(overrides)
    return DevelopResult(**base)


# --- fetch_task_context -------------------------------------------------------


def test_fetch_builds_context(fake_client) -> None:
    ctx = lithos_io.fetch_task_context("http://x", "task-1")
    assert ctx.task_id == "task-1"
    assert ctx.title == "Add a flag"
    assert ctx.task_text == "Add a flag\n\nBody text."
    assert ctx.acceptance_criteria is None


def test_fetch_reads_acceptance_criteria_metadata(fake_client) -> None:
    fake_client.task = _task(metadata={"acceptance_criteria": "must have tests"})
    ctx = lithos_io.fetch_task_context("http://x", "task-1")
    assert ctx.acceptance_criteria == "must have tests"


def test_fetch_ignores_blank_acceptance_criteria(fake_client) -> None:
    fake_client.task = _task(metadata={"acceptance_criteria": "   "})
    ctx = lithos_io.fetch_task_context("http://x", "task-1")
    assert ctx.acceptance_criteria is None


def test_fetch_task_text_without_body(fake_client) -> None:
    fake_client.task = _task(description=None)
    assert lithos_io.fetch_task_context("http://x", "task-1").task_text == "Add a flag"


def test_fetch_not_found_raises(fake_client) -> None:
    fake_client.task = None
    with pytest.raises(lithos_io.LithosIOError, match="not found"):
        lithos_io.fetch_task_context("http://x", "task-1")


def test_fetch_terminal_task_refused(fake_client) -> None:
    fake_client.task = _task(status="completed")
    with pytest.raises(lithos_io.LithosIOError, match="terminal"):
        lithos_io.fetch_task_context("http://x", "task-1")


# --- post_results --------------------------------------------------------------


def test_post_results_finding_and_metadata(fake_client) -> None:
    ok = lithos_io.post_results("http://x", "task-1", _result())
    assert ok is True
    assert len(fake_client.findings) == 1
    body = fake_client.findings[0]
    assert body.startswith("[DevelopResult] APPROVED:")
    assert "branch: my-branch" in body
    assert "[cq/f-001] minor (open): tighten the type" in body  # open survives
    assert "f-002" not in body  # resolved findings are not re-listed
    (meta,) = fake_client.metadata_updates
    assert meta["develop_status"] == "approved"
    assert meta["develop_branch"] == "my-branch"
    assert meta["develop_cost_usd"] == 0.75


def test_post_results_disputed_adds_breadcrumb(fake_client) -> None:
    lithos_io.post_results("http://x", "task-1", _result(status="disputed"))
    assert len(fake_client.findings) == 2
    assert fake_client.findings[1].startswith("[ReviewDispute]")
    assert "human" in fake_client.findings[1]


def test_post_failure_returns_false_not_raise(fake_client) -> None:
    fake_client.raise_on_post = True
    assert lithos_io.post_results("http://x", "task-1", _result()) is False


def test_complete_task_calls_client(fake_client, monkeypatch) -> None:
    completed: list[str] = []

    async def fake_complete(self, *, task_id):
        completed.append(task_id)

    monkeypatch.setattr(_FakeClient, "task_complete", fake_complete, raising=False)
    assert lithos_io.complete_task("http://x", "task-1", _result()) is True
    assert completed == ["task-1"]


def test_complete_task_failure_returns_false(fake_client, monkeypatch) -> None:
    async def boom(self, *, task_id):
        raise RuntimeError("down")

    monkeypatch.setattr(_FakeClient, "task_complete", boom, raising=False)
    assert lithos_io.complete_task("http://x", "task-1", _result()) is False
