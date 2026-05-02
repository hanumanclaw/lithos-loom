"""Shared pytest fixtures for lithos-loom."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest


@pytest.fixture(autouse=True)
def clean_loom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ``LITHOS_*`` env vars so a developer's shell cannot leak into tests.

    Tests that need a specific env should set vars explicitly via
    ``monkeypatch`` inside the test body.
    """
    for var in (
        "LITHOS_URL",
        "LITHOS_LOOM_CONFIG",
        "LITHOS_LOOM_ENVIRONMENT",
    ):
        monkeypatch.delenv(var, raising=False)


_FIXTURE_ROUTE_CMD = (
    "uv run python -m lithos_loom.plugins.prd_decompose "
    "--task-json {{task_json}} "
    "--work-dir {{work_dir}} "
    "--result-file {{result_file}}"
)


@pytest.fixture
def loom_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a minimal ``config.toml`` and point ``LITHOS_LOOM_CONFIG`` at it."""
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            f"""
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"
            poll_interval_seconds = 30
            work_dir = "{tmp_path / "work"}"
            max_concurrency = 2
            log_level = "info"

            [projects.lithos-lens]
            repo = "{repo}"

            [[routes]]
            name = "prd-decompose"
            command = "{_FIXTURE_ROUTE_CMD}"
            [routes.match]
            tags = ["trigger:prd-decompose"]
            """
        )
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(config_path))
    return config_path
