"""Tests for ``lithos_loom.cursor_store`` — SSE Last-Event-ID persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lithos_loom.cursor_store import CursorStore

# ── basic round-trip ────────────────────────────────────────────────────


def test_save_and_get(tmp_path: Path) -> None:
    """Write a cursor, read it back in the same instance."""
    store = CursorStore(tmp_path / "cursors.json")
    assert store.get("task-events") is None
    store.save("task-events", "evt-42")
    assert store.get("task-events") == "evt-42"


def test_persistence_across_instances(tmp_path: Path) -> None:
    """A new CursorStore on the same path should recover persisted cursors."""
    path = tmp_path / "cursors.json"
    store1 = CursorStore(path)
    store1.save("task-events", "evt-1")
    store1.save("note-events", "evt-2")

    store2 = CursorStore(path)
    assert store2.get("task-events") == "evt-1"
    assert store2.get("note-events") == "evt-2"


def test_overwrite_cursor(tmp_path: Path) -> None:
    """Updating a cursor replaces the previous value."""
    store = CursorStore(tmp_path / "cursors.json")
    store.save("task-events", "evt-1")
    store.save("task-events", "evt-2")
    assert store.get("task-events") == "evt-2"

    reloaded = CursorStore(tmp_path / "cursors.json")
    assert reloaded.get("task-events") == "evt-2"


def test_no_op_save_skips_write(tmp_path: Path) -> None:
    """Saving the same value again should not re-write the file."""
    path = tmp_path / "cursors.json"
    store = CursorStore(path)
    store.save("k", "v")
    mtime_after_first = path.stat().st_mtime_ns
    store.save("k", "v")
    mtime_after_second = path.stat().st_mtime_ns
    assert mtime_after_first == mtime_after_second


# ── missing / corrupt file ──────────────────────────────────────────────


def test_missing_file_returns_none(tmp_path: Path) -> None:
    """When the cursor file doesn't exist, get() returns None."""
    store = CursorStore(tmp_path / "nonexistent.json")
    assert store.get("anything") is None


def test_corrupt_json_starts_fresh(tmp_path: Path) -> None:
    """A corrupt file is tolerated — the store starts empty."""
    path = tmp_path / "cursors.json"
    path.write_text("not json!")
    store = CursorStore(path)
    assert store.get("task-events") is None
    # Writing still works after recovering from corruption.
    store.save("task-events", "evt-new")
    assert store.get("task-events") == "evt-new"


def test_non_object_json_starts_fresh(tmp_path: Path) -> None:
    """A valid JSON file that is not an object is treated as empty."""
    path = tmp_path / "cursors.json"
    path.write_text('"just a string"')
    store = CursorStore(path)
    assert store.get("task-events") is None


def test_non_string_values_filtered(tmp_path: Path) -> None:
    """Only string values are loaded; non-string values are silently dropped."""
    path = tmp_path / "cursors.json"
    path.write_text(json.dumps({"good": "val", "bad": 123, "also_bad": None}))
    store = CursorStore(path)
    assert store.get("good") == "val"
    assert store.get("bad") is None
    assert store.get("also_bad") is None


# ── parent directory creation ───────────────────────────────────────────


def test_creates_parent_dirs(tmp_path: Path) -> None:
    """The store should create parent directories on first write."""
    path = tmp_path / "deep" / "nested" / "cursors.json"
    store = CursorStore(path)
    store.save("k", "v")
    assert path.exists()
    reloaded = CursorStore(path)
    assert reloaded.get("k") == "v"


# ── atomic write ────────────────────────────────────────────────────────


def test_file_contains_valid_json(tmp_path: Path) -> None:
    """The written file must be valid JSON with the expected shape."""
    path = tmp_path / "cursors.json"
    store = CursorStore(path)
    store.save("a", "1")
    store.save("b", "2")
    data = json.loads(path.read_text())
    assert data == {"a": "1", "b": "2"}


def test_failed_write_does_not_poison_cache_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient write failure must not record the cursor as persisted.

    The cache is the source of truth for "what is on disk", so after a failed
    write it must stay unchanged — otherwise a later ``save`` of the same value
    would no-op against an un-persisted cursor and the daemon would replay from
    an older cursor on restart (duplicate work).
    """
    import os

    path = tmp_path / "cursors.json"
    store = CursorStore(path)

    def boom(fd: int, data: object) -> int:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "write", boom)
    store.save("task-events", "evt-1")
    assert not path.exists()  # the write failed
    assert store.get("task-events") is None  # cache NOT poisoned

    # Disk recovers; saving the SAME value must retry (not short-circuit).
    monkeypatch.undo()
    store.save("task-events", "evt-1")
    assert json.loads(path.read_text()) == {"task-events": "evt-1"}
    assert store.get("task-events") == "evt-1"


def test_partial_os_write_writes_full_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.write`` may return a short count; the store must loop until the
    whole payload is on disk rather than truncating the file."""
    import os

    real_write = os.write

    def short_write(fd: int, data: object) -> int:
        # Force a one-byte-at-a-time write to exercise the loop.
        view = memoryview(bytes(data))  # type: ignore[arg-type]
        return real_write(fd, view[:1])

    path = tmp_path / "cursors.json"
    store = CursorStore(path)
    monkeypatch.setattr(os, "write", short_write)
    store.save("task-events", "evt-1234567890")

    # The file must be complete and parseable despite the short writes.
    assert json.loads(path.read_text()) == {"task-events": "evt-1234567890"}
