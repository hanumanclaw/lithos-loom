"""Shared atomic file-write helper for projection subscriptions.

Extracted from :mod:`._obsidian_projection` so the upcoming
project-context projection (Slice 4) and any future per-file
projection can share the same temp + fsync + rename contract without
copy-paste. The strategy and load-bearing invariants are unchanged
from the original site — only the import surface moved.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

__all__ = ["write_file_atomic"]


async def write_file_atomic(path: Path, content: str) -> None:
    """Atomically rewrite ``path`` with ``content``.

    Strategy: write to ``<path>.tmp``, fsync, then ``os.replace`` onto
    the final path. ``os.replace`` is atomic on POSIX. Creates
    ``path.parent`` if absent. If anything between the temp-write and
    the replace raises, the temp file is best-effort cleaned up so a
    failed write doesn't litter the vault with ``<name>.md.tmp``
    (Copilot review on lithos-loom#17, mirroring
    ``write_result_atomically`` in plugin_runner.py).

    **No internal** ``await`` **points** — load-bearing invariant for
    every projection that uses this. Two properties depend on it:

    1. The fs watcher's self-write suppression. The caller updates
       ``sync_state`` *before* awaiting this function; if there were
       a yield between that update and ``os.replace``, the watcher
       could poll with ``sync_state.last_written_hash`` pointing at
       the new content while the file still showed the old, mis-firing
       per-task suppression. The same window exists for any per-file
       projection (Slice 4 onwards).
    2. The caller's failure-rollback contract. The caller catches
       ``Exception`` to roll back ``sync_state`` when the rename
       didn't apply, and lets ``CancelledError`` propagate without
       rolling back on the grounds that cancellation cannot fire
       mid-rename. That reasoning requires this function to have no
       suspension points where cancellation could fire after the
       rename but before this function returns.

    Don't add ``await`` here without re-deriving both invariants. If
    write latency becomes an issue, ``asyncio.to_thread`` wraps this
    whole synchronous body in one yield-after-completion shot rather
    than introducing yields inside it.

    Synchronous I/O inside an async function — fine for the
    vault-sized files this serves (<10kB typical for tasks files;
    project-context bodies may be larger but still bounded by KB scale).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup. If unlink itself fails (already gone,
        # permission flip), swallow — the original exception is more
        # informative for the operator.
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
