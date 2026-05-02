"""Tests for the TOML config loader."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from lithos_loom.config import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_POLL_INTERVAL_SECONDS,
    LoomConfig,
    find_config_path,
    load_config,
)
from lithos_loom.errors import ConfigError


def test_load_config_parses_orchestrator_projects_routes(loom_config_env: Path) -> None:
    cfg = load_config()
    assert isinstance(cfg, LoomConfig)
    assert cfg.orchestrator.agent_id == "lithos-orchestrator-test"
    assert cfg.orchestrator.lithos_url == "http://localhost:8765"
    assert cfg.orchestrator.poll_interval_seconds == 30
    assert cfg.orchestrator.max_concurrency == 2
    assert "lithos-lens" in cfg.projects
    assert len(cfg.routes) == 1
    assert cfg.routes[0].name == "prd-decompose"
    assert cfg.routes[0].match.tags == ("trigger:prd-decompose",)


def test_lithos_url_env_override_wins(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_URL", "http://override:9999")
    cfg = load_config()
    assert cfg.orchestrator.lithos_url == "http://override:9999"


def test_missing_config_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigError, match="LITHOS_LOOM_CONFIG points at"):
        find_config_path()


def test_environment_picks_per_env_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LITHOS_LOOM_ENVIRONMENT=prod selects config.prod.toml from the search dirs."""
    cfg_dir = tmp_path / "lithos-loom"
    cfg_dir.mkdir()
    (cfg_dir / "config.prod.toml").write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "lithos-orchestrator-prod"
            lithos_url = "http://prod:8765"
            """
        )
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("LITHOS_LOOM_ENVIRONMENT", "prod")
    monkeypatch.delenv("LITHOS_LOOM_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # so cwd doesn't accidentally hit a config.toml

    cfg = load_config()
    assert cfg.orchestrator.agent_id == "lithos-orchestrator-prod"
    assert cfg.environment == "prod"
    assert cfg.orchestrator.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS
    assert cfg.orchestrator.max_concurrency == DEFAULT_MAX_CONCURRENCY


def test_invalid_toml_surfaces_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text("not = valid = toml")
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(bad))
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config()
