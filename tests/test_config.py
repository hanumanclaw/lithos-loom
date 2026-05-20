"""Tests for the TOML config loader."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from lithos_loom.config import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_OBSIDIAN_RESOLVED_TTL_DAYS,
    DEFAULT_OBSIDIAN_TASKS_FILE,
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
    assert cfg.orchestrator.max_concurrency == DEFAULT_MAX_CONCURRENCY


def test_invalid_toml_surfaces_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text("not = valid = toml")
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(bad))
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config()


# ── [obsidian_sync] section (Slice 1 US7) ──────────────────────────────


_MINIMAL_ORCHESTRATOR_TOML = dedent(
    """
    [orchestrator]
    agent_id = "lithos-orchestrator-test"
    lithos_url = "http://localhost:8765"
    """
)


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_MINIMAL_ORCHESTRATOR_TOML + body)
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    return cfg_path


def test_obsidian_sync_absent_yields_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No [obsidian_sync] section → cfg.obsidian_sync is None.

    This is the supervisor's spawn gate: the obsidian-sync child is
    only forked when the section is present.
    """
    _write_config(tmp_path, monkeypatch, "")
    cfg = load_config()
    assert cfg.obsidian_sync is None


def test_obsidian_sync_minimal_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vault_path alone is sufficient; other fields use documented defaults."""
    vault = tmp_path / "vault"
    _write_config(
        tmp_path,
        monkeypatch,
        dedent(
            f"""
            [obsidian_sync]
            vault_path = "{vault}"
            """
        ),
    )
    cfg = load_config()
    assert cfg.obsidian_sync is not None
    assert cfg.obsidian_sync.vault_path == vault
    assert cfg.obsidian_sync.tasks_file == DEFAULT_OBSIDIAN_TASKS_FILE
    assert cfg.obsidian_sync.resolved_ttl_days == DEFAULT_OBSIDIAN_RESOLVED_TTL_DAYS
    # D6 revised default: blocked tasks project.
    assert cfg.obsidian_sync.include_blocked is True
    assert cfg.obsidian_sync.exclude_tags == ()


def test_obsidian_sync_full_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All fields override their defaults, including the projection filter knobs."""
    vault = tmp_path / "vault"
    _write_config(
        tmp_path,
        monkeypatch,
        dedent(
            f"""
            [obsidian_sync]
            vault_path = "{vault}"
            tasks_file = "loom/inbox.md"
            resolved_ttl_days = 14
            include_blocked = false
            exclude_tags = ["debug:trace", "internal"]
            """
        ),
    )
    cfg = load_config()
    assert cfg.obsidian_sync is not None
    assert cfg.obsidian_sync.vault_path == vault
    assert cfg.obsidian_sync.tasks_file == Path("loom/inbox.md")
    assert cfg.obsidian_sync.resolved_ttl_days == 14
    assert cfg.obsidian_sync.include_blocked is False
    assert cfg.obsidian_sync.exclude_tags == ("debug:trace", "internal")


def test_obsidian_sync_vault_path_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section present but missing vault_path → ConfigError naming the field."""
    _write_config(
        tmp_path,
        monkeypatch,
        dedent(
            """
            [obsidian_sync]
            tasks_file = "_lithos/tasks.md"
            """
        ),
    )
    with pytest.raises(ConfigError, match="obsidian_sync.vault_path"):
        load_config()


def test_obsidian_sync_vault_path_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vault_path runs through Path.expanduser() like other path fields."""
    _write_config(
        tmp_path,
        monkeypatch,
        dedent(
            """
            [obsidian_sync]
            vault_path = "~/Obsidian/Vault"
            """
        ),
    )
    cfg = load_config()
    assert cfg.obsidian_sync is not None
    assert "~" not in str(cfg.obsidian_sync.vault_path)
    assert cfg.obsidian_sync.vault_path == Path("~/Obsidian/Vault").expanduser()


def test_obsidian_sync_tasks_file_must_be_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tasks_file is joined with vault_path at use time, so absolute or
    parent-escape paths are config errors at parse time."""
    vault = tmp_path / "vault"

    _write_config(
        tmp_path,
        monkeypatch,
        dedent(
            f"""
            [obsidian_sync]
            vault_path = "{vault}"
            tasks_file = "/etc/tasks.md"
            """
        ),
    )
    with pytest.raises(ConfigError, match="tasks_file must be relative"):
        load_config()

    _write_config(
        tmp_path,
        monkeypatch,
        dedent(
            f"""
            [obsidian_sync]
            vault_path = "{vault}"
            tasks_file = "../escape.md"
            """
        ),
    )
    with pytest.raises(ConfigError, match="tasks_file must be relative"):
        load_config()


def test_obsidian_sync_resolved_ttl_days_must_be_non_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _write_config(
        tmp_path,
        monkeypatch,
        dedent(
            f"""
            [obsidian_sync]
            vault_path = "{vault}"
            resolved_ttl_days = -1
            """
        ),
    )
    with pytest.raises(ConfigError, match="resolved_ttl_days must be >= 0"):
        load_config()


def test_obsidian_sync_rejects_unknown_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typos like `vault_paths` (or fields slated for later slices like
    `fs_debounce_ms`) raise so the operator catches them at parse time."""
    vault = tmp_path / "vault"
    _write_config(
        tmp_path,
        monkeypatch,
        dedent(
            f"""
            [obsidian_sync]
            vault_path = "{vault}"
            vault_paths = "{vault}"
            """
        ),
    )
    with pytest.raises(ConfigError, match="unknown key.*vault_paths"):
        load_config()


def test_obsidian_sync_include_blocked_must_be_bool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _write_config(
        tmp_path,
        monkeypatch,
        dedent(
            f"""
            [obsidian_sync]
            vault_path = "{vault}"
            include_blocked = "yes"
            """
        ),
    )
    with pytest.raises(ConfigError, match="include_blocked must be a boolean"):
        load_config()


def test_obsidian_sync_exclude_tags_must_be_string_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exclude_tags rejects non-list inputs and any non-string / empty entries."""
    vault = tmp_path / "vault"

    # Not a list at all.
    _write_config(
        tmp_path,
        monkeypatch,
        dedent(
            f"""
            [obsidian_sync]
            vault_path = "{vault}"
            exclude_tags = "debug:trace"
            """
        ),
    )
    with pytest.raises(ConfigError, match="exclude_tags must be a list of strings"):
        load_config()

    # List with a non-string element.
    _write_config(
        tmp_path,
        monkeypatch,
        dedent(
            f"""
            [obsidian_sync]
            vault_path = "{vault}"
            exclude_tags = ["debug:trace", 42]
            """
        ),
    )
    with pytest.raises(ConfigError, match="exclude_tags must be a list of strings"):
        load_config()

    # List with an empty-string element.
    _write_config(
        tmp_path,
        monkeypatch,
        dedent(
            f"""
            [obsidian_sync]
            vault_path = "{vault}"
            exclude_tags = ["debug:trace", ""]
            """
        ),
    )
    with pytest.raises(ConfigError, match="exclude_tags entries must be non-empty"):
        load_config()
