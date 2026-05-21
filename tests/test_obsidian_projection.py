"""Tests for the obsidian-projection handler (Slice 1 US8).

Drives the handler directly with synthetic Events against a
tmp_path-based vault. Idempotency, atomic write, and rendering rules
are exercised here; end-to-end wiring through the obsidian-sync child
is covered in ``test_obsidian_sync_child.py``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from lithos_loom.bus import Event
from lithos_loom.config import (
    LoomConfig,
    ObsidianSyncConfig,
    OrchestratorConfig,
    RouteConfig,
    RouteMatch,
)
from lithos_loom.subscriptions._obsidian_projection import make_handler

# ── Fixtures ───────────────────────────────────────────────────────────


@dataclass
class _StubCtx:
    """Mimics the bits of SubscriptionContext the handler reads."""

    logger: logging.Logger


def _ctx() -> _StubCtx:
    return _StubCtx(logger=logging.getLogger("test.obsidian_projection"))


def _cfg(
    tmp_path: Path,
    *,
    routes: tuple[RouteConfig, ...] = (),
    include_blocked: bool = True,
    exclude_tags: tuple[str, ...] = (),
    tasks_file: Path = Path("_lithos/tasks.md"),
) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        routes=routes,
        obsidian_sync=ObsidianSyncConfig(
            vault_path=tmp_path,
            tasks_file=tasks_file,
            include_blocked=include_blocked,
            exclude_tags=exclude_tags,
        ),
    )


def _event(
    event_type: str,
    *,
    task_id: str,
    title: str = "test task",
    status: str = "open",
    tags: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
    claims: tuple[Mapping[str, Any], ...] = (),
) -> Event:
    return Event(
        type=event_type,
        timestamp=datetime.now(UTC),
        payload={
            "id": task_id,
            "title": title,
            "status": status,
            "tags": list(tags),
            "metadata": dict(metadata or {}),
            "claims": list(claims),
        },
    )


# ── Handler behaviour ──────────────────────────────────────────────────


async def test_created_event_for_actionable_task_writes_line(tmp_path: Path) -> None:
    """Orphan task (no matching route) is human-actionable → line appears."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(
        _event("lithos.task.created", task_id="abc", title="Review PR"), _ctx()
    )

    content = (tmp_path / "_lithos/tasks.md").read_text()
    assert "- [ ] Review PR 🆔 lithos:abc" in content


async def test_created_event_for_autonomous_task_writes_nothing(
    tmp_path: Path,
) -> None:
    """Autonomous-route task (human_blocking=False) doesn't actionably change
    the projection. We skip the write entirely rather than rewriting the
    file with the same content — the file only appears once an actionable
    task is seen. Keeps the operator's first signal clean ("a file
    appeared = there's something to do")."""
    routes = (
        RouteConfig(
            name="auto",
            command="echo",
            match=RouteMatch(tags=("trigger:auto",)),
            human_blocking=False,
        ),
    )
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg)
    await handler(
        _event("lithos.task.created", task_id="autonomous", tags=("trigger:auto",)),
        _ctx(),
    )

    # No actionable state change → no file written yet.
    assert not (tmp_path / "_lithos/tasks.md").exists()


async def test_updated_event_replaces_line_for_same_id(tmp_path: Path) -> None:
    """A title change on the same task replaces (not duplicates) the line.

    Note: the live LithosEventStream source doesn't actually emit
    lithos.task.updated today (verified at lithos_event_stream.py:63);
    the handler subscribes to it for forward-compat. The real runtime
    re-evaluation path is exercised by the claimed/released tests below.
    """
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(
        _event("lithos.task.created", task_id="t1", title="old title"), _ctx()
    )
    await handler(
        _event("lithos.task.updated", task_id="t1", title="new title"), _ctx()
    )

    content = (tmp_path / "_lithos/tasks.md").read_text()
    assert "new title" in content
    assert "old title" not in content
    assert content.count("🆔 lithos:t1") == 1


async def test_claimed_by_autonomous_route_drops_orphan_line(tmp_path: Path) -> None:
    """If a task that was a projected orphan gets claimed by an
    autonomous route, drop its line — automation now owns it."""
    routes = (
        RouteConfig(
            name="auto",
            command="echo",
            match=RouteMatch(tags=("trigger:auto",)),
            human_blocking=False,
        ),
    )
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg)
    # Initially orphan (actionable) → line appears.
    await handler(_event("lithos.task.created", task_id="t1", title="x"), _ctx())
    assert "🆔 lithos:t1" in (tmp_path / "_lithos/tasks.md").read_text()
    # Now claimed by the autonomous route → line removed.
    await handler(
        _event(
            "lithos.task.claimed",
            task_id="t1",
            title="x",
            tags=("trigger:auto",),
            claims=({"agent": "automation", "aspect": "auto"},),
        ),
        _ctx(),
    )
    assert "🆔 lithos:t1" not in (tmp_path / "_lithos/tasks.md").read_text()


async def test_claimed_by_human_blocking_route_promotes_task(tmp_path: Path) -> None:
    """D6's second disjunct in action: a task that was claimable-but-
    hidden (autonomous-route-claimable) becomes actionable the moment a
    human_blocking route claims it. This is the real runtime path the
    projection needs to react to — without it, US8 would never surface
    story-review-human tasks until they hit a created/updated event."""
    routes = (
        RouteConfig(
            name="review-human",
            command="echo",
            match=RouteMatch(tags=("trigger:review",)),
            human_blocking=True,
        ),
    )
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg)
    # Created: tag matches human_blocking route but no claim yet → hidden.
    await handler(
        _event(
            "lithos.task.created",
            task_id="rev",
            title="Review PR #42",
            tags=("trigger:review",),
        ),
        _ctx(),
    )
    assert not (tmp_path / "_lithos/tasks.md").exists()

    # Claimed by review-human → promote.
    await handler(
        _event(
            "lithos.task.claimed",
            task_id="rev",
            title="Review PR #42",
            tags=("trigger:review",),
            claims=({"agent": "loom", "aspect": "review-human"},),
        ),
        _ctx(),
    )
    assert (
        "- [ ] Review PR #42 🆔 lithos:rev"
        in (tmp_path / "_lithos/tasks.md").read_text()
    )


async def test_released_by_human_blocking_route_demotes_task(tmp_path: Path) -> None:
    """Inverse of the claim promotion: when the human_blocking route
    releases the claim and no other claim makes the task actionable,
    the line disappears."""
    routes = (
        RouteConfig(
            name="review-human",
            command="echo",
            match=RouteMatch(tags=("trigger:review",)),
            human_blocking=True,
        ),
    )
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg)
    # Created + claimed → projected.
    await handler(
        _event(
            "lithos.task.created",
            task_id="rev",
            tags=("trigger:review",),
        ),
        _ctx(),
    )
    await handler(
        _event(
            "lithos.task.claimed",
            task_id="rev",
            tags=("trigger:review",),
            claims=({"agent": "loom", "aspect": "review-human"},),
        ),
        _ctx(),
    )
    assert "lithos:rev" in (tmp_path / "_lithos/tasks.md").read_text()
    # Released → claims=() → no longer actionable → drop line.
    await handler(
        _event(
            "lithos.task.released",
            task_id="rev",
            tags=("trigger:review",),
            claims=(),
        ),
        _ctx(),
    )
    assert "lithos:rev" not in (tmp_path / "_lithos/tasks.md").read_text()


async def test_completed_event_removes_line(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="done"), _ctx())
    await handler(_event("lithos.task.completed", task_id="done"), _ctx())
    assert "🆔 lithos:done" not in (tmp_path / "_lithos/tasks.md").read_text()


async def test_cancelled_event_removes_line(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="cx"), _ctx())
    await handler(_event("lithos.task.cancelled", task_id="cx"), _ctx())
    assert "🆔 lithos:cx" not in (tmp_path / "_lithos/tasks.md").read_text()


async def test_title_with_newlines_collapsed_to_spaces(tmp_path: Path) -> None:
    """Multi-line titles would break the single-line markdown task syntax —
    collapse whitespace so the projection stays parseable."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(
        _event("lithos.task.created", task_id="nl", title="foo\nbar\tbaz"),
        _ctx(),
    )
    content = (tmp_path / "_lithos/tasks.md").read_text()
    assert "- [ ] foo bar baz 🆔 lithos:nl" in content
    # No newline mid-title.
    lines_with_id = [ln for ln in content.splitlines() if "lithos:nl" in ln]
    assert len(lines_with_id) == 1


async def test_atomic_write_uses_temp_then_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler must write to a .tmp file then os.replace onto the
    final path, not write the final path directly. Verifies the
    atomicity contract that survives crashes/partial reads."""
    real_replace = os.replace
    calls: list[tuple[str, str]] = []

    def _spy_replace(src: str | Path, dst: str | Path) -> None:
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy_replace)

    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="atomic"), _ctx())

    assert len(calls) == 1
    src, dst = calls[0]
    assert src.endswith("tasks.md.tmp")
    assert dst.endswith("tasks.md")
    # Final file exists; tmp file does not linger.
    assert (tmp_path / "_lithos/tasks.md").exists()
    assert not (tmp_path / "_lithos/tasks.md.tmp").exists()


async def test_parent_directory_created_when_absent(tmp_path: Path) -> None:
    """Vault exists but the _lithos/ subdirectory does not yet — the
    handler must create it on first write."""
    assert not (tmp_path / "_lithos").exists()
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="first"), _ctx())
    assert (tmp_path / "_lithos").is_dir()
    assert (tmp_path / "_lithos/tasks.md").is_file()


async def test_multiple_tasks_sorted_by_id_deterministic(tmp_path: Path) -> None:
    """Three tasks added in arbitrary order render in id-sorted order so
    file content is stable across runs (helpful for US14 dedup)."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="c", title="C"), _ctx())
    await handler(_event("lithos.task.created", task_id="a", title="A"), _ctx())
    await handler(_event("lithos.task.created", task_id="b", title="B"), _ctx())

    content = (tmp_path / "_lithos/tasks.md").read_text()
    task_lines = [ln for ln in content.splitlines() if ln.startswith("- [ ]")]
    assert task_lines == [
        "- [ ] A 🆔 lithos:a",
        "- [ ] B 🆔 lithos:b",
        "- [ ] C 🆔 lithos:c",
    ]


async def test_file_includes_auto_generated_header(tmp_path: Path) -> None:
    """First line of the file is a clear hand-off warning so a curious
    operator who opens the file sees it's machine-managed."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="t"), _ctx())
    content = (tmp_path / "_lithos/tasks.md").read_text()
    first_line = content.splitlines()[0]
    assert first_line.startswith("%%")
    assert "Auto-generated" in first_line


async def test_idempotent_repeated_event_yields_same_file(tmp_path: Path) -> None:
    """Replaying the same created event twice produces identical file
    content — necessary because the SSE source's bootstrap replays
    created events on every daemon restart."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="r"), _ctx())
    first = (tmp_path / "_lithos/tasks.md").read_text()
    await handler(_event("lithos.task.created", task_id="r"), _ctx())
    second = (tmp_path / "_lithos/tasks.md").read_text()
    assert first == second


# ── Copilot review fixes (#17) ─────────────────────────────────────────


async def test_removal_of_untracked_task_skips_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing a task that was never in state is a no-op — no disk I/O.
    Without this, every spurious completed/cancelled event for an
    autonomous task would touch the file's mtime and ripple through
    Obsidian Sync."""
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _spy(src: str | Path, dst: str | Path) -> None:
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    # Never created — straight to completed.
    await handler(_event("lithos.task.completed", task_id="ghost"), _ctx())
    assert calls == [], "completed-for-untracked should not have written"
    assert not (tmp_path / "_lithos/tasks.md").exists()


async def test_upsert_with_identical_line_skips_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replaying a created event for a task already in state yields the
    same rendered line — skip the write (Copilot review on #17). Also
    sets up US14's content-hash dedup work for later."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    await handler(_event("lithos.task.created", task_id="dup"), _ctx())

    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _spy(src: str | Path, dst: str | Path) -> None:
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    # Same event again — identical line, should not write.
    await handler(_event("lithos.task.created", task_id="dup"), _ctx())
    assert calls == [], "second identical event triggered a write"


async def test_unknown_event_type_no_op(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An event type outside the known sets is debug-logged and dropped
    — payload parsing is NOT attempted (Copilot review on #17 flagged
    that a foreign payload could raise KeyError on 'id' otherwise)."""
    import logging as _logging

    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    # Construct a foreign event with a payload that lacks 'id' — proof
    # the handler doesn't try to parse it.
    foreign = Event(
        type="obsidian.note.modified",
        timestamp=datetime.now(UTC),
        payload={"path": "/some/note.md"},
    )
    with caplog.at_level(_logging.DEBUG):
        await handler(foreign, _ctx())
    # No file written; no exception raised.
    assert not (tmp_path / "_lithos/tasks.md").exists()


async def test_malformed_task_payload_warns_no_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A task-typed event whose payload is missing 'id' (programming
    error somewhere upstream) should warn and drop, not crash the
    subscription loop."""
    import logging as _logging

    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    bad = Event(
        type="lithos.task.created",
        timestamp=datetime.now(UTC),
        payload={"title": "no id here"},
    )
    with caplog.at_level(_logging.WARNING):
        await handler(bad, _ctx())  # must not raise

    warns = [r.getMessage() for r in caplog.records if r.levelno == _logging.WARNING]
    assert any("malformed payload" in m for m in warns), warns
    assert not (tmp_path / "_lithos/tasks.md").exists()


async def test_atomic_write_cleans_up_tmp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace raises (disk full, perms flip, etc.) the .tmp file
    must NOT be left behind to litter the vault (Copilot review on #17,
    mirrors plugin_runner.write_result_atomically)."""

    def _failing_replace(src: str | Path, dst: str | Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _failing_replace)

    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)
    with pytest.raises(OSError, match="simulated replace failure"):
        await handler(_event("lithos.task.created", task_id="x"), _ctx())

    # The real file was never written, and the tmp file was cleaned up.
    assert not (tmp_path / "_lithos/tasks.md").exists()
    assert not (tmp_path / "_lithos/tasks.md.tmp").exists()


# ── US9 line enrichment: 📅 / #project/ / #lithos/ markers ──────────────


_TODAY = date(2026, 5, 20)


def _fixed_today() -> date:
    return _TODAY


def _human_blocking_route(name: str = "review-human") -> RouteConfig:
    return RouteConfig(
        name=name,
        command="echo",
        match=RouteMatch(tags=(f"trigger:{name}",)),
        human_blocking=True,
    )


def _projected_line(tmp_path: Path) -> str:
    """Read the single rendered task line from the projection file."""
    content = (tmp_path / "_lithos/tasks.md").read_text()
    lines = [ln for ln in content.splitlines() if ln.startswith("- [ ]")]
    assert len(lines) == 1, f"expected exactly one task line, got: {lines!r}"
    return lines[0]


# ── 📅 date marker ─────────────────────────────────────────────────────


async def test_orphan_open_task_renders_no_date_marker(tmp_path: Path) -> None:
    """Backlog tasks (orphan, no scheduled_for) emit no 📅 (D10)."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event("lithos.task.created", task_id="orph", title="Orphan task"), _ctx()
    )
    line = _projected_line(tmp_path)
    assert "📅" not in line
    assert line == "- [ ] Orphan task 🆔 lithos:orph"


async def test_human_blocking_claim_renders_today_date(tmp_path: Path) -> None:
    """A task claimed by a human_blocking route renders 📅 today."""
    routes = (_human_blocking_route(),)
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.claimed",
            task_id="hb",
            title="Human review",
            tags=("trigger:review-human",),
            claims=({"agent": "loom", "aspect": "review-human"},),
        ),
        _ctx(),
    )
    assert "📅 2026-05-20" in _projected_line(tmp_path)


async def test_scheduled_for_override_used_when_set(tmp_path: Path) -> None:
    """metadata.scheduled_for wins over the default-absent case for orphans."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.created",
            task_id="sched",
            title="Plan retro",
            metadata={"scheduled_for": "2026-06-15"},
        ),
        _ctx(),
    )
    assert "📅 2026-06-15" in _projected_line(tmp_path)


async def test_scheduled_for_override_used_for_human_blocking(
    tmp_path: Path,
) -> None:
    """scheduled_for beats the computed today for claimed tasks too."""
    routes = (_human_blocking_route(),)
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.claimed",
            task_id="hb",
            title="Review",
            tags=("trigger:review-human",),
            metadata={"scheduled_for": "2026-06-15"},
            claims=({"agent": "loom", "aspect": "review-human"},),
        ),
        _ctx(),
    )
    line = _projected_line(tmp_path)
    assert "📅 2026-06-15" in line
    assert "2026-05-20" not in line


async def test_scheduled_for_accepts_iso_datetime(tmp_path: Path) -> None:
    """Full ISO datetime values (Z-suffixed) are parsed down to a date."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.created",
            task_id="dt",
            metadata={"scheduled_for": "2026-06-15T09:00:00Z"},
        ),
        _ctx(),
    )
    assert "📅 2026-06-15" in _projected_line(tmp_path)


async def test_scheduled_for_malformed_falls_through_to_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unparseable scheduled_for is warn-and-drop; orphan → no 📅."""
    import logging as _logging

    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    with caplog.at_level(_logging.WARNING):
        await handler(
            _event(
                "lithos.task.created",
                task_id="bad",
                metadata={"scheduled_for": "yesterday"},
            ),
            _ctx(),
        )
    assert "📅" not in _projected_line(tmp_path)
    warns = [r.getMessage() for r in caplog.records if r.levelno == _logging.WARNING]
    assert any("scheduled_for" in m for m in warns), warns


# ── #project/<slug> tag ────────────────────────────────────────────────


async def test_metadata_project_renders_project_tag(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.created",
            task_id="p",
            title="Refactor",
            metadata={"project": "lithos-loom"},
        ),
        _ctx(),
    )
    assert "#project/lithos-loom" in _projected_line(tmp_path)


async def test_missing_metadata_project_omits_project_tag(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(_event("lithos.task.created", task_id="np"), _ctx())
    assert "#project/" not in _projected_line(tmp_path)


async def test_non_string_metadata_project_omits_project_tag(
    tmp_path: Path,
) -> None:
    """Defensive against schema drift — int/None project values are ignored."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.created",
            task_id="num",
            metadata={"project": 42},
        ),
        _ctx(),
    )
    assert "#project/" not in _projected_line(tmp_path)


# ── #lithos/<route-name> tag ───────────────────────────────────────────


async def test_human_blocking_claim_renders_lithos_route_tag(
    tmp_path: Path,
) -> None:
    routes = (_human_blocking_route(name="review-human"),)
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.claimed",
            task_id="hb",
            tags=("trigger:review-human",),
            claims=({"agent": "loom", "aspect": "review-human"},),
        ),
        _ctx(),
    )
    assert "#lithos/review-human" in _projected_line(tmp_path)


async def test_autonomous_claim_does_not_render_lithos_tag(tmp_path: Path) -> None:
    """A claim by an autonomous route never produces a #lithos/ tag.

    Such tasks aren't even actionable (per D6 they'd be hidden), so the
    only way this gets exercised is the path where a task is human-
    actionable for another reason and one of its claims is autonomous.
    Orphan + autonomous-route-name in unrelated config: still no tag.
    """
    routes = (
        RouteConfig(
            name="auto",
            command="echo",
            match=RouteMatch(tags=("trigger:auto",)),
            human_blocking=False,
        ),
    )
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg, today_provider=_fixed_today)
    # Orphan (its tags don't intersect the route) but claim aspect
    # collides with the autonomous route name — must NOT produce a
    # #lithos/auto tag because that route is not human_blocking.
    await handler(
        _event(
            "lithos.task.created",
            task_id="o",
            title="Orphan",
            tags=("unrelated",),
            claims=({"agent": "x", "aspect": "auto"},),
        ),
        _ctx(),
    )
    line = _projected_line(tmp_path)
    assert "#lithos/" not in line


async def test_orphan_task_no_lithos_tag(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(_event("lithos.task.created", task_id="orph"), _ctx())
    assert "#lithos/" not in _projected_line(tmp_path)


async def test_multiple_claims_first_human_blocking_wins(tmp_path: Path) -> None:
    """When two human_blocking routes exist and the task carries claims
    from both, the first claim (Lithos-canonical order) wins so the
    rendered tag is stable across event re-runs."""
    routes = (
        _human_blocking_route(name="review-human"),
        _human_blocking_route(name="signoff"),
    )
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.claimed",
            task_id="dual",
            tags=("trigger:review-human", "trigger:signoff"),
            claims=(
                {"agent": "a", "aspect": "review-human"},
                {"agent": "b", "aspect": "signoff"},
            ),
        ),
        _ctx(),
    )
    line = _projected_line(tmp_path)
    assert "#lithos/review-human" in line
    assert "#lithos/signoff" not in line


# ── Composition + ordering ─────────────────────────────────────────────


async def test_full_marker_set_orders_correctly(tmp_path: Path) -> None:
    """All markers present in expected order (after US10):

    - [ ] <title> <priority> 🆔 lithos:<id> 📅 <date> #project/<slug> #lithos/<route>
    """
    routes = (_human_blocking_route(name="review-human"),)
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.claimed",
            task_id="full",
            title="Review PR for story 03",
            tags=("trigger:review-human",),
            metadata={
                "project": "lithos-loom",
                "scheduled_for": "2026-06-15",
                "priority": "high",
            },
            claims=({"agent": "loom", "aspect": "review-human"},),
        ),
        _ctx(),
    )
    assert _projected_line(tmp_path) == (
        "- [ ] Review PR for story 03 ⏫ 🆔 lithos:full "
        "📅 2026-06-15 #project/lithos-loom #lithos/review-human"
    )


async def test_no_double_space_when_optional_markers_omitted(
    tmp_path: Path,
) -> None:
    """US8-shape orphan task — no trailing whitespace, no double-space."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event("lithos.task.created", task_id="bare", title="bare task"), _ctx()
    )
    line = _projected_line(tmp_path)
    assert line == "- [ ] bare task 🆔 lithos:bare"
    assert "  " not in line


# ── Idempotency under US9 ──────────────────────────────────────────────


async def test_repeated_event_with_fixed_today_yields_skip_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a pinned today_provider, the second identical claimed event
    produces the same rendered line and hits the skip-write fast path."""
    routes = (_human_blocking_route(),)
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg, today_provider=_fixed_today)

    event = _event(
        "lithos.task.claimed",
        task_id="r",
        tags=("trigger:review-human",),
        metadata={"project": "lithos-loom"},
        claims=({"agent": "loom", "aspect": "review-human"},),
    )
    await handler(event, _ctx())

    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _spy(src: str | Path, dst: str | Path) -> None:
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    await handler(event, _ctx())
    assert calls == [], "second identical claimed event triggered a write"


# ── US10 priority emoji ─────────────────────────────────────────────────


_PRIORITY_EMOJI_CHARS = "🔺⏫🔼🔽⏬"


async def test_priority_highest_renders_red_triangle(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event("lithos.task.created", task_id="p", metadata={"priority": "highest"}),
        _ctx(),
    )
    assert "🔺" in _projected_line(tmp_path)


async def test_priority_high_renders_double_up(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event("lithos.task.created", task_id="p", metadata={"priority": "high"}),
        _ctx(),
    )
    assert "⏫" in _projected_line(tmp_path)


async def test_priority_medium_renders_up(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event("lithos.task.created", task_id="p", metadata={"priority": "medium"}),
        _ctx(),
    )
    assert "🔼" in _projected_line(tmp_path)


async def test_priority_low_renders_down(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event("lithos.task.created", task_id="p", metadata={"priority": "low"}),
        _ctx(),
    )
    assert "🔽" in _projected_line(tmp_path)


async def test_priority_lowest_renders_double_down(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event("lithos.task.created", task_id="p", metadata={"priority": "lowest"}),
        _ctx(),
    )
    assert "⏬" in _projected_line(tmp_path)


async def test_missing_priority_omits_emoji(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(_event("lithos.task.created", task_id="np"), _ctx())
    line = _projected_line(tmp_path)
    assert not any(ch in line for ch in _PRIORITY_EMOJI_CHARS)


async def test_unknown_priority_warns_and_omits(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Drift-protection: an enum value Lithos doesn't define is logged and
    dropped, never crashes the subscription loop."""
    import logging as _logging

    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    with caplog.at_level(_logging.WARNING):
        await handler(
            _event(
                "lithos.task.created",
                task_id="u",
                metadata={"priority": "urgent"},
            ),
            _ctx(),
        )
    line = _projected_line(tmp_path)
    assert not any(ch in line for ch in _PRIORITY_EMOJI_CHARS)
    warns = [r.getMessage() for r in caplog.records if r.levelno == _logging.WARNING]
    assert any("priority" in m and "urgent" in m for m in warns), warns


async def test_non_string_priority_warns_and_omits(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Defensive against schema drift — an int / dict / None for priority
    is dropped with a warning, never crashes."""
    import logging as _logging

    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    with caplog.at_level(_logging.WARNING):
        await handler(
            _event("lithos.task.created", task_id="ns", metadata={"priority": 42}),
            _ctx(),
        )
    line = _projected_line(tmp_path)
    assert not any(ch in line for ch in _PRIORITY_EMOJI_CHARS)
    warns = [r.getMessage() for r in caplog.records if r.levelno == _logging.WARNING]
    assert any("priority" in m and "42" in m for m in warns), warns


async def test_priority_slots_between_title_and_id(tmp_path: Path) -> None:
    """Position lock: priority emoji appears after <title> and before
    🆔 lithos:<id>, matching the PRD's rendered example."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.created",
            task_id="slot",
            title="Slotted task",
            metadata={"priority": "high"},
        ),
        _ctx(),
    )
    assert _projected_line(tmp_path) == "- [ ] Slotted task ⏫ 🆔 lithos:slot"
