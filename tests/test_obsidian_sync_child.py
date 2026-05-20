"""Tests for ``lithos_loom.children.obsidian_sync`` (Slice 1 US7).

US7 ships a stub child: load config, log operator-visible startup
detail, park on SIGTERM. US8 fills the park with the real bus +
projection subscription runtime.

These tests drive ``_amain`` directly with a fabricated ``LoomConfig``
so they don't shell out to subprocess. The supervisor-level
end-to-end gating is exercised in ``test_supervisor.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import pytest

from lithos_loom.children.obsidian_sync import _amain
from lithos_loom.config import LoomConfig, ObsidianSyncConfig, OrchestratorConfig


async def _cancel_and_drain(task: asyncio.Task[None]) -> None:
    """Cancel a helper task and await its completion so it can't leak past
    the test (Copilot review on #16): a bare ``cancel()`` returns
    immediately, leaving a pending task that may still race and deliver
    its side effect after the test exits.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _cfg_with_obsidian(tmp_path: Path) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        obsidian_sync=ObsidianSyncConfig(
            vault_path=tmp_path / "vault",
            tasks_file=Path("_lithos/tasks.md"),
            resolved_ttl_days=7,
            include_blocked=False,
            exclude_tags=("debug:trace",),
        ),
    )


def _cfg_without_obsidian(tmp_path: Path) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
    )


async def test_obsidian_sync_main_exits_zero_on_stop_event(tmp_path: Path) -> None:
    """``_amain`` parks on its internal stop_event until SIGTERM. Simulate
    by sending SIGTERM via os.kill once the child is in the await."""
    import os
    import signal

    cfg = _cfg_with_obsidian(tmp_path)

    async def _send_sigterm_soon() -> None:
        # Give _amain enough time to install its signal handler before
        # the signal arrives, otherwise we'd race the install and the
        # signal would terminate the test process.
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    sender = asyncio.create_task(_send_sigterm_soon())
    try:
        rc = await asyncio.wait_for(_amain(cfg), timeout=2.0)
    finally:
        await _cancel_and_drain(sender)
    assert rc == 0


async def test_obsidian_sync_main_exits_one_when_config_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Defensive guard: ``_amain`` returns 1 when ``obsidian_sync`` is None,
    rather than parking on a stop event under an undefined config."""
    cfg = _cfg_without_obsidian(tmp_path)
    source_logger = "lithos_loom.children.obsidian_sync"
    with caplog.at_level(logging.ERROR, logger=source_logger):
        rc = await _amain(cfg)
    assert rc == 1
    error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("obsidian-sync spawned without" in m for m in error_msgs), error_msgs


async def test_obsidian_sync_logs_config_on_startup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Operator visibility: the startup INFO log names vault, tasks_file,
    and resolved_ttl_days so a quick log grep can confirm what the child
    actually loaded."""
    import os
    import signal

    cfg = _cfg_with_obsidian(tmp_path)
    source_logger = "lithos_loom.children.obsidian_sync"

    async def _send_sigterm_soon() -> None:
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    sender = asyncio.create_task(_send_sigterm_soon())
    try:
        with caplog.at_level(logging.INFO, logger=source_logger):
            await asyncio.wait_for(_amain(cfg), timeout=2.0)
    finally:
        await _cancel_and_drain(sender)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    started = next((m for m in info_msgs if "obsidian-sync child started" in m), None)
    assert started is not None, f"no startup log; got {info_msgs}"
    assert str(cfg.obsidian_sync.vault_path) in started  # type: ignore[union-attr]
    assert "_lithos/tasks.md" in started
    assert "resolved_ttl_days=7" in started
    # Projection filter knobs surface in the startup log too — useful
    # for operators tweaking them via per-environment config and
    # confirming the child actually picked the change up.
    assert "include_blocked=False" in started
    assert "debug:trace" in started
