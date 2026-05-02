"""Smoke tests for the Typer CLI dispatcher."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

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
