"""Tests for ``lithos_loom.sources.obsidian_dir_watcher`` (Slice 5 US33).

Drives ``poll_once()`` directly instead of running the polling loop —
gives deterministic ordering between projection writes, operator
edits, and watcher polls without timing flakiness. Mirrors the
testing shape of :mod:`tests.test_obsidian_fs_watcher`.

Each test wires a real :class:`EventBus`, subscribes to
``obsidian.note.modified``, and asserts on the queue.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.render_project_context import compute_body_hash
from lithos_loom.sources.obsidian_dir_watcher import ObsidianDirWatcher
from lithos_loom.sync_state import ProjectionSyncState

# ── Helpers ────────────────────────────────────────────────────────────


def _subscribe(bus: EventBus) -> Subscription:
    return bus.subscribe(
        event_types=("obsidian.note.modified",),
        name="test-subscriber",
    )


def _drain(sub: Subscription) -> list[Event]:
    out: list[Event] = []
    while True:
        try:
            out.append(sub.queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


def _write_doc(
    path: Path,
    *,
    lithos_id: str,
    lithos_version: int,
    body: str,
    extra_frontmatter: str = "",
) -> None:
    """Write a project-context-shaped Markdown file to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = [
        f"lithos_id: {lithos_id}",
        f"lithos_version: {lithos_version}",
    ]
    if extra_frontmatter:
        fm_lines.append(extra_frontmatter)
    fm = "\n".join(fm_lines)
    text = f"---\n{fm}\n---\n{body}"
    path.write_text(text, encoding="utf-8")


def _record_projection(
    sync_state: ProjectionSyncState,
    path: Path,
    doc_id: str,
    version: int,
) -> None:
    """Stand in for the projection's write — record sync_state so
    the dir-watcher's self-write check has something to compare."""
    text = path.read_text(encoding="utf-8")
    file_hash = hashlib.sha256(text.encode("utf-8")).digest()
    body_hash = compute_body_hash(text)
    sync_state.record_project_context_write(
        doc_id=doc_id,
        file_hash=file_hash,
        body_hash=body_hash,
        version=version,
        projected_path=path,
    )


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault" / "_lithos" / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def sub(bus: EventBus) -> Subscription:
    return _subscribe(bus)


# ── Layer 1: file unchanged since last poll ────────────────────────────


async def test_poll_with_no_files_emits_nothing(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """Empty projects directory → no events, no errors."""
    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=ProjectionSyncState()
    )
    n = await watcher.poll_once()
    assert n == 0
    assert _drain(sub) == []


async def test_poll_with_missing_projects_root_returns_zero(
    bus: EventBus, sub: Subscription, tmp_path: Path
) -> None:
    """projects_root not yet created → no errors, just zero work."""
    nonexistent = tmp_path / "no" / "such" / "dir"
    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=nonexistent, sync_state=ProjectionSyncState()
    )
    n = await watcher.poll_once()
    assert n == 0
    assert _drain(sub) == []


async def test_unchanged_file_polled_twice_emits_once(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """Operator-edited file on poll 1 → emit. Poll 2 with no change → silent."""
    sync_state = ProjectionSyncState()
    path = projects_root / "lithos-loom" / "context.md"
    _write_doc(path, lithos_id="doc-1", lithos_version=5, body="# T\n\nOld body\n")
    _record_projection(sync_state, path, "doc-1", 5)

    # Operator edits the body.
    _write_doc(path, lithos_id="doc-1", lithos_version=5, body="# T\n\nNew body\n")

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )

    n1 = await watcher.poll_once()
    n2 = await watcher.poll_once()
    assert (n1, n2) == (1, 0)
    events = _drain(sub)
    assert len(events) == 1


# ── Layer 2: projection self-write suppression ─────────────────────────


async def test_projection_write_is_absorbed_silently(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """File matches sync_state.note_file_hashes → no emit."""
    sync_state = ProjectionSyncState()
    path = projects_root / "lithos-loom" / "context.md"
    _write_doc(path, lithos_id="doc-1", lithos_version=5, body="# T\n\nBody\n")
    _record_projection(sync_state, path, "doc-1", 5)

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )
    n = await watcher.poll_once()
    assert n == 0
    assert _drain(sub) == []


async def test_projection_rewrite_after_operator_edit_resets_baseline(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """Operator edits → emit. Projection rewrites (e.g. doc updated
    upstream) → silent (the rewrite is authoritative). Subsequent
    operator edit on the new baseline → emits the new transition,
    not a stale one."""
    sync_state = ProjectionSyncState()
    path = projects_root / "lithos-loom" / "context.md"
    _write_doc(path, lithos_id="doc-1", lithos_version=5, body="# T\n\nOriginal\n")
    _record_projection(sync_state, path, "doc-1", 5)

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )

    # Operator edits.
    _write_doc(path, lithos_id="doc-1", lithos_version=5, body="# T\n\nOperator edit\n")
    assert await watcher.poll_once() == 1

    # Projection rewrites with a fresh body (Lithos updated upstream).
    _write_doc(path, lithos_id="doc-1", lithos_version=6, body="# T\n\nServer body\n")
    _record_projection(sync_state, path, "doc-1", 6)
    assert await watcher.poll_once() == 0

    # Operator edits AGAIN, this time on top of the server body.
    _write_doc(
        path, lithos_id="doc-1", lithos_version=6, body="# T\n\nNew operator edit\n"
    )
    assert await watcher.poll_once() == 1


# ── Layer 3: body-only diff (D28) ──────────────────────────────────────


async def test_body_edit_emits(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """Operator changes the body → ``obsidian.note.modified`` emitted."""
    sync_state = ProjectionSyncState()
    path = projects_root / "lithos-loom" / "context.md"
    _write_doc(path, lithos_id="doc-1", lithos_version=5, body="# T\n\nOriginal\n")
    _record_projection(sync_state, path, "doc-1", 5)

    _write_doc(path, lithos_id="doc-1", lithos_version=5, body="# T\n\nNew body\n")

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )
    n = await watcher.poll_once()
    assert n == 1
    events = _drain(sub)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "obsidian.note.modified"
    payload = dict(ev.payload)
    assert payload["lithos_id"] == "doc-1"
    assert payload["lithos_version"] == 5
    assert payload["slug"] == "lithos-loom"
    assert payload["filename"] == "context.md"
    assert payload["vault_path"] == str(path)
    assert payload["body"] == "# T\n\nNew body\n"


async def test_frontmatter_only_edit_is_absorbed(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """D28: operator adds a Dataview field, no body change → silent."""
    sync_state = ProjectionSyncState()
    path = projects_root / "lithos-loom" / "context.md"
    _write_doc(path, lithos_id="doc-1", lithos_version=5, body="# T\n\nBody\n")
    _record_projection(sync_state, path, "doc-1", 5)

    _write_doc(
        path,
        lithos_id="doc-1",
        lithos_version=5,
        body="# T\n\nBody\n",
        extra_frontmatter="dataview_field: 42",
    )

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )
    n = await watcher.poll_once()
    assert n == 0
    assert _drain(sub) == []


async def test_body_edit_after_frontmatter_edit_emits_once(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """Two saves: first frontmatter-only (no emit), second body-only.
    Second save must emit even though the first updated our cached
    file-hash. Without the body-hash baseline this would silently
    swallow the body edit because the file-hash had already advanced."""
    sync_state = ProjectionSyncState()
    path = projects_root / "lithos-loom" / "context.md"
    _write_doc(path, lithos_id="doc-1", lithos_version=5, body="# T\n\nOriginal\n")
    _record_projection(sync_state, path, "doc-1", 5)

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )

    _write_doc(
        path,
        lithos_id="doc-1",
        lithos_version=5,
        body="# T\n\nOriginal\n",
        extra_frontmatter="my_field: x",
    )
    assert await watcher.poll_once() == 0

    _write_doc(
        path,
        lithos_id="doc-1",
        lithos_version=5,
        body="# T\n\nReal change\n",
        extra_frontmatter="my_field: x",
    )
    assert await watcher.poll_once() == 1


async def test_repeated_body_save_with_same_content_emits_once(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """Operator save → edit → save (with the SAME body as the edit)
    must emit exactly once. The local observed-hash overlay prevents
    repeat emissions of the same body transition."""
    sync_state = ProjectionSyncState()
    path = projects_root / "lithos-loom" / "context.md"
    _write_doc(path, lithos_id="doc-1", lithos_version=5, body="# T\n\nOriginal\n")
    _record_projection(sync_state, path, "doc-1", 5)

    _write_doc(path, lithos_id="doc-1", lithos_version=5, body="# T\n\nNew\n")

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )
    assert await watcher.poll_once() == 1

    # Same file content again — operator did a save without editing.
    # File hash + body hash both unchanged; layer 1 short-circuits.
    assert await watcher.poll_once() == 0


# ── Multiple files ─────────────────────────────────────────────────────


async def test_multiple_docs_emit_independently(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """Two docs, each with an operator edit → two events."""
    sync_state = ProjectionSyncState()
    path_a = projects_root / "foo" / "context.md"
    path_b = projects_root / "bar" / "context.md"
    _write_doc(path_a, lithos_id="doc-a", lithos_version=1, body="# A\n\nold-a\n")
    _write_doc(path_b, lithos_id="doc-b", lithos_version=2, body="# B\n\nold-b\n")
    _record_projection(sync_state, path_a, "doc-a", 1)
    _record_projection(sync_state, path_b, "doc-b", 2)

    _write_doc(path_a, lithos_id="doc-a", lithos_version=1, body="# A\n\nnew-a\n")
    _write_doc(path_b, lithos_id="doc-b", lithos_version=2, body="# B\n\nnew-b\n")

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )
    n = await watcher.poll_once()
    assert n == 2
    events = _drain(sub)
    ids = {ev.payload["lithos_id"] for ev in events}
    assert ids == {"doc-a", "doc-b"}


async def test_nested_filename_yields_slash_separated_filename(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """Doc at ``<slug>/sub/notes.md`` yields filename=``sub/notes.md``."""
    sync_state = ProjectionSyncState()
    path = projects_root / "lithos-loom" / "sub" / "notes.md"
    _write_doc(path, lithos_id="doc-1", lithos_version=1, body="# T\n\nOld\n")
    _record_projection(sync_state, path, "doc-1", 1)

    _write_doc(path, lithos_id="doc-1", lithos_version=1, body="# T\n\nNew\n")
    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )
    assert await watcher.poll_once() == 1
    events = _drain(sub)
    assert events[0].payload["filename"] == "sub/notes.md"
    assert events[0].payload["slug"] == "lithos-loom"


# ── File added / removed (no emit) ─────────────────────────────────────


async def test_operator_created_file_without_projection_is_skipped(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """Operator creates a file with a lithos_id we've never projected
    → first sight seeds the body baseline silently, no emit (we have
    nothing authoritative to push)."""
    sync_state = ProjectionSyncState()  # empty
    path = projects_root / "lithos-loom" / "context.md"
    _write_doc(path, lithos_id="doc-1", lithos_version=1, body="# T\n\nBody\n")

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )
    assert await watcher.poll_once() == 0
    assert _drain(sub) == []


async def test_operator_created_file_then_edits_emits_on_edit(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """The seeded baseline lets a SUBSEQUENT edit on the same file emit."""
    sync_state = ProjectionSyncState()  # empty
    path = projects_root / "lithos-loom" / "context.md"
    _write_doc(path, lithos_id="doc-1", lithos_version=1, body="# T\n\nOriginal\n")

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )
    # First poll seeds baseline; no emit.
    assert await watcher.poll_once() == 0

    # Operator edits.
    _write_doc(path, lithos_id="doc-1", lithos_version=1, body="# T\n\nEdited\n")
    assert await watcher.poll_once() == 1


async def test_file_with_no_frontmatter_is_skipped(
    bus: EventBus,
    sub: Subscription,
    projects_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plain Markdown file in projects dir (operator put it there
    manually without going through the projection or CLI) → skip
    with a warning."""
    import logging

    caplog.set_level(logging.WARNING)
    sync_state = ProjectionSyncState()
    path = projects_root / "lithos-loom" / "context.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Just a doc\n\nNo frontmatter\n", encoding="utf-8")

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )
    assert await watcher.poll_once() == 0
    assert _drain(sub) == []
    assert any("no lithos_id" in r.message for r in caplog.records)


async def test_malformed_frontmatter_is_skipped(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """Operator typed garbage in the YAML → skip; don't crash."""
    sync_state = ProjectionSyncState()
    path = projects_root / "lithos-loom" / "context.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n}}}}\n---\n# T\n\nBody\n", encoding="utf-8")

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )
    assert await watcher.poll_once() == 0
    assert _drain(sub) == []


async def test_missing_lithos_version_skips_with_warning(
    bus: EventBus,
    sub: Subscription,
    projects_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Operator stripped ``lithos_version`` from frontmatter → skip
    rather than provide a bogus expected_version that's guaranteed
    to conflict."""
    import logging

    caplog.set_level(logging.WARNING)
    sync_state = ProjectionSyncState()
    path = projects_root / "lithos-loom" / "context.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nlithos_id: doc-1\n---\n# T\n\nOriginal\n", encoding="utf-8")
    # Seed sync_state via baseline_body_hash mechanism — write the
    # body-only hash so we can simulate "previously known" doc.
    sync_state.note_body_hashes["doc-1"] = compute_body_hash(
        "---\nlithos_id: doc-1\n---\n# T\n\nOriginal\n"
    )

    # Edit body.
    path.write_text("---\nlithos_id: doc-1\n---\n# T\n\nEdited\n", encoding="utf-8")

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )
    assert await watcher.poll_once() == 0
    assert _drain(sub) == []
    assert any("missing/malformed lithos_version" in r.message for r in caplog.records)


async def test_removed_file_drops_cached_hash(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """File polled, then deleted → cached hash dropped so a re-creation
    later isn't suppressed."""
    sync_state = ProjectionSyncState()
    path = projects_root / "lithos-loom" / "context.md"
    _write_doc(path, lithos_id="doc-1", lithos_version=1, body="# T\n\nBody\n")
    _record_projection(sync_state, path, "doc-1", 1)

    watcher = ObsidianDirWatcher(
        bus=bus, projects_root=projects_root, sync_state=sync_state
    )
    assert await watcher.poll_once() == 0
    # File now in cache.
    assert path in watcher._last_seen_file_hashes  # type: ignore[reportPrivateUsage]

    # Remove and re-poll.
    path.unlink()
    assert await watcher.poll_once() == 0
    assert path not in watcher._last_seen_file_hashes  # type: ignore[reportPrivateUsage]


# ── Event timestamp ────────────────────────────────────────────────────


async def test_emitted_event_timestamp_uses_now_provider(
    bus: EventBus, sub: Subscription, projects_root: Path
) -> None:
    """Tests can pin the emitted event's timestamp via the now_provider seam."""
    fixed = datetime(2026, 1, 15, 9, 30, 0, tzinfo=UTC)
    sync_state = ProjectionSyncState()
    path = projects_root / "lithos-loom" / "context.md"
    _write_doc(path, lithos_id="doc-1", lithos_version=1, body="# T\n\nOld\n")
    _record_projection(sync_state, path, "doc-1", 1)
    _write_doc(path, lithos_id="doc-1", lithos_version=1, body="# T\n\nNew\n")

    watcher = ObsidianDirWatcher(
        bus=bus,
        projects_root=projects_root,
        sync_state=sync_state,
        _now_provider=lambda: fixed,
    )
    assert await watcher.poll_once() == 1
    events = _drain(sub)
    assert events[0].timestamp == fixed
