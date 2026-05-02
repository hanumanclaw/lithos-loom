"""Tests for the atomic-write helper in plugin_runner."""

from __future__ import annotations

from pathlib import Path

from lithos_loom.plugin_runner import write_result_atomically


def test_write_result_atomically_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "result.json"
    payload = {"schema_version": 1, "task_id": "t1", "status": "succeeded"}
    write_result_atomically(target, payload)
    assert target.exists()
    import json

    assert json.loads(target.read_text()) == payload


def test_write_result_atomically_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "result.json"
    write_result_atomically(target, {"k": "v"})
    assert target.exists()


def test_write_result_atomically_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "result.json"
    write_result_atomically(target, {"k": "v1"})
    write_result_atomically(target, {"k": "v2"})
    import json

    assert json.loads(target.read_text()) == {"k": "v2"}
