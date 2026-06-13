"""Persist SSE Last-Event-ID cursors across daemon restarts.

Each child process (route-runner, obsidian-sync, github-watcher) maintains
one or more SSE streams, each with its own cursor. The :class:`CursorStore`
writes cursors atomically to ``<work_dir>/sse_cursors.json`` keyed by a
caller-chosen stream name (e.g. ``"task-events"``, ``"note-events"``).

Atomic write uses temp + fsync + rename (same pattern the projection layer
uses for vault files) so a crash mid-write never leaves a half-written file.

The file is a flat ``{"<name>": "<last-event-id>", …}`` JSON object. Each child
process keeps its own cursor file (``<work_dir>/<child>/sse_cursors.json``) and
addresses its streams by plain name (e.g. ``"task-events"``, ``"note-events"``);
the per-stream keying means a single file could hold several streams' cursors,
but the children do not share one in practice.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path

__all__ = ["CursorStore"]

logger = logging.getLogger(__name__)


class CursorStore:
    """Read/write SSE Last-Event-ID values to a JSON file under *path*.

    Parameters
    ----------
    path:
        Filesystem path to the JSON cursor file. The file and any missing
        parent directories are created on first write.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cache: dict[str, str] = self._load()

    # ── public API ──────────────────────────────────────────────────

    def get(self, name: str) -> str | None:
        """Return the persisted cursor for *name*, or ``None``."""
        return self._cache.get(name)

    def save(self, name: str, cursor: str) -> None:
        """Persist *cursor* under *name* (atomic write).

        ``_cache`` is the source of truth for "what is durably on disk", so it
        is updated ONLY after a successful write. If the write fails, the cache
        is left unchanged — the failed cursor is not recorded as persisted, so
        the next ``save`` of the same value retries the disk sync instead of
        short-circuiting as a no-op against an un-persisted cursor.
        """
        if self._cache.get(name) == cursor:
            return  # already durable (cache mirrors disk) — nothing to do
        candidate = {**self._cache, name: cursor}
        if self._write(candidate):
            self._cache = candidate

    # ── internals ───────────────────────────────────────────────────

    def _load(self) -> dict[str, str]:
        """Load existing cursors from disk, or return empty dict."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning(
                "CursorStore: cannot read %s (%s); starting fresh", self._path, exc
            )
            return {}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "CursorStore: corrupt JSON in %s (%s); starting fresh",
                self._path,
                exc,
            )
            return {}

        if not isinstance(data, dict):
            logger.warning(
                "CursorStore: expected object in %s, got %s; starting fresh",
                self._path,
                type(data).__name__,
            )
            return {}

        # Accept only string values; ignore anything else.
        return {
            k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)
        }

    def _write(self, data: dict[str, str]) -> bool:
        """Atomically write *data* (temp → fsync → rename).

        Returns ``True`` on a durable write, ``False`` if it failed. Every
        failure mode — unwritable parent dir, short write, fsync/rename error —
        is logged and reported as ``False`` (never raised) so the caller keeps
        the old cache and the stream degrades gracefully rather than crashing.
        """
        fd = -1
        tmp_path: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=".sse_cursors.",
                suffix=".tmp",
            )
            payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
            # os.write may write fewer bytes than requested; loop until the
            # whole payload is out, or a short write would truncate the file.
            view = memoryview(payload)
            written = 0
            while written < len(payload):
                written += os.write(fd, view[written:])
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp_path, str(self._path))
            tmp_path = None
            return True
        except OSError as exc:
            logger.warning("CursorStore: failed to write %s: %s", self._path, exc)
            return False
        finally:
            if fd >= 0:
                os.close(fd)
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
