"""Tests for the obsidian-projection handler (Slice 1 US8).

Drives the handler directly with synthetic Events against a
tmp_path-based vault. Idempotency, atomic write, and rendering rules
are exercised here; end-to-end wiring through the obsidian-sync child
is covered in ``test_obsidian_sync_child.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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

    lithos.task.updated arrives whenever the upstream task is edited
    (lithos#283 / PR #284). The source force-refreshes for that event
    type so the payload carries the post-edit task snapshot; the
    projection then re-renders with the new field values.
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


async def test_updated_event_with_identical_content_skips_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lithos#283 + US14 compose: an updated event whose rendered line
    is byte-identical to the prior write is content-hash-deduped and
    no second write happens. This guards the common case where the
    upstream edit changed a field loom doesn't render (e.g. description)
    — Obsidian Sync shouldn't ripple a no-op mtime touch to other
    devices."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg)

    # First event writes the file once.
    await handler(
        _event("lithos.task.created", task_id="t1", title="same title"), _ctx()
    )
    tasks_file = tmp_path / "_lithos/tasks.md"
    assert tasks_file.exists()
    first_mtime = tasks_file.stat().st_mtime_ns

    # Spy on the atomic-write helper to count writes precisely; mtime
    # snapshotting is also captured for the cross-check.
    from lithos_loom.subscriptions import _obsidian_projection as proj

    write_calls: list[Path] = []
    real_write = proj.write_file_atomic

    async def _spy_write(path: Path, content: str) -> None:
        write_calls.append(path)
        await real_write(path, content)

    monkeypatch.setattr(proj, "write_file_atomic", _spy_write)

    # Updated event with the SAME title → render produces an identical
    # line → content-hash matches last_written_hash → no write.
    await handler(
        _event("lithos.task.updated", task_id="t1", title="same title"), _ctx()
    )

    assert write_calls == [], (
        f"identical-content update should not write, got writes: {write_calls}"
    )
    # Belt + braces: mtime did not move.
    assert tasks_file.stat().st_mtime_ns == first_mtime


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
    """Read the single rendered task line from the projection file.

    Matches all three checkbox states emitted by the renderer:
    ``- [ ]`` (open, US8+), ``- [x]`` (completed, US13),
    ``- [-]`` (cancelled, US13).
    """
    content = (tmp_path / "_lithos/tasks.md").read_text()
    lines = [
        ln for ln in content.splitlines() if ln.startswith(("- [ ]", "- [x]", "- [-]"))
    ]
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
    """All markers present in expected order (after US11):

    - [ ] <title> <priority> 🆔 lithos:<id> ⛔ lithos:<dep> \
        📅 <date> #project/<slug> #lithos/<route>
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
                "depends_on": ["dep-456"],
            },
            claims=({"agent": "loom", "aspect": "review-human"},),
        ),
        _ctx(),
    )
    # Tasks-plugin layout: title → 🆔 → tags → trailing emoji metadata
    # (deps → priority → date). See render.py docstring.
    assert _projected_line(tmp_path) == (
        "- [ ] Review PR for story 03 🆔 lithos:full "
        "#project/lithos-loom #lithos/review-human "
        "⛔ lithos:dep-456 ⏫ 📅 2026-06-15"
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
    # Priority emoji at the END — see render.py docstring.
    assert _projected_line(tmp_path) == "- [ ] Slotted task 🆔 lithos:slot ⏫"


# ── US11 dependency markers ────────────────────────────────────────────


async def test_single_dep_renders_one_marker(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.created",
            task_id="t",
            metadata={"depends_on": ["dep-1"]},
        ),
        _ctx(),
    )
    assert "⛔ lithos:dep-1" in _projected_line(tmp_path)


async def test_multiple_deps_render_one_marker_each_in_order(
    tmp_path: Path,
) -> None:
    """List order is preserved — D19 says dep order has graph meaning
    upstream; reshuffling would lose that signal."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.created",
            task_id="t",
            title="multi",
            metadata={"depends_on": ["dep-a", "dep-b", "dep-c"]},
        ),
        _ctx(),
    )
    assert _projected_line(tmp_path) == (
        "- [ ] multi 🆔 lithos:t ⛔ lithos:dep-a ⛔ lithos:dep-b ⛔ lithos:dep-c"
    )


async def test_dep_markers_slot_between_id_and_date(tmp_path: Path) -> None:
    """Position lock: ⛔ markers appear after 🆔 lithos:<id> and before
    📅 <date>, matching the PRD's rendered example."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.created",
            task_id="s",
            title="slotted",
            metadata={"depends_on": ["dep-1"], "scheduled_for": "2026-06-15"},
        ),
        _ctx(),
    )
    assert _projected_line(tmp_path) == (
        "- [ ] slotted 🆔 lithos:s ⛔ lithos:dep-1 📅 2026-06-15"
    )


async def test_missing_depends_on_omits_dep_markers(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(_event("lithos.task.created", task_id="nd"), _ctx())
    assert "⛔" not in _projected_line(tmp_path)


async def test_empty_depends_on_omits_dep_markers(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event("lithos.task.created", task_id="e", metadata={"depends_on": []}),
        _ctx(),
    )
    assert "⛔" not in _projected_line(tmp_path)


async def test_non_list_depends_on_warns_and_omits(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Defensive: a string-by-mistake (instead of list of strings) is
    warn-logged and dropped, never crashes the subscription loop."""
    import logging as _logging

    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    with caplog.at_level(_logging.WARNING):
        await handler(
            _event(
                "lithos.task.created",
                task_id="nl",
                metadata={"depends_on": "dep-a"},
            ),
            _ctx(),
        )
    assert "⛔" not in _projected_line(tmp_path)
    warns = [r.getMessage() for r in caplog.records if r.levelno == _logging.WARNING]
    assert any("depends_on" in m and "dep-a" in m for m in warns), warns


async def test_non_string_entries_skipped_with_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Mixed list — only string entries become markers; bad entries get
    one consolidated warning."""
    import logging as _logging

    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    with caplog.at_level(_logging.WARNING):
        await handler(
            _event(
                "lithos.task.created",
                task_id="m",
                title="mixed",
                metadata={"depends_on": ["dep-a", 42, None, "", "dep-b"]},
            ),
            _ctx(),
        )
    assert _projected_line(tmp_path) == (
        "- [ ] mixed 🆔 lithos:m ⛔ lithos:dep-a ⛔ lithos:dep-b"
    )
    warns = [r.getMessage() for r in caplog.records if r.levelno == _logging.WARNING]
    assert any("depends_on" in m and "invalid" in m for m in warns), warns


async def test_duplicate_dep_ids_deduped_preserving_first_occurrence(
    tmp_path: Path,
) -> None:
    """Two ⛔ markers for the same dep is visual noise; emit once at the
    first-occurrence position."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.created",
            task_id="d",
            title="dup",
            metadata={"depends_on": ["dep-a", "dep-b", "dep-a"]},
        ),
        _ctx(),
    )
    assert _projected_line(tmp_path) == (
        "- [ ] dup 🆔 lithos:d ⛔ lithos:dep-a ⛔ lithos:dep-b"
    )


# ── US13 resolved-task TTL lingering ───────────────────────────────────


class _Missing:
    """Sentinel marker — distinguishes "argument not passed" from
    "explicitly passed ``None``" in ``_resolved_event``."""


_MISSING = _Missing()


def _local_noon(d: date) -> datetime:
    """Build a tz-aware datetime anchored at noon-local on ``d``.

    Used for both ``Event.timestamp`` and ``payload["resolved_at"]`` so
    ``.astimezone().date()`` round-trips back to ``d`` regardless of
    host timezone (the previous UTC-noon helper flipped dates on
    +13/+14 offsets — Copilot review on PR #21).
    """
    return datetime(d.year, d.month, d.day, 12, 0, 0).astimezone()


def _resolved_event(
    event_type: str,
    *,
    task_id: str,
    title: str = "test task",
    when: date = _TODAY,
    resolved_at: date | None | _Missing = _MISSING,
    tags: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
    claims: tuple[Mapping[str, Any], ...] = (),
) -> Event:
    """Build a completed/cancelled event for the obsidian-projection handler.

    Sets payload ``resolved_at`` to ``_local_noon(when)`` by default —
    the handler's canonical resolution-date source (lithos#286 / PR
    #288 renamed Lithos's column from completed_at to resolved_at; both
    complete_task and cancel_task now write it).
    Pass ``resolved_at=None`` to exercise the fallback-to-event-timestamp
    path; pass ``resolved_at=<date>`` to anchor it elsewhere than
    ``when`` (e.g. for "payload wins over event timestamp" tests).
    """
    resolved_at_iso: str | None
    if isinstance(resolved_at, _Missing):
        resolved_at_iso = _local_noon(when).isoformat()
    elif resolved_at is None:
        resolved_at_iso = None
    else:
        resolved_at_iso = _local_noon(resolved_at).isoformat()
    return Event(
        type=event_type,
        timestamp=_local_noon(when),
        payload={
            "id": task_id,
            "title": title,
            "status": "completed" if event_type.endswith("completed") else "cancelled",
            "tags": list(tags),
            "metadata": dict(metadata or {}),
            "claims": list(claims),
            "resolved_at": resolved_at_iso,
        },
    )


# Resolved-line rendering


async def test_completed_event_renders_x_line_with_check_mark(
    tmp_path: Path,
) -> None:
    """Completed event on an open task → [x] line with ✅ <date>."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event("lithos.task.created", task_id="d", title="ship feature"), _ctx()
    )
    await handler(
        _resolved_event(
            "lithos.task.completed", task_id="d", title="ship feature", when=_TODAY
        ),
        _ctx(),
    )
    assert _projected_line(tmp_path) == ("- [x] ship feature 🆔 lithos:d ✅ 2026-05-20")


async def test_cancelled_event_renders_dash_line_with_x_mark(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event("lithos.task.created", task_id="c", title="abandon idea"), _ctx()
    )
    await handler(
        _resolved_event(
            "lithos.task.cancelled", task_id="c", title="abandon idea", when=_TODAY
        ),
        _ctx(),
    )
    assert _projected_line(tmp_path) == ("- [-] abandon idea 🆔 lithos:c ❌ 2026-05-20")


async def test_resolved_line_preserves_project_tag(tmp_path: Path) -> None:
    """metadata.project is the one decoration kept on resolved lines —
    so 'done this week for project X' queries still cluster correctly."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _event(
            "lithos.task.created",
            task_id="p",
            title="ship",
            metadata={"project": "cardinal"},
        ),
        _ctx(),
    )
    await handler(
        _resolved_event(
            "lithos.task.completed",
            task_id="p",
            title="ship",
            when=_TODAY,
            metadata={"project": "cardinal"},
        ),
        _ctx(),
    )
    assert _projected_line(tmp_path) == (
        "- [x] ship 🆔 lithos:p #project/cardinal ✅ 2026-05-20"
    )


async def test_resolved_line_omits_priority_dep_due_route_markers(
    tmp_path: Path,
) -> None:
    """A task with the full open-line marker set, when completed,
    renders the minimal resolved shape — priority emoji, ⛔ deps, 📅,
    and #lithos/<route> all drop because they are actionability-only."""
    routes = (_human_blocking_route(name="review-human"),)
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg, today_provider=_fixed_today)
    # First land the task as open with everything set.
    await handler(
        _event(
            "lithos.task.claimed",
            task_id="full",
            title="rich task",
            tags=("trigger:review-human",),
            metadata={
                "project": "p1",
                "priority": "high",
                "depends_on": ["dep-1"],
                "scheduled_for": "2026-06-15",
            },
            claims=({"agent": "loom", "aspect": "review-human"},),
        ),
        _ctx(),
    )
    # Then complete it.
    await handler(
        _resolved_event(
            "lithos.task.completed",
            task_id="full",
            title="rich task",
            when=_TODAY,
            metadata={
                "project": "p1",
                "priority": "high",
                "depends_on": ["dep-1"],
                "scheduled_for": "2026-06-15",
            },
        ),
        _ctx(),
    )
    line = _projected_line(tmp_path)
    assert line == "- [x] rich task 🆔 lithos:full #project/p1 ✅ 2026-05-20"
    # Belt-and-braces: explicitly none of the actionability markers.
    assert not any(ch in line for ch in "🔺⏫🔼🔽⏬")
    assert "⛔" not in line
    assert "📅" not in line
    assert "#lithos/" not in line


async def test_completed_event_for_untracked_orphan_creates_resolved_entry(
    tmp_path: Path,
) -> None:
    """Behaviour change vs the deleted US8 test: a completed event for an
    untracked task is no longer always a no-op. When ``would_be_actionable``
    is True (orphan with no matching route → first D6 disjunct), the
    event adds a fresh resolved entry — this is the path the
    bootstrap-resolved restart recovery (LithosEventStream
    ``bootstrap_resolved_window``) relies on to rehydrate Monday's
    completed tasks into Wednesday's projection."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _resolved_event(
            "lithos.task.completed",
            task_id="orph",
            title="orphan done",
            when=_TODAY,
        ),
        _ctx(),
    )
    assert _projected_line(tmp_path) == (
        "- [x] orphan done 🆔 lithos:orph ✅ 2026-05-20"
    )


async def test_completed_event_for_autonomous_route_task_does_not_project(
    tmp_path: Path,
) -> None:
    """PR #21 review issue 2: terminal events must respect D6 the same
    way live re-evaluation does. An autonomous-route task that was
    hidden while open should NOT appear in the resolved-task projection
    just because Lithos transitioned it to completed."""
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
    await handler(
        _resolved_event(
            "lithos.task.completed",
            task_id="auto-done",
            title="automation ran",
            tags=("trigger:auto",),
            when=_TODAY,
        ),
        _ctx(),
    )
    assert not (tmp_path / "_lithos/tasks.md").exists(), (
        "autonomous-route task that completed must NOT join the projection"
    )


async def test_completed_event_for_human_blocking_claim_promotes(
    tmp_path: Path,
) -> None:
    """A task with tags matching a human_blocking route AND a claim by
    that route (D6 second disjunct) is would-be-actionable and should
    project as resolved on completion."""
    routes = (_human_blocking_route(name="review-human"),)
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _resolved_event(
            "lithos.task.completed",
            task_id="hb-done",
            title="reviewed",
            tags=("trigger:review-human",),
            claims=({"agent": "loom", "aspect": "review-human"},),
            when=_TODAY,
        ),
        _ctx(),
    )
    assert "lithos:hb-done" in (tmp_path / "_lithos/tasks.md").read_text()


async def test_resolved_at_prefers_payload_resolved_at_over_event_timestamp(
    tmp_path: Path,
) -> None:
    """PR #21 review issue 3: Lithos's canonical ``resolved_at`` is the
    truth for the resolution date. Event.timestamp is "when Loom
    received the event", which drifts under reconnect/replay/restart.
    When both are present and differ, the payload wins."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    # Event "received" today, but Lithos says the task was resolved
    # three days ago. Render the canonical date, not today.
    three_days_ago = _TODAY - timedelta(days=3)
    await handler(
        _resolved_event(
            "lithos.task.completed",
            task_id="canon",
            title="canonical",
            when=_TODAY,
            resolved_at=three_days_ago,
        ),
        _ctx(),
    )
    line = _projected_line(tmp_path)
    assert "✅ 2026-05-17" in line, line
    assert "2026-05-20" not in line, line


async def test_resolved_at_falls_back_to_event_timestamp_when_payload_silent(
    tmp_path: Path,
) -> None:
    """Backwards-compat: an older SSE source / replay path that doesn't
    include ``resolved_at`` in the payload falls back to
    ``event.timestamp``. The marker is still rendered (this path is
    less canonical but doesn't crash)."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _resolved_event(
            "lithos.task.completed",
            task_id="fb",
            title="fallback",
            when=_TODAY,
            resolved_at=None,  # explicit: payload has no resolved_at
        ),
        _ctx(),
    )
    assert "✅ 2026-05-20" in _projected_line(tmp_path)


# TTL lingering / eviction


async def test_resolved_task_lingers_within_ttl(tmp_path: Path) -> None:
    """Default TTL = 7. Completed today → still in file today."""
    cfg = _cfg(tmp_path)  # resolved_ttl_days default = 7
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _resolved_event("lithos.task.completed", task_id="x", title="t", when=_TODAY),
        _ctx(),
    )
    assert "lithos:x" in (tmp_path / "_lithos/tasks.md").read_text()


async def test_resolved_task_evicted_after_ttl_via_next_event(
    tmp_path: Path,
) -> None:
    """Task A completed 10 days ago, TTL = 7. State carries it. An
    unrelated event for task B arrives today → A is evicted by the
    sweep at the top of the handle, and the file is rewritten without
    A even though the triggering event was about B."""
    cfg = _cfg(tmp_path)  # ttl_days = 7
    handler = make_handler(cfg, today_provider=_fixed_today)
    # Seed: complete task A 10 days ago.
    ten_days_ago = _TODAY - timedelta(days=10)
    await handler(
        _resolved_event(
            "lithos.task.completed", task_id="a", title="old done", when=ten_days_ago
        ),
        _ctx(),
    )
    assert "lithos:a" in (tmp_path / "_lithos/tasks.md").read_text()
    # Now a fresh open event for B arrives today.
    await handler(
        _event("lithos.task.created", task_id="b", title="new open"),
        _ctx(),
    )
    text = (tmp_path / "_lithos/tasks.md").read_text()
    assert "lithos:a" not in text, "old completed task should be evicted by TTL sweep"
    assert "lithos:b" in text


async def test_resolved_task_at_ttl_boundary_still_lingers(tmp_path: Path) -> None:
    """Completed exactly 7 days ago, TTL = 7. cutoff = today-7;
    resolved_at == cutoff is NOT < cutoff, so the entry survives."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    boundary = _TODAY - timedelta(days=7)
    await handler(
        _resolved_event(
            "lithos.task.completed", task_id="edge", title="t", when=boundary
        ),
        _ctx(),
    )
    # Trigger a sweep via an unrelated event.
    await handler(_event("lithos.task.created", task_id="trigger", title="t"), _ctx())
    assert "lithos:edge" in (tmp_path / "_lithos/tasks.md").read_text()


async def test_resolved_task_one_day_past_ttl_evicted(tmp_path: Path) -> None:
    """Completed 8 days ago, TTL = 7 → evicted on next event handle."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    past = _TODAY - timedelta(days=8)
    await handler(
        _resolved_event("lithos.task.completed", task_id="gone", title="t", when=past),
        _ctx(),
    )
    await handler(_event("lithos.task.created", task_id="trigger", title="t"), _ctx())
    assert "lithos:gone" not in (tmp_path / "_lithos/tasks.md").read_text()


async def test_ttl_zero_evicts_immediately_on_next_event(tmp_path: Path) -> None:
    """ttl=0 means cutoff == today; an entry resolved_at < today gets
    evicted. An entry resolved today survives (resolved_at == cutoff)
    — operator can still see things completed in the current day
    until the next day rolls over."""
    cfg = LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        routes=(),
        obsidian_sync=ObsidianSyncConfig(
            vault_path=tmp_path,
            tasks_file=Path("_lithos/tasks.md"),
            resolved_ttl_days=0,
        ),
    )
    handler = make_handler(cfg, today_provider=_fixed_today)
    # Resolved yesterday → cutoff is today, yesterday < today → evicted.
    yesterday = _TODAY - timedelta(days=1)
    await handler(
        _resolved_event(
            "lithos.task.completed", task_id="dead", title="t", when=yesterday
        ),
        _ctx(),
    )
    await handler(_event("lithos.task.created", task_id="trigger", title="t"), _ctx())
    assert "lithos:dead" not in (tmp_path / "_lithos/tasks.md").read_text()


# State immunity for resolved entries


async def test_claimed_event_on_resolved_task_does_not_change_line(
    tmp_path: Path,
) -> None:
    """Once resolved, terminal status is final — a stale claimed event
    (e.g. from a delayed retry by a route) must not rewrite the line."""
    routes = (_human_blocking_route(name="review-human"),)
    cfg = _cfg(tmp_path, routes=routes)
    handler = make_handler(cfg, today_provider=_fixed_today)
    # Resolve it.
    await handler(
        _resolved_event(
            "lithos.task.completed", task_id="done", title="t", when=_TODAY
        ),
        _ctx(),
    )
    before = (tmp_path / "_lithos/tasks.md").read_text()
    # Stale claimed event arrives for the same task.
    await handler(
        _event(
            "lithos.task.claimed",
            task_id="done",
            tags=("trigger:review-human",),
            claims=({"agent": "loom", "aspect": "review-human"},),
        ),
        _ctx(),
    )
    after = (tmp_path / "_lithos/tasks.md").read_text()
    assert before == after, "resolved-state should be immune to claimed event"


async def test_updated_event_on_resolved_task_does_not_change_line(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(
        _resolved_event(
            "lithos.task.completed", task_id="done", title="t", when=_TODAY
        ),
        _ctx(),
    )
    before = (tmp_path / "_lithos/tasks.md").read_text()
    await handler(
        _event(
            "lithos.task.updated",
            task_id="done",
            title="t",
            metadata={"priority": "high"},
        ),
        _ctx(),
    )
    after = (tmp_path / "_lithos/tasks.md").read_text()
    assert before == after, "resolved-state should be immune to updated event"


# Interleaving + idempotency


async def test_open_and_resolved_tasks_sort_by_id_together(
    tmp_path: Path,
) -> None:
    """Mixed open + resolved entries in state → file output sorts by
    task id; the user's Tasks-plugin queries differentiate via the
    checkbox state."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(_event("lithos.task.created", task_id="a-open", title="A"), _ctx())
    await handler(_event("lithos.task.created", task_id="c-open", title="C"), _ctx())
    await handler(
        _resolved_event(
            "lithos.task.completed", task_id="b-done", title="B", when=_TODAY
        ),
        _ctx(),
    )
    content = (tmp_path / "_lithos/tasks.md").read_text()
    task_lines = [
        ln for ln in content.splitlines() if ln.startswith(("- [ ]", "- [x]", "- [-]"))
    ]
    assert task_lines == [
        "- [ ] A 🆔 lithos:a-open",
        "- [x] B 🆔 lithos:b-done ✅ 2026-05-20",
        "- [ ] C 🆔 lithos:c-open",
    ]


async def test_idempotent_repeat_completed_event_skips_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replaying the same completed event twice produces an identical
    _StateEntry → skip-write triggers on the second call."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    event = _resolved_event(
        "lithos.task.completed", task_id="dup", title="t", when=_TODAY
    )
    await handler(event, _ctx())

    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _spy(src: str | Path, dst: str | Path) -> None:
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    await handler(event, _ctx())
    assert calls == [], "second identical completed event should skip the write"


# ── US14 content-hash dedup ────────────────────────────────────────────


def _spy_os_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str]]:
    """Install an os.replace spy that records (src, dst) pairs while
    still performing the actual replace. Returns the recording list."""
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _spy(src: str | Path, dst: str | Path) -> None:
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    return calls


def _read_handler_closure(handler: Any, name: str) -> Any:
    """Read a closure variable from a handler returned by make_handler.

    Reaches into Python-private state (``__code__.co_freevars`` /
    ``__closure__``) — only suitable for verifying internal invariants
    that can't be observed via the public event API. Specifically,
    ``last_written_hash`` is updated AFTER ``write_file_atomic``
    returns, so a failed write must leave it untouched; the same-
    content retry path that would otherwise prove this hits the
    in-memory ``current == prior`` short-circuit and never reaches
    the hash check.
    """
    freevars = handler.__code__.co_freevars
    closure = handler.__closure__ or ()
    for cell_name, cell in zip(freevars, closure, strict=True):
        if cell_name == name:
            return cell.cell_contents
    raise AssertionError(f"handler does not close over {name!r} (freevars: {freevars})")


# Disk-seed behaviour


async def test_handler_init_seeds_hash_from_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler constructed against a vault whose projection file
    already contains exactly the content we'd render next must NOT
    write. The seed reads the file's current bytes and skips the
    first convergent write — restart-idempotency at the file
    boundary (US14)."""
    # First handler: write a known projection.
    cfg = _cfg(tmp_path)
    handler_a = make_handler(cfg, today_provider=_fixed_today)
    await handler_a(
        _event("lithos.task.created", task_id="seed", title="seeded"), _ctx()
    )
    disk_content = (tmp_path / "_lithos/tasks.md").read_text()
    assert "lithos:seed" in disk_content

    # Second handler: brand-new state, but file already on disk.
    # Replay the same created event — final render should match disk
    # → disk-seed should fire → zero writes.
    handler_b = make_handler(cfg, today_provider=_fixed_today)
    calls = _spy_os_replace(monkeypatch)
    await handler_b(
        _event("lithos.task.created", task_id="seed", title="seeded"), _ctx()
    )
    assert calls == [], (
        "convergent replay against disk-seeded handler should write zero times; "
        f"got {calls}"
    )


async def test_handler_init_no_seed_when_file_missing(tmp_path: Path) -> None:
    """Cold-start case: vault dir exists but projection file doesn't.
    last_written_hash starts None; first event writes normally and
    seeds the hash from new content."""
    cfg = _cfg(tmp_path)
    assert not (tmp_path / "_lithos/tasks.md").exists()
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(_event("lithos.task.created", task_id="cold"), _ctx())
    assert (tmp_path / "_lithos/tasks.md").exists()
    assert "lithos:cold" in (tmp_path / "_lithos/tasks.md").read_text()


async def test_handler_init_no_seed_when_file_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permission flip / weird FS state: seed read raises OSError →
    last_written_hash stays None → first write happens normally.
    Init must NOT crash."""

    def _explode(self: Path) -> bytes:
        raise PermissionError("simulated permission flip")

    monkeypatch.setattr(Path, "read_bytes", _explode)

    cfg = _cfg(tmp_path)
    # make_handler must not raise even though _hash_existing_file's
    # disk read would.
    handler = make_handler(cfg, today_provider=_fixed_today)
    # First write should happen normally (no seeded hash to compare).
    await handler(_event("lithos.task.created", task_id="perm"), _ctx())
    # Stop intercepting so the read-back works.
    monkeypatch.undo()
    assert "lithos:perm" in (tmp_path / "_lithos/tasks.md").read_text()


# Content-hash dedup at runtime


async def test_write_skipped_when_content_matches_disk_seeded_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct test of the disk-seed path: write some content via
    handler A, tear down. Build handler B against the same vault —
    its ``last_written_hash`` is seeded from the existing on-disk
    file via ``_hash_existing_file``. Replay the same event → render
    matches seed → no write happens."""
    cfg = _cfg(tmp_path)
    handler_a = make_handler(cfg, today_provider=_fixed_today)
    await handler_a(_event("lithos.task.created", task_id="seed"), _ctx())

    handler_b = make_handler(cfg, today_provider=_fixed_today)
    calls = _spy_os_replace(monkeypatch)
    await handler_b(_event("lithos.task.created", task_id="seed"), _ctx())
    assert calls == [], (
        "single-task replay against disk-seeded handler should skip the write"
    )


async def test_write_happens_when_state_change_produces_different_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(_event("lithos.task.created", task_id="a"), _ctx())
    calls = _spy_os_replace(monkeypatch)
    await handler(_event("lithos.task.created", task_id="b"), _ctx())
    assert len(calls) == 1, (
        f"adding a different task should have written once; got {calls}"
    )


async def test_last_written_hash_updates_after_successful_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write A then write A+B then write A+B again. The third call's
    content matches the second write's hash → skipped. If the
    last_written_hash hadn't been updated after the second write,
    the third call would NOT be skipped."""
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today)
    await handler(_event("lithos.task.created", task_id="a"), _ctx())
    await handler(_event("lithos.task.created", task_id="b"), _ctx())
    calls = _spy_os_replace(monkeypatch)
    # Replay the second event — identical state, identical content.
    await handler(_event("lithos.task.created", task_id="b"), _ctx())
    assert calls == [], "replay of last write should be hash-skipped"


async def test_atomic_write_failure_does_not_advance_last_written_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Internal invariant: when ``os.replace`` raises mid-write, the
    coordination state shared with the fs-watcher (Slice 2 US23) must
    NOT be updated to the failed-write's hash — otherwise a same-content
    retry would silently skip and leave disk byte-inconsistent with
    in-memory state, and the watcher's per-task suppression would
    desync from disk reality.

    Verified via the explicit ``sync_state`` handle so we can observe
    its post-failure values directly without reaching into the
    handler's closure.
    """
    from lithos_loom.sync_state import ProjectionSyncState

    sync_state = ProjectionSyncState()
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today, sync_state=sync_state)
    # Cold start: nothing on disk → seed is None.
    assert sync_state.last_written_hash is None
    assert sync_state.task_status_markers == {}

    # First write succeeds → hash advances away from None.
    await handler(_event("lithos.task.created", task_id="a"), _ctx())
    hash_after_a = sync_state.last_written_hash
    markers_after_a = dict(sync_state.task_status_markers)
    assert hash_after_a is not None, "successful write should seed the hash"
    assert markers_after_a == {"a": "[ ]"}

    # Second write fails → hash + markers must roll back to post-a state.
    def _failing(src: str | Path, dst: str | Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _failing)
    with pytest.raises(OSError, match="simulated replace failure"):
        await handler(_event("lithos.task.created", task_id="b"), _ctx())

    assert sync_state.last_written_hash == hash_after_a, (
        "sync_state.last_written_hash must not advance when os.replace raises"
    )
    assert sync_state.task_status_markers == markers_after_a, (
        "sync_state.task_status_markers must not advance when os.replace raises"
    )


async def test_atomic_write_failure_does_not_advance_write_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ProjectionSyncState.write_version`` must NOT advance on a
    failed write. The watcher uses ``write_version > _last_processed``
    to detect "projection wrote since my last poll"; if version
    advanced on a failed write, the watcher would clear
    ``_observed_markers`` and emit a duplicate transition on the
    next real user edit. Regression for Copilot review on PR #26.
    """
    from lithos_loom.sync_state import ProjectionSyncState

    sync_state = ProjectionSyncState()
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today, sync_state=sync_state)

    await handler(_event("lithos.task.created", task_id="a"), _ctx())
    version_after_a = sync_state.write_version
    assert version_after_a == 1, "successful first write must bump version to 1"

    def _failing(src: str | Path, dst: str | Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _failing)
    with pytest.raises(OSError, match="simulated replace failure"):
        await handler(_event("lithos.task.created", task_id="b"), _ctx())

    assert sync_state.write_version == version_after_a, (
        "write_version must not advance when os.replace raises; "
        f"got {sync_state.write_version} (expected {version_after_a})"
    )


# Restart convergence (the US14 user-facing scenario)


async def test_post_restart_single_task_replay_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user-facing US14 scenario at its cleanest: a single-task
    projection. Write via handler A, tear down, build handler B
    against the same vault, replay the single created event. The
    render produces content matching the disk-seed → zero writes.

    Multi-task projections still cost N writes during bootstrap
    (state grows incrementally and the seed gets overwritten by our
    own intermediate writes before the convergent state lands);
    closing that needs a write-debounce / coalesce layer, documented
    as a known limitation in the module docstring."""
    cfg = _cfg(tmp_path)
    handler_a = make_handler(cfg, today_provider=_fixed_today)
    await handler_a(_event("lithos.task.created", task_id="only"), _ctx())
    disk_content_before = (tmp_path / "_lithos/tasks.md").read_text()

    handler_b = make_handler(cfg, today_provider=_fixed_today)
    calls = _spy_os_replace(monkeypatch)
    await handler_b(_event("lithos.task.created", task_id="only"), _ctx())
    assert calls == [], (
        f"single-task restart should hash-skip the convergent write; got {calls}"
    )
    # Disk content is unchanged.
    assert (tmp_path / "_lithos/tasks.md").read_text() == disk_content_before


async def test_post_restart_multi_task_replay_inline_writes_intermediates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documents the inline-flush behavior (``debounce_seconds=0``):
    bootstrap of an N-task quiet-KB restart writes N times during
    state rebuild because each event flushes immediately, the
    intermediate render differs from disk-seed, and the disk-seed
    gets overwritten in ``last_written_hash`` by our own
    intermediates before convergence.

    Production uses ``debounce_seconds > 0`` to coalesce this — see
    ``test_post_restart_multi_task_replay_under_debounce_coalesces_to_zero_writes``
    for the user-visible behaviour."""
    cfg = _cfg(tmp_path)
    handler_a = make_handler(cfg, today_provider=_fixed_today)
    for tid in ("a", "b", "c"):
        await handler_a(
            _event("lithos.task.created", task_id=tid, title=tid.upper()), _ctx()
        )
    disk_content_before = (tmp_path / "_lithos/tasks.md").read_text()

    handler_b = make_handler(cfg, today_provider=_fixed_today)
    calls = _spy_os_replace(monkeypatch)
    for tid in ("a", "b", "c"):
        await handler_b(
            _event("lithos.task.created", task_id=tid, title=tid.upper()), _ctx()
        )
    assert len(calls) == 3, (
        f"inline-flush mode writes once per event during multi-task bootstrap; "
        f"got {len(calls)}: {calls}"
    )
    # End-state on disk matches what was there before.
    assert (tmp_path / "_lithos/tasks.md").read_text() == disk_content_before


async def test_post_restart_with_different_state_writes_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inverse: when the rebuilt state differs from disk, writes
    happen normally (no spurious dedup)."""
    cfg = _cfg(tmp_path)
    handler_a = make_handler(cfg, today_provider=_fixed_today)
    await handler_a(_event("lithos.task.created", task_id="a", title="A"), _ctx())
    await handler_a(_event("lithos.task.created", task_id="b", title="B"), _ctx())

    handler_b = make_handler(cfg, today_provider=_fixed_today)
    calls = _spy_os_replace(monkeypatch)
    # New restart sees only 'a' and a new 'c' — final state differs
    # from pre-restart {a, b}. Each step must actually write:
    #  - "a": render {a} differs from disk-seed sha256({a, b}) → WRITE
    #  - "c": render {a, c} differs from sha256({a}) → WRITE
    # Exact count guards against an over-dedup regression that would
    # silently skip one of the writes.
    await handler_b(_event("lithos.task.created", task_id="a", title="A"), _ctx())
    await handler_b(_event("lithos.task.created", task_id="c", title="C"), _ctx())
    assert len(calls) == 2, (
        f"expected exactly 2 writes (one per event with different content); "
        f"got {len(calls)}: {calls}"
    )
    text = (tmp_path / "_lithos/tasks.md").read_text()
    assert "lithos:a" in text and "lithos:c" in text


# TTL eviction interaction


async def test_ttl_eviction_alone_still_triggers_write_when_content_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry expires under TTL between event handles → file rewrites
    because content actually changed (the resolved line is gone). The
    content-hash layer must NOT block the eviction-driven write."""
    cfg = _cfg(tmp_path)
    # Stepping today_provider: stays at _TODAY until we bump it.
    now_box = [_TODAY]

    def _stepping_today() -> date:
        return now_box[0]

    handler = make_handler(cfg, today_provider=_stepping_today)
    # Land a resolved entry today (well within TTL = 7).
    await handler(
        _resolved_event("lithos.task.completed", task_id="r", title="r", when=_TODAY),
        _ctx(),
    )
    assert "lithos:r" in (tmp_path / "_lithos/tasks.md").read_text()

    # Jump 20 days forward — TTL=7 means the resolved entry is now
    # past cutoff and the sweep will evict it on the next event.
    now_box[0] = _TODAY + timedelta(days=20)

    calls = _spy_os_replace(monkeypatch)
    # Any non-replayed event triggers the sweep at top of handle.
    # Use a fresh orphan that ALSO mutates state, so content changes
    # via two paths (eviction + new entry). The write should fire.
    await handler(_event("lithos.task.created", task_id="fresh"), _ctx())

    assert len(calls) == 1, (
        f"TTL eviction + new content should have written once; got {calls}"
    )
    text = (tmp_path / "_lithos/tasks.md").read_text()
    assert "lithos:r" not in text
    assert "lithos:fresh" in text


# ── US14 debounce / coalesce (production mode) ─────────────────────────


_TEST_DEBOUNCE = 0.02
"""20ms — long enough that the test can fire several events inside the
window before the timer expires; short enough that the test waits a
total of ~30ms after the burst to observe the coalesced flush."""


async def test_burst_of_events_coalesces_into_single_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three events fired back-to-back within the debounce window
    cancel-and-reschedule the pending flush; only the burst's tail
    flush actually writes — once, with the final {a,b,c} content."""
    cfg = _cfg(tmp_path)
    handler = make_handler(
        cfg, today_provider=_fixed_today, debounce_seconds=_TEST_DEBOUNCE
    )
    calls = _spy_os_replace(monkeypatch)
    for tid in ("a", "b", "c"):
        await handler(
            _event("lithos.task.created", task_id=tid, title=tid.upper()), _ctx()
        )
    # Wait long enough for the trailing debounce to expire and the
    # flush task to complete.
    await asyncio.sleep(_TEST_DEBOUNCE * 2.5)
    assert len(calls) == 1, (
        f"burst of 3 events inside the debounce window should coalesce "
        f"into 1 write; got {len(calls)}: {calls}"
    )
    text = (tmp_path / "_lithos/tasks.md").read_text()
    assert "lithos:a" in text and "lithos:b" in text and "lithos:c" in text


async def test_post_restart_multi_task_replay_under_debounce_zero_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user-facing US14 scenario at full strength: write a 3-task
    projection via handler A (inline), tear down, build handler B with
    debounce enabled and replay the same 3 events. The burst coalesces
    into one flush; that flush's rendered content matches the disk
    seed → ZERO writes. Obsidian Sync sees no mtime ripple."""
    cfg = _cfg(tmp_path)
    handler_a = make_handler(cfg, today_provider=_fixed_today)  # inline
    for tid in ("a", "b", "c"):
        await handler_a(
            _event("lithos.task.created", task_id=tid, title=tid.upper()), _ctx()
        )
    disk_content_before = (tmp_path / "_lithos/tasks.md").read_text()
    mtime_before = (tmp_path / "_lithos/tasks.md").stat().st_mtime_ns

    handler_b = make_handler(
        cfg, today_provider=_fixed_today, debounce_seconds=_TEST_DEBOUNCE
    )
    calls = _spy_os_replace(monkeypatch)
    for tid in ("a", "b", "c"):
        await handler_b(
            _event("lithos.task.created", task_id=tid, title=tid.upper()), _ctx()
        )
    await asyncio.sleep(_TEST_DEBOUNCE * 2.5)
    assert calls == [], (
        f"debounced multi-task replay against disk-seeded handler should "
        f"write zero times; got {calls}"
    )
    # Disk content + mtime both unchanged.
    assert (tmp_path / "_lithos/tasks.md").read_text() == disk_content_before
    assert (tmp_path / "_lithos/tasks.md").stat().st_mtime_ns == mtime_before


async def test_single_event_under_debounce_flushes_after_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single event in debounce mode writes once after the window
    elapses — verifies the deferred flush actually fires."""
    cfg = _cfg(tmp_path)
    handler = make_handler(
        cfg, today_provider=_fixed_today, debounce_seconds=_TEST_DEBOUNCE
    )
    calls = _spy_os_replace(monkeypatch)
    await handler(_event("lithos.task.created", task_id="x"), _ctx())
    # Immediately: no write yet (still inside debounce window).
    assert calls == [], "write should be deferred during debounce window"
    await asyncio.sleep(_TEST_DEBOUNCE * 2.5)
    assert len(calls) == 1, (
        f"single event should produce one write after debounce; got {calls}"
    )


async def test_new_event_cancels_pending_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Event A schedules a flush. Event B arrives within the window
    and cancels-then-reschedules. Only ONE write happens (B's tail
    flush), with the final {a,b} content — not two."""
    cfg = _cfg(tmp_path)
    handler = make_handler(
        cfg, today_provider=_fixed_today, debounce_seconds=_TEST_DEBOUNCE
    )
    calls = _spy_os_replace(monkeypatch)
    await handler(_event("lithos.task.created", task_id="a"), _ctx())
    # Sleep less than the debounce window — A's flush is still pending.
    await asyncio.sleep(_TEST_DEBOUNCE * 0.3)
    await handler(_event("lithos.task.created", task_id="b"), _ctx())
    # Now wait the full window for B's coalesced flush.
    await asyncio.sleep(_TEST_DEBOUNCE * 2.5)
    assert len(calls) == 1, (
        f"A's pending flush should have been cancelled by B; expected one "
        f"coalesced write, got {len(calls)}: {calls}"
    )
    text = (tmp_path / "_lithos/tasks.md").read_text()
    assert "lithos:a" in text and "lithos:b" in text


async def test_under_debounce_in_memory_short_circuit_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replaying the same event under debounce mode hits the in-memory
    ``current == prior`` short-circuit BEFORE _schedule_flush is even
    reached — so no pending flush gets created, and the spy stays
    empty. Defensive: the in-memory layer should not be defeated by
    the debounce layer."""
    cfg = _cfg(tmp_path)
    handler = make_handler(
        cfg, today_provider=_fixed_today, debounce_seconds=_TEST_DEBOUNCE
    )
    # First event writes (after debounce).
    await handler(_event("lithos.task.created", task_id="a"), _ctx())
    await asyncio.sleep(_TEST_DEBOUNCE * 2.5)
    calls = _spy_os_replace(monkeypatch)
    # Replay same event — current == prior, short-circuits before
    # scheduling any flush.
    await handler(_event("lithos.task.created", task_id="a"), _ctx())
    await asyncio.sleep(_TEST_DEBOUNCE * 2.5)
    assert calls == [], f"replayed event should have stayed inert; got {calls}"


# ── US21: priority on _StateEntry + sync_state ride-through ────────────


async def test_flush_passes_priority_to_sync_state_for_open_task(
    tmp_path: Path,
) -> None:
    """After ``_flush``, ``sync_state.task_priority_markers`` reflects
    the per-task priority for open tasks. Drives the fs-watcher's
    US21 priority diff."""
    from lithos_loom.sync_state import ProjectionSyncState

    sync_state = ProjectionSyncState()
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today, sync_state=sync_state)
    await handler(
        _event(
            "lithos.task.created",
            task_id="ap",
            metadata={"priority": "high"},
        ),
        _ctx(),
    )
    assert sync_state.task_priority_markers == {"ap": "high"}


async def test_flush_passes_none_priority_when_metadata_absent(
    tmp_path: Path,
) -> None:
    """Open task with no priority → sync_state carries ``None`` (not
    a missing key). The fs-watcher relies on the dict entry existing
    to distinguish "projection knows about this task, no priority"
    from "projection has never written this task"."""
    from lithos_loom.sync_state import ProjectionSyncState

    sync_state = ProjectionSyncState()
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today, sync_state=sync_state)
    await handler(_event("lithos.task.created", task_id="np"), _ctx())
    assert sync_state.task_priority_markers == {"np": None}


async def test_flush_drops_priority_for_unknown_enum_value(
    tmp_path: Path,
) -> None:
    """Lithos sends an unknown priority enum (typo, future value) →
    sync_state stores ``None`` so the watcher diff doesn't trip
    against a value the projection never rendered."""
    from lithos_loom.sync_state import ProjectionSyncState

    sync_state = ProjectionSyncState()
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today, sync_state=sync_state)
    await handler(
        _event(
            "lithos.task.created",
            task_id="up",
            metadata={"priority": "urgent"},  # not in the D18 enum
        ),
        _ctx(),
    )
    assert sync_state.task_priority_markers == {"up": None}


async def test_resolved_state_entry_has_none_priority(
    tmp_path: Path,
) -> None:
    """Completed/cancelled lines drop the priority marker; sync_state
    must mirror that with ``None`` for resolved entries so the
    watcher's diff doesn't fire against a stale projection-known
    priority after the task resolves."""
    from lithos_loom.sync_state import ProjectionSyncState

    sync_state = ProjectionSyncState()
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, today_provider=_fixed_today, sync_state=sync_state)
    await handler(
        _event(
            "lithos.task.created",
            task_id="rp",
            metadata={"priority": "high"},
        ),
        _ctx(),
    )
    assert sync_state.task_priority_markers == {"rp": "high"}
    # Now complete it.
    await handler(
        _event(
            "lithos.task.completed",
            task_id="rp",
            status="completed",
            metadata={"priority": "high"},
        ),
        _ctx(),
    )
    assert sync_state.task_priority_markers == {"rp": None}
