"""Plugin subprocess runner and ``result.json`` contract.

Stub — implements ``docs/prd/mvp.md`` US-3, US-31, US-33, US-34:

* US-3: write_result_atomically (temp + fsync + rename) + JSON Schema validator
* US-31: per-task staging directory ``{loom.work_dir}/{task.id}/`` lifecycle
  (auto-clean on success, retain on failure when ``retain_failed_workdirs``)
* US-33: schema validated against ``docs/result-schema.json`` (versioned artifact)
* US-34: ``max_runtime_seconds`` enforcement via SIGTERM at timeout
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_result_atomically(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically: temp + fsync + rename (US-3).

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


def run_plugin(*args: object, **kwargs: object) -> dict[str, Any]:
    """Invoke a plugin subprocess and parse its result.

    Stub — implement per docs/prd/mvp.md US-3 / US-34.
    """
    raise NotImplementedError(
        "plugin_runner.run_plugin — implement per docs/prd/mvp.md US-3 / US-34"
    )
