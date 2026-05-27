"""Tests for ``lithos-loom project regenerate-done``.

Typer CliRunner + a mocked LithosClient (patched in
``lithos_loom.cli._regenerate_done``, where ``collect_resolved_lines``
imports it). Focus: the all-resolved query + client-side slug filter,
chronological sort, overwrite semantics, the dry-run/confirm/--yes
guard rails, and the exit-code contract.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from lithos_loom.cli.project import project_app
from lithos_loom.lithos_client import Task


def _write_config(tmp_path: Path, *, obsidian: bool = True) -> Path:
    cfg_path = tmp_path / "config.toml"
    body = """
[orchestrator]
agent_id = "test-agent"
lithos_url = "http://localhost:8765"
"""
    if obsidian:
        body += f"""
[obsidian_sync]
vault_path = "{tmp_path / "vault"}"
tasks_file = "_lithos/tasks.md"
projects_dir = "_lithos/projects"
"""
    cfg_path.write_text(body, encoding="utf-8")
    return cfg_path


def _done_path(tmp_path: Path, slug: str) -> Path:
    return tmp_path / "vault" / "_lithos" / "projects" / slug / f"{slug}-done.md"


def _local_noon(d: date) -> datetime:
    """Noon-local on ``d`` so ``.astimezone().date()`` round-trips to ``d``
    regardless of host timezone (mirrors the projection tests)."""
    return datetime(d.year, d.month, d.day, 12, 0, 0).astimezone()


def _task(
    task_id: str,
    *,
    title: str = "A task",
    status: str = "completed",
    project: str | None = "demo",
    resolved_on: date | None = date(2026, 5, 20),
    resolved_at: datetime | None | object = ...,
    created_at: datetime | None = None,
) -> Task:
    meta: dict[str, Any] = {}
    if project is not None:
        meta["project"] = project
    if resolved_at is ...:
        resolved_at = _local_noon(resolved_on) if resolved_on is not None else None
    return Task(
        id=task_id,
        title=title,
        status=status,
        tags=(),
        metadata=meta,
        claims=(),
        resolved_at=resolved_at,  # type: ignore[arg-type]
        created_at=created_at,
    )


def _stub_client(*, completed: list[Task], cancelled: list[Task]) -> Any:
    """LithosClient stand-in whose task_list returns per-status lists.

    Keyed on the ``status`` kwarg (not call order) so the stub stays
    correct if the command's status iteration is reordered/extended.
    """
    by_status = {"completed": completed, "cancelled": cancelled}

    async def _task_list(*, status: str | None = None, **_: Any) -> list[Task]:
        return by_status.get(status or "", [])

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    client.task_list.side_effect = _task_list
    return client


def _run(args: list[str], client: Any, *, stdin: str | None = None) -> Any:
    runner = CliRunner()
    with patch("lithos_loom.cli._regenerate_done.LithosClient", return_value=client):
        return runner.invoke(project_app, args, input=stdin)


def _combined(result: Any) -> str:
    """stdout + stderr (this CliRunner separates the streams; error and
    abort messages go to stderr via ``typer.echo(..., err=True)``)."""
    return result.stdout + (result.stderr if hasattr(result, "stderr") else "")


# ── Happy path ─────────────────────────────────────────────────────────


def test_regenerate_writes_sorted_resolved_lines(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    client = _stub_client(
        completed=[
            _task("t2", title="Later one", resolved_on=date(2026, 5, 20)),
            _task("t1", title="Earlier one", resolved_on=date(2026, 5, 10)),
        ],
        cancelled=[
            _task(
                "t3", title="Dropped", status="cancelled", resolved_on=date(2026, 5, 15)
            ),
        ],
    )
    result = _run(["regenerate-done", "--slug", "demo", "-c", str(cfg)], client)
    assert result.exit_code == 0, result.stdout

    content = _done_path(tmp_path, "demo").read_text(encoding="utf-8")
    lines = content.splitlines()
    # Sorted ascending by resolution date: t1 (05-10), t3 (05-15), t2 (05-20).
    assert lines == [
        "- [x] Earlier one 🆔 lithos:t1 #project/demo ✅ 2026-05-10",
        "- [-] Dropped 🆔 lithos:t3 #project/demo ❌ 2026-05-15",
        "- [x] Later one 🆔 lithos:t2 #project/demo ✅ 2026-05-20",
    ]
    assert content.endswith("\n")


def test_filters_out_other_and_missing_project(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    client = _stub_client(
        completed=[
            _task("keep", project="demo"),
            _task("other", project="something-else"),
            _task("none", project=None),
        ],
        cancelled=[],
    )
    result = _run(["regenerate-done", "-s", "demo", "-c", str(cfg)], client)
    assert result.exit_code == 0, result.stdout
    content = _done_path(tmp_path, "demo").read_text(encoding="utf-8")
    assert "lithos:keep" in content
    assert "lithos:other" not in content
    assert "lithos:none" not in content


def test_json_output(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    client = _stub_client(completed=[_task("t1")], cancelled=[])
    result = _run(
        ["regenerate-done", "-s", "demo", "-f", "json", "-c", str(cfg)], client
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["slug"] == "demo"
    assert payload["action"] == "written"
    assert payload["count"] == 1
    assert payload["written"] is True
    assert payload["path"].endswith("demo/demo-done.md")


def test_json_dry_run_emits_json_not_banner(tmp_path: Path) -> None:
    """--format json honors json on the dry-run path (no human banner)."""
    cfg = _write_config(tmp_path)
    client = _stub_client(completed=[_task("t1"), _task("t2")], cancelled=[])
    result = _run(
        ["regenerate-done", "-s", "demo", "--dry-run", "-f", "json", "-c", str(cfg)],
        client,
    )
    assert result.exit_code == 0, result.stdout
    assert "NO CHANGES MADE" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["action"] == "dry-run"
    assert payload["count"] == 2
    assert payload["written"] is False
    assert not _done_path(tmp_path, "demo").exists()


def test_json_noop_emits_json(tmp_path: Path) -> None:
    """Zero tasks + no file in json mode → a parseable noop object on stdout."""
    cfg = _write_config(tmp_path)
    client = _stub_client(completed=[], cancelled=[])
    result = _run(
        ["regenerate-done", "-s", "demo", "-f", "json", "-c", str(cfg)], client
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["action"] == "noop"
    assert payload["count"] == 0
    assert payload["written"] is False
    assert not _done_path(tmp_path, "demo").exists()


def test_json_aborted_emits_json(tmp_path: Path) -> None:
    """Declining the overwrite prompt in json mode → a parseable aborted object."""
    cfg = _write_config(tmp_path)
    done = _done_path(tmp_path, "demo")
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text("- [x] stale 🆔 lithos:old ✅ 2026-01-01\n", encoding="utf-8")

    client = _stub_client(completed=[_task("t1")], cancelled=[])
    result = _run(
        ["regenerate-done", "-s", "demo", "-f", "json", "-c", str(cfg)],
        client,
        stdin="n\n",
    )
    assert result.exit_code == 0, result.stdout
    # The prompt text + JSON share stdout; the last non-empty line is the JSON.
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["action"] == "aborted"
    assert payload["written"] is False
    assert "lithos:old" in done.read_text(encoding="utf-8")


def test_same_id_in_both_lists_deduped(tmp_path: Path) -> None:
    """Defensive: if Lithos returns a task under both status filters,
    it's written once."""
    cfg = _write_config(tmp_path)
    client = _stub_client(
        completed=[_task("dup")],
        cancelled=[_task("dup", status="cancelled")],
    )
    result = _run(["regenerate-done", "-s", "demo", "-c", str(cfg)], client)
    assert result.exit_code == 0, result.stdout
    content = _done_path(tmp_path, "demo").read_text(encoding="utf-8")
    assert content.count("lithos:dup") == 1


def test_same_date_ties_broken_by_id(tmp_path: Path) -> None:
    """Two tasks resolved on the same day sort deterministically by id."""
    cfg = _write_config(tmp_path)
    client = _stub_client(
        completed=[
            _task("zzz", title="Zed", resolved_on=date(2026, 5, 20)),
            _task("aaa", title="Ay", resolved_on=date(2026, 5, 20)),
        ],
        cancelled=[],
    )
    result = _run(["regenerate-done", "-s", "demo", "-c", str(cfg)], client)
    assert result.exit_code == 0, result.stdout
    lines = _done_path(tmp_path, "demo").read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith("✅ 2026-05-20") and "lithos:aaa" in lines[0]
    assert "lithos:zzz" in lines[1]


# ── Dry run ────────────────────────────────────────────────────────────


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    client = _stub_client(completed=[_task("t1"), _task("t2")], cancelled=[])
    result = _run(
        ["regenerate-done", "-s", "demo", "--dry-run", "-c", str(cfg)], client
    )
    assert result.exit_code == 0, result.stdout
    assert "NO CHANGES MADE" in result.stdout
    assert "resolved tasks found: 2" in result.stdout
    assert not _done_path(tmp_path, "demo").exists()


# ── Overwrite + confirm gate ───────────────────────────────────────────


def test_overwrite_prompt_declined_keeps_file(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    done = _done_path(tmp_path, "demo")
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text("- [x] stale line 🆔 lithos:old ✅ 2026-01-01\n", encoding="utf-8")

    client = _stub_client(completed=[_task("t1")], cancelled=[])
    result = _run(
        ["regenerate-done", "-s", "demo", "-c", str(cfg)], client, stdin="n\n"
    )
    assert result.exit_code == 0
    assert "aborted; no changes made" in _combined(result)
    # File untouched.
    assert "lithos:old" in done.read_text(encoding="utf-8")
    assert "lithos:t1" not in done.read_text(encoding="utf-8")


def test_overwrite_prompt_accepted_replaces_file(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    done = _done_path(tmp_path, "demo")
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text("- [x] stale line 🆔 lithos:old ✅ 2026-01-01\n", encoding="utf-8")

    client = _stub_client(completed=[_task("t1")], cancelled=[])
    result = _run(
        ["regenerate-done", "-s", "demo", "-c", str(cfg)], client, stdin="y\n"
    )
    assert result.exit_code == 0, result.stdout
    content = done.read_text(encoding="utf-8")
    assert "lithos:old" not in content
    assert "lithos:t1" in content


def test_yes_bypasses_prompt(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    done = _done_path(tmp_path, "demo")
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text("- [x] stale 🆔 lithos:old ✅ 2026-01-01\n", encoding="utf-8")

    client = _stub_client(completed=[_task("t1")], cancelled=[])
    result = _run(["regenerate-done", "-s", "demo", "--yes", "-c", str(cfg)], client)
    assert result.exit_code == 0, result.stdout
    assert "lithos:old" not in done.read_text(encoding="utf-8")


def test_fresh_file_writes_without_prompt(tmp_path: Path) -> None:
    """No existing file → nothing to clobber → no confirmation needed."""
    cfg = _write_config(tmp_path)
    client = _stub_client(completed=[_task("t1")], cancelled=[])
    # No stdin provided: if it prompted, the runner would error/hang.
    result = _run(["regenerate-done", "-s", "demo", "-c", str(cfg)], client)
    assert result.exit_code == 0, result.stdout
    assert _done_path(tmp_path, "demo").exists()


# ── Zero-result handling ───────────────────────────────────────────────


def test_zero_tasks_no_existing_file_is_noop(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    client = _stub_client(completed=[], cancelled=[])
    result = _run(["regenerate-done", "-s", "demo", "-c", str(cfg)], client)
    assert result.exit_code == 0
    assert "nothing to write" in _combined(result)
    assert not _done_path(tmp_path, "demo").exists()


def test_zero_tasks_with_existing_file_prompts_to_clear(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    done = _done_path(tmp_path, "demo")
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text("- [x] stale 🆔 lithos:old ✅ 2026-01-01\n", encoding="utf-8")

    client = _stub_client(completed=[], cancelled=[])
    result = _run(
        ["regenerate-done", "-s", "demo", "-c", str(cfg)], client, stdin="y\n"
    )
    assert result.exit_code == 0, result.stdout
    assert done.read_text(encoding="utf-8") == ""


def test_yes_clears_existing_file_without_prompt(tmp_path: Path) -> None:
    """--yes bypasses the prompt for the clear action too (no stdin)."""
    cfg = _write_config(tmp_path)
    done = _done_path(tmp_path, "demo")
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text("- [x] stale 🆔 lithos:old ✅ 2026-01-01\n", encoding="utf-8")

    client = _stub_client(completed=[], cancelled=[])
    result = _run(["regenerate-done", "-s", "demo", "--yes", "-c", str(cfg)], client)
    assert result.exit_code == 0, result.stdout
    assert done.read_text(encoding="utf-8") == ""


# ── resolved_at fallback ───────────────────────────────────────────────


def test_resolved_at_none_falls_back_to_created_at(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    client = _stub_client(
        completed=[
            _task(
                "t1",
                resolved_at=None,
                created_at=_local_noon(date(2026, 4, 1)),
            )
        ],
        cancelled=[],
    )
    result = _run(["regenerate-done", "-s", "demo", "-c", str(cfg)], client)
    assert result.exit_code == 0, result.stdout
    content = _done_path(tmp_path, "demo").read_text(encoding="utf-8")
    assert "✅ 2026-04-01" in content


# ── Validation + failure exit codes ────────────────────────────────────


def test_invalid_slug_unassigned_exits_2(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    client = _stub_client(completed=[], cancelled=[])
    result = _run(["regenerate-done", "-s", "_unassigned", "-c", str(cfg)], client)
    assert result.exit_code == 2
    assert "invalid slug" in _combined(result)


def test_invalid_slug_with_slash_exits_2(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    client = _stub_client(completed=[], cancelled=[])
    result = _run(["regenerate-done", "-s", "Foo/bar", "-c", str(cfg)], client)
    assert result.exit_code == 2


def test_missing_obsidian_sync_exits_1(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, obsidian=False)
    client = _stub_client(completed=[], cancelled=[])
    result = _run(["regenerate-done", "-s", "demo", "-c", str(cfg)], client)
    assert result.exit_code == 1
    assert "obsidian_sync" in _combined(result)


def test_lithos_unreachable_exits_1(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    client.task_list.side_effect = OSError("connection refused")
    result = _run(["regenerate-done", "-s", "demo", "-c", str(cfg)], client)
    assert result.exit_code == 1
    assert "could not reach Lithos" in _combined(result)
    assert not _done_path(tmp_path, "demo").exists()
