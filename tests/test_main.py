"""Smoke tests for the Typer CLI dispatcher."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from textwrap import dedent
from typing import Any
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from lithos_loom import main as main_module
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import Task
from lithos_loom.main import app

runner = CliRunner()


def test_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for sub in ("run", "doctor", "validate-config", "config"):
        assert sub in result.stdout


def test_validate_config_succeeds(loom_config_env: Path) -> None:
    result = runner.invoke(app, ["validate-config"])
    assert result.exit_code == 0
    assert "lithos-orchestrator-test" in result.stdout
    assert "prd-decompose" in result.stdout


def test_validate_config_fails_clearly_when_missing(tmp_path: Path) -> None:
    """A bogus config path must exit non-zero with a useful message."""
    result = runner.invoke(
        app, ["validate-config", "--config", str(tmp_path / "nope.toml")]
    )
    assert result.exit_code != 0


# ── validate-config --dry-run ──────────────────────────────────────────


def _task(
    id_: str,
    *,
    tags: tuple[str, ...] = (),
    status: str = "open",
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Task:
    return Task(
        id=id_,
        title=title or f"Task {id_}",
        status=status,
        tags=tags,
        metadata=metadata or {},
        claims=(),
    )


class _FakeLithos:
    """Async-context-manager mock that records every call on it.

    Read-only methods (``task_list``, ``task_status``) are explicit.
    Anything else routed through ``__getattr__`` is recorded under
    ``mutating_calls`` so tests can assert dry-run stays non-mutating.
    """

    def __init__(
        self,
        tasks: Sequence[Task],
        *,
        dep_statuses: dict[str, str | None] | None = None,
    ) -> None:
        self._tasks = list(tasks)
        self._dep_statuses = dict(dep_statuses or {})
        self.task_list_calls: list[dict[str, Any]] = []
        self.task_status_calls: list[str] = []
        self.mutating_calls: list[str] = []

    async def __aenter__(self) -> _FakeLithos:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def task_list(
        self,
        *,
        status: str | None = None,
        with_claims: bool = False,
    ) -> list[Task]:
        self.task_list_calls.append({"status": status, "with_claims": with_claims})
        return list(self._tasks)

    async def task_status(self, *, task_id: str) -> Task | None:
        self.task_status_calls.append(task_id)
        if task_id not in self._dep_statuses:
            return None  # task_not_found
        return Task(
            id=task_id,
            title=f"dep {task_id}",
            status=self._dep_statuses[task_id] or "open",
            tags=(),
            metadata={},
            claims=(),
        )

    def __getattr__(self, name: str) -> Any:
        # Any other call is recorded so tests can assert non-mutation.
        if name.startswith("task_") or name.startswith("finding_"):
            self.mutating_calls.append(name)
            return AsyncMock()
        raise AttributeError(name)


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeLithos) -> None:
    """Patch the LithosClient symbol the CLI imports with a factory."""

    def factory(*args: object, **kwargs: object) -> _FakeLithos:
        return fake

    monkeypatch.setattr(main_module, "LithosClient", factory)


def test_dry_run_lists_matched_routes_per_task(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task whose tags match a configured route is reported as 'would fire'."""
    fake = _FakeLithos(
        tasks=[_task("abc123", tags=("trigger:prd-decompose",), title="Decompose me")]
    )
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "abc123" in result.output
    assert "route:prd-decompose" in result.output
    assert fake.task_list_calls == [{"status": "open", "with_claims": True}]


def test_dry_run_flags_orphan_tasks(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open task with no matching route or subscription is listed as orphan."""
    fake = _FakeLithos(
        tasks=[_task("orph-1", tags=("unrouted",), title="Nobody wants me")]
    )
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "orph-1" in result.output
    assert "orphan" in result.output.lower()


def test_dry_run_flags_dead_routes(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured route that matches no open task is flagged as dead config."""
    fake = _FakeLithos(tasks=[])  # no open tasks → every route is dead
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "dead" in result.output.lower()
    assert "prd-decompose" in result.output


def test_dry_run_does_not_call_mutating_lithos_methods(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run is non-mutating: no claim, complete, update, release, finding_post."""
    fake = _FakeLithos(tasks=[_task("abc123", tags=("trigger:prd-decompose",))])
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    forbidden = {
        "task_claim",
        "task_release",
        "task_renew",
        "task_complete",
        "task_update",
        "finding_post",
    }
    assert not (set(fake.mutating_calls) & forbidden), fake.mutating_calls


def test_dry_run_clear_error_when_lithos_unreachable(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the Lithos session cannot be opened, fail with a clear message that
    points the operator at ``lithos-loom doctor`` for follow-up.
    """

    class _UnreachableClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def __aenter__(self) -> _UnreachableClient:
            raise OSError("connection refused")

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(main_module, "LithosClient", _UnreachableClient)

    result = runner.invoke(app, ["validate-config", "--dry-run"])
    assert result.exit_code != 0
    assert "doctor" in result.output.lower() or "doctor" in (
        result.stderr if result.stderr else ""
    )


def test_dry_run_matches_subscription_with_where_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscriptions with a where expression are evaluated during dry-run.

    Pins that the dry-run uses the same matcher machinery the bus uses at
    runtime, so the table reflects what would actually fire.
    """
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            [[subscriptions]]
            name = "high-priority-only"
            on = "lithos.task.created"
            action = "noop"
            where = "task.get('title') == 'urgent'"
            """
        )
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    fake = _FakeLithos(
        tasks=[
            _task("hi", title="urgent"),
            _task("lo", title="meh"),
        ]
    )
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    # The where predicate fires for "hi" but not for "lo".
    lines = result.output.splitlines()
    high_lines = [
        line for line in lines if "hi" in line and "high-priority-only" in line
    ]
    low_lines = [
        line for line in lines if "lo" in line and "high-priority-only" in line
    ]
    assert any("would fire" in line.lower() or "✓" in line for line in high_lines)
    # "lo" appears in the orphan list, but should NOT show a "would fire"
    # against the where-gated subscription.
    for line in low_lines:
        assert "would fire" not in line.lower() and "✓" not in line


def test_dry_run_subscription_with_updated_event_type_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subscription with on='lithos.task.updated' must show as 'would fire'
    when its filter matches the task — the dry-run must test the sub
    against every type in its on-list, not hard-code lithos.task.created.
    """
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            [[subscriptions]]
            name = "updated-only"
            on = "lithos.task.updated"
            action = "noop"
            match.tags = ["any-tag"]
            """
        )
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    fake = _FakeLithos(tasks=[_task("t1", tags=("any-tag",))])
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    sub_lines = [
        line
        for line in result.output.splitlines()
        if "subscription:updated-only" in line
    ]
    assert sub_lines, result.output
    assert any("would fire" in line.lower() or "✓" in line for line in sub_lines)


def test_dry_run_route_deferred_when_dependencies_not_completed(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tag-matching open task whose depends_on includes an unfinished dep
    must NOT be reported as 'would fire (claim)'. The runner's actual gate
    defers it; the dry-run output must reflect that.
    """
    fake = _FakeLithos(
        tasks=[
            _task(
                "blocked",
                tags=("trigger:prd-decompose",),
                metadata={"depends_on": ["dep-1"]},
            )
        ],
        dep_statuses={"dep-1": "open"},  # dep is still open → not satisfied
    )
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    # Find the row under the "blocked" task heading.
    lines = result.output.splitlines()
    blocked_idx = next(i for i, line in enumerate(lines) if "blocked" in line)
    # Subsequent lines indent under it; find the prd-decompose route row.
    route_row = next(
        line
        for line in lines[blocked_idx + 1 : blocked_idx + 5]
        if "route:prd-decompose" in line
    )
    assert "✓" not in route_row, route_row
    assert (
        "deferred" in route_row.lower() or "deps not complete" in route_row.lower()
    ), route_row
    # Dep-1 must have been resolved via task_status.
    assert "dep-1" in fake.task_status_calls


def test_dry_run_route_fires_when_dependencies_completed(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dep-gating doesn't over-correct: completed deps allow the route to fire."""
    fake = _FakeLithos(
        tasks=[
            _task(
                "ready",
                tags=("trigger:prd-decompose",),
                metadata={"depends_on": ["dep-1"]},
            )
        ],
        dep_statuses={"dep-1": "completed"},
    )
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    idx = next(i for i, line in enumerate(lines) if "ready" in line)
    route_row = next(
        line for line in lines[idx + 1 : idx + 5] if "route:prd-decompose" in line
    )
    assert "✓" in route_row, route_row


# Sentinel that keeps LithosClientError importable in this module so tests
# referencing it don't lose to import pruning even when the symbol isn't
# actively used.
_ = LithosClientError
