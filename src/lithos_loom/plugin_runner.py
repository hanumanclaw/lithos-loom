"""Plugin subprocess runner and ``result.json`` contract.

Plugins are stateless subprocesses invoked with three flags:

    <command> --task-json <path> --work-dir <path> --result-file <path>

The route TOML's ``command`` field is a template; this module substitutes
the three ``{{task_json}}`` / ``{{work_dir}}`` / ``{{result_file}}`` tokens
and then ``shlex.split``s the result into argv.

The plugin writes its outcome atomically to ``--result-file`` (using
:func:`write_result_atomically` here, or any equivalent atomic-rename
implementation in another language). The runner reads the file, validates
it against ``docs/result-schema.json``, and returns the parsed dict for
the caller to apply.

``max_runtime_seconds`` enforcement: if the plugin doesn't exit within
the budget, the runner sends SIGTERM, waits up to a 5s grace period,
SIGKILLs anything still alive, then raises :class:`TimeoutError`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

import jsonschema

from lithos_loom.errors import PluginContractError

__all__ = ["run_plugin", "write_result_atomically"]

logger = logging.getLogger(__name__)

_FORCE_KILL_GRACE_SECONDS = 5.0


def write_result_atomically(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically: temp + fsync + rename.

    The orchestrator must never observe a partial or truncated result file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


async def run_plugin(
    *,
    command: str,
    task_json_path: Path,
    work_dir: Path,
    result_file: Path,
    max_runtime_seconds: int | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Spawn the plugin subprocess and return the parsed + validated result.

    Parameters
    ----------
    command:
        The route's ``command`` template. The three ``{{task_json}}``,
        ``{{work_dir}}``, and ``{{result_file}}`` tokens are substituted
        with the absolute paths supplied here.
    task_json_path, work_dir, result_file:
        Absolute paths the plugin will read / write.
    max_runtime_seconds:
        Optional wall-clock budget. SIGTERM on overrun, SIGKILL after a
        5s grace period, then :class:`TimeoutError`.
    env:
        Optional environment overrides for the subprocess. Defaults to
        the parent's full environment.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    argv = _build_argv(command, task_json_path, work_dir, result_file)

    # Clear any stale result file from a prior run before launching. Without
    # this, a plugin that exits without writing a fresh result.json would
    # leave the runner parsing the previous attempt's outcome and acting on
    # it as if it were the new one.
    with contextlib.suppress(FileNotFoundError):
        result_file.unlink()

    proc = await asyncio.create_subprocess_exec(*argv, env=env)
    try:
        if max_runtime_seconds is None:
            await proc.wait()
        else:
            try:
                await asyncio.wait_for(proc.wait(), timeout=max_runtime_seconds)
            except TimeoutError:
                await _kill_with_grace(proc)
                raise TimeoutError(
                    f"plugin exceeded max_runtime_seconds={max_runtime_seconds}s"
                ) from None
    except BaseException:
        # Cancellation or any other exit: don't leak a child.
        if proc.returncode is None:
            await _kill_with_grace(proc)
        raise

    if not result_file.exists():
        raise PluginContractError(
            f"plugin did not write result file at {result_file} "
            f"(exit code {proc.returncode})"
        )

    try:
        payload = json.loads(result_file.read_text())
    except json.JSONDecodeError as exc:
        raise PluginContractError(
            f"plugin result file at {result_file} is not valid JSON: {exc}"
        ) from exc

    _validate_result_schema(payload)
    return payload


# ── Internals ──────────────────────────────────────────────────────────


def _build_argv(
    command: str, task_json_path: Path, work_dir: Path, result_file: Path
) -> list[str]:
    substituted = (
        command.replace("{{task_json}}", str(task_json_path))
        .replace("{{work_dir}}", str(work_dir))
        .replace("{{result_file}}", str(result_file))
    )
    argv = shlex.split(substituted)
    if not argv:
        raise PluginContractError(f"plugin command resolved to empty argv: {command!r}")
    return argv


async def _kill_with_grace(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=_FORCE_KILL_GRACE_SECONDS)
        return
    except TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()
    logger.warning(
        "plugin %d did not honour SIGTERM within %ss; sent SIGKILL",
        proc.pid,
        _FORCE_KILL_GRACE_SECONDS,
    )


_RESULT_SCHEMA: dict[str, Any] | None = None


def _load_result_schema() -> dict[str, Any]:
    global _RESULT_SCHEMA
    schema = _RESULT_SCHEMA
    if schema is None:
        schema = json.loads(_packaged_schema_path().read_text())
        _RESULT_SCHEMA = schema
    return schema


def _packaged_schema_path() -> Path:
    """Locate ``docs/result-schema.json`` whether running from src or installed.

    The schema is bundled at the repo root, not under the package — so we
    walk up from this module file until we find ``docs/result-schema.json``.
    Falls back to the package's data files if the walk doesn't find it
    (e.g. when running from a wheel install).
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "docs" / "result-schema.json"
        if candidate.exists():
            return candidate
    # Last-ditch: maybe future packaging puts it inside the package.
    raise PluginContractError(
        "could not locate docs/result-schema.json; ensure it ships with the package"
    )


def _validate_result_schema(payload: Any) -> None:
    schema = _load_result_schema()
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        path = list(exc.absolute_path)
        raise PluginContractError(
            f"plugin result.json violates schema: {exc.message} (at {path})"
        ) from exc
