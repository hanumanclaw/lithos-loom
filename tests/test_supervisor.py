"""Tests for ``lithos_loom.supervisor`` (Slice 0 US1).

The Supervisor reads a ``LoomConfig``, fans out subprocess children for each
enabled category, propagates a graceful shutdown to them, and surfaces a
single exit code that summarises the run. v1 does not auto-restart crashed
children — first crash triggers a coordinated shutdown of the rest.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lithos_loom.config import (
    LoomConfig,
    ObsidianSyncConfig,
    OrchestratorConfig,
)
from lithos_loom.main import app
from lithos_loom.supervisor import CategorySpec, Supervisor, default_categories

runner = CliRunner()


# ── Fixtures ───────────────────────────────────────────────────────────


def _minimal_cfg(tmp_path: Path) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        source_path=tmp_path / "config.toml",
    )


def _echo_category(
    name: str = "echo",
    *,
    enabled: bool = True,
    extra_args: tuple[str, ...] = (),
) -> CategorySpec:
    return CategorySpec(
        name=name,
        module="lithos_loom.children._echo",
        enabled=lambda _cfg, e=enabled: e,
        extra_args=extra_args,
    )


# ── Tests ──────────────────────────────────────────────────────────────


async def test_supervisor_returns_zero_when_no_categories(tmp_path: Path) -> None:
    """An empty category list short-circuits to exit 0 without spawning."""
    sup = Supervisor(_minimal_cfg(tmp_path), categories=())
    assert await sup.run() == 0
    assert sup.children == ()


async def test_supervisor_returns_zero_when_all_categories_disabled(
    tmp_path: Path,
) -> None:
    """A category with ``enabled(cfg) is False`` is never spawned."""
    sup = Supervisor(
        _minimal_cfg(tmp_path),
        categories=[_echo_category("echo-off", enabled=False)],
    )
    assert await sup.run() == 0
    assert sup.children == ()


async def test_supervisor_spawns_enabled_children_then_shuts_down_cleanly(
    tmp_path: Path,
) -> None:
    """Children are spawned, are alive after spawn, and exit cleanly on shutdown()."""
    sup = Supervisor(
        _minimal_cfg(tmp_path),
        categories=[_echo_category()],
    )

    run_task = asyncio.create_task(sup.run())
    # Give the supervisor a moment to spawn the child.
    await asyncio.sleep(0.2)

    assert len(sup.children) == 1
    child = sup.children[0]
    assert child.spec.name == "echo"
    assert child.proc.pid > 0
    assert child.proc.returncode is None

    await sup.shutdown()
    exit_code = await asyncio.wait_for(run_task, timeout=5.0)
    assert exit_code == 0


async def test_supervisor_filters_by_enabled_predicate(tmp_path: Path) -> None:
    """Only the enabled category spawns; the disabled one is skipped silently."""
    sup = Supervisor(
        _minimal_cfg(tmp_path),
        categories=[
            _echo_category("on", enabled=True),
            _echo_category("off", enabled=False),
        ],
    )

    run_task = asyncio.create_task(sup.run())
    await asyncio.sleep(0.2)

    assert [c.spec.name for c in sup.children] == ["on"]

    await sup.shutdown()
    assert await asyncio.wait_for(run_task, timeout=5.0) == 0


async def test_supervisor_waits_for_child_that_briefly_ignores_sigterm(
    tmp_path: Path,
) -> None:
    """Patient shutdown: child handles SIGTERM late, supervisor still cleans up."""
    sup = Supervisor(
        _minimal_cfg(tmp_path),
        categories=[_echo_category(extra_args=("--ignore-sigterm-for", "0.5"))],
        shutdown_grace_seconds=3.0,
    )

    run_task = asyncio.create_task(sup.run())
    await asyncio.sleep(0.2)
    await sup.shutdown()

    exit_code = await asyncio.wait_for(run_task, timeout=5.0)
    assert exit_code == 0
    # The child eventually honoured SIGTERM and exited cleanly.
    assert sup.children[0].proc.returncode == 0


async def test_supervisor_force_kills_child_that_exceeds_grace_period(
    tmp_path: Path,
) -> None:
    """If a child won't exit within shutdown_grace_seconds, supervisor SIGKILLs it."""
    sup = Supervisor(
        _minimal_cfg(tmp_path),
        categories=[_echo_category(extra_args=("--ignore-sigterm-for", "10"))],
        shutdown_grace_seconds=0.5,
    )

    run_task = asyncio.create_task(sup.run())
    await asyncio.sleep(0.2)
    await sup.shutdown()

    exit_code = await asyncio.wait_for(run_task, timeout=5.0)
    # Force-killed children are not "clean"; surface as non-zero.
    assert exit_code != 0
    assert sup.children[0].proc.returncode == -signal.SIGKILL


async def test_supervisor_records_child_crash_and_shuts_down_others(
    tmp_path: Path,
) -> None:
    """A child crashing on its own triggers shutdown; supervisor exits non-zero."""
    sup = Supervisor(
        _minimal_cfg(tmp_path),
        categories=[
            _echo_category("crasher", extra_args=("--crash-after", "0.1")),
            _echo_category("survivor"),
        ],
    )

    run_task = asyncio.create_task(sup.run())
    exit_code = await asyncio.wait_for(run_task, timeout=5.0)

    assert exit_code != 0
    assert sup.crashes == ("crasher",)
    # The survivor was sent SIGTERM and exited cleanly.
    survivor = next(c for c in sup.children if c.spec.name == "survivor")
    assert survivor.proc.returncode in (0, -signal.SIGTERM)


async def test_supervisor_terminates_already_spawned_children_when_later_spawn_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial startup must not orphan children.

    If spawn N+1 raises after spawn N succeeded, the supervisor must SIGTERM
    the already-running children before the exception bubbles. Otherwise a
    failed startup leaves subprocesses adrift, breaking the "single start/
    stop surface" property of US1.
    """
    real_spawn = asyncio.create_subprocess_exec
    call_count = {"n": 0}

    async def flaky_spawn(
        *args: object, **kwargs: object
    ) -> asyncio.subprocess.Process:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated spawn failure")
        return await real_spawn(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", flaky_spawn)

    sup = Supervisor(
        _minimal_cfg(tmp_path),
        categories=[
            _echo_category("first"),
            _echo_category("second"),
        ],
    )

    with pytest.raises(RuntimeError, match="simulated spawn failure"):
        await asyncio.wait_for(sup.run(), timeout=5.0)

    # The first child was spawned successfully — it must have been reaped.
    assert len(sup.children) == 1
    first = sup.children[0]
    assert first.spec.name == "first"
    assert first.proc.returncode is not None, (
        "first child was orphaned when second spawn failed"
    )
    # Clean SIGTERM honour (0) or signal-killed (-SIGTERM/-SIGKILL) — any of
    # these proves the supervisor reaped it rather than leaking it.
    assert first.proc.returncode in (0, -signal.SIGTERM, -signal.SIGKILL)


async def test_supervisor_does_not_restart_crashed_child(tmp_path: Path) -> None:
    """v1 lifecycle is monolithic: no auto-restart on child crash."""
    sup = Supervisor(
        _minimal_cfg(tmp_path),
        categories=[_echo_category(extra_args=("--crash-after", "0.1"))],
    )

    run_task = asyncio.create_task(sup.run())
    await asyncio.wait_for(run_task, timeout=5.0)

    assert len(sup.children) == 1
    assert sup.crashes == ("echo",)


async def test_supervisor_spawns_child_with_config_path_argv(tmp_path: Path) -> None:
    """The child is invoked with --config <source_path> so it can load the same TOML."""
    cfg_path = tmp_path / "alt.toml"
    cfg = replace(_minimal_cfg(tmp_path), source_path=cfg_path)
    sup = Supervisor(cfg, categories=[_echo_category(extra_args=("--echo-argv",))])

    run_task = asyncio.create_task(sup.run())
    await asyncio.sleep(0.3)
    assert len(sup.children) == 1
    await sup.shutdown()
    await asyncio.wait_for(run_task, timeout=5.0)

    # The echo child writes its argv to stderr when --echo-argv is set;
    # we just confirm the supervisor did pass --config <cfg_path>.
    argv = sup.children[0].argv
    assert "--config" in argv
    assert str(cfg_path) in argv
    assert "--echo-argv" in argv


# ── CLI smoke ──────────────────────────────────────────────────────────


def test_run_command_exits_cleanly_when_no_routes_or_subscriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``lithos-loom run`` returns 0 when no enabled categories apply.

    Story 5's ``default_categories()`` includes the route-runner category
    gated on ``cfg.routes`` being non-empty. With a minimal config that
    has no routes (and no subscriptions), the supervisor short-circuits
    to exit 0 without spawning any subprocesses or contacting Lithos.
    """
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[orchestrator]\n"
        'agent_id = "lithos-orchestrator-test"\n'
        'lithos_url = "http://localhost:8765"\n'
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg))
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output


def test_run_command_configures_parent_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run`` must call ``logging.basicConfig`` in the parent process.

    Without this, the Supervisor's INFO (``spawned child …``) and WARNING
    (``[Friction] child … exited``, ``SIGKILLing …``) lines are silently
    dropped when running under ``uv run lithos-loom run`` because no
    handler is attached to the root logger. The child process configures
    its own logging; the parent did not, until this regression test.
    """
    import logging

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[orchestrator]\n"
        'agent_id = "lithos-orchestrator-test"\n'
        'lithos_url = "http://localhost:8765"\n'
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg))

    calls: list[dict[str, object]] = []

    def _spy(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(logging, "basicConfig", _spy)

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].get("level") == logging.INFO
    assert "%(name)s" in str(calls[0].get("format", ""))


# ── Echo child standalone smoke ────────────────────────────────────────


async def test_echo_child_responds_to_sigterm() -> None:
    """The bundled echo child exits 0 when SIGTERM'd."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lithos_loom.children._echo",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.sleep(0.1)
    proc.terminate()
    rc = await asyncio.wait_for(proc.wait(), timeout=5.0)
    assert rc == 0


async def test_echo_child_crash_after_flag() -> None:
    """``--crash-after`` causes a non-zero exit on its own."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lithos_loom.children._echo",
        "--crash-after",
        "0.05",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await asyncio.wait_for(proc.wait(), timeout=5.0)
    assert rc != 0


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="add_signal_handler/SIGTERM semantics differ on Windows",
)
def test_supervisor_signal_handler_exists() -> None:
    """Sanity: importing supervisor doesn't blow up on signal-handler imports."""
    # Soft check that the module imported all the bits it needs.
    import lithos_loom.supervisor as mod

    assert hasattr(mod, "Supervisor")
    assert hasattr(mod, "CategorySpec")


# ── default_categories() / obsidian-sync gate (Slice 1 US7) ────────────


def _obs_sync_cfg(tmp_path: Path) -> LoomConfig:
    return replace(
        _minimal_cfg(tmp_path),
        obsidian_sync=ObsidianSyncConfig(vault_path=tmp_path / "vault"),
    )


def test_default_categories_includes_obsidian_sync_spec(tmp_path: Path) -> None:
    """The obsidian-sync category is registered; the supervisor gates it on
    cfg.obsidian_sync presence rather than dropping it from the list."""
    names = [c.name for c in default_categories()]
    assert "obsidian-sync" in names
    assert "route-runner" in names


def test_obsidian_sync_disabled_when_section_absent(tmp_path: Path) -> None:
    """With no [obsidian_sync] in config, the spec's enabled() returns False."""
    cfg = _minimal_cfg(tmp_path)
    obs_spec = next(c for c in default_categories() if c.name == "obsidian-sync")
    assert obs_spec.enabled(cfg) is False


def test_obsidian_sync_enabled_when_section_present(tmp_path: Path) -> None:
    """With [obsidian_sync] in config, the spec's enabled() returns True."""
    cfg = _obs_sync_cfg(tmp_path)
    obs_spec = next(c for c in default_categories() if c.name == "obsidian-sync")
    assert obs_spec.enabled(cfg) is True
