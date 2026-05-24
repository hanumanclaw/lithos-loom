"""Tests for ``lithos_loom.subscriptions._project_context_projection``
(Slice 4 US29).

The handler consumes ``lithos.note.{created,updated,deleted}`` events,
filters at the boundary (path-prefix + tag per D26), re-fetches via
``note_read`` for the full body, renders, and atomic-writes per-doc
files under ``<vault>/<projects_dir>/<slug>/<filename>.md``.

Tests inject a fake LithosClient (via SubscriptionContext.lithos) and
a temp vault to exercise the handler end-to-end without an HTTP call.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lithos_loom.bus import Event
from lithos_loom.config import (
    LoomConfig,
    ObsidianSyncConfig,
    OrchestratorConfig,
)
from lithos_loom.lithos_client import Note
from lithos_loom.render_project_context import (
    extract_frontmatter,
    render_doc,
)
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._project_context_projection import make_handler
from lithos_loom.sync_state import ProjectionSyncState

# ── Test helpers ────────────────────────────────────────────────────────


def _note(
    *,
    id_: str = "doc-1",
    title: str = "Lithos Loom",
    body: str = "Body content.",
    version: int = 12,
    tags: tuple[str, ...] = ("project-context",),
    path: str = "projects/lithos-loom/context.md",
) -> Note:
    return Note(
        id=id_,
        title=title,
        body=body,
        version=version,
        updated_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        tags=tags,
        status="active",
        note_type="concept",
        path=path,
        slug=path.split("/")[1] if path.startswith("projects/") else "",
    )


def _cfg(tmp_path: Path) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        obsidian_sync=ObsidianSyncConfig(
            vault_path=tmp_path / "vault",
            tasks_file=Path("_lithos/tasks.md"),
            projects_dir=Path("_lithos/projects"),
        ),
    )


def _ctx(lithos: Any | None = None) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=lithos if lithos is not None else AsyncMock(),
        logger=logging.getLogger("test.project_context_projection"),
        agent_id="lithos-orchestrator-test",
    )


def _event(
    event_type: str,
    *,
    id_: str = "doc-1",
    title: str = "Lithos Loom",
    path: str = "projects/lithos-loom/context.md",
) -> Event:
    payload = {"id": id_, "title": title, "path": path}
    return Event(
        type=event_type,
        timestamp=datetime.now(UTC),
        payload=MappingProxyType(payload),
    )


def _vault_path(tmp_path: Path, rel: str) -> Path:
    return tmp_path / "vault" / "_lithos" / "projects" / rel


# ── Created / updated happy path ───────────────────────────────────────


async def test_created_writes_projected_file(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))

    target = _vault_path(tmp_path, "lithos-loom/context.md")
    assert target.exists(), f"expected projection at {target}"
    body = target.read_text()
    assert "lithos_id: doc-1" in body
    assert "# Lithos Loom" in body


async def test_updated_overwrites_existing_file(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(version=1, body="Original.")
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    # First write
    await handler(_event("lithos.note.created"), _ctx(lithos))
    target = _vault_path(tmp_path, "lithos-loom/context.md")
    assert "Original." in target.read_text()

    # Updated version
    lithos.note_read.return_value = _note(version=2, body="Updated.")
    await handler(_event("lithos.note.updated"), _ctx(lithos))
    text = target.read_text()
    assert "Updated." in text
    assert "lithos_version: 2" in text
    assert "Original." not in text


async def test_re_fetches_via_note_read_on_each_event(tmp_path: Path) -> None:
    """The SSE payload only carries ``{id, title, path}``; the
    handler must call ``note_read(id=...)`` to get the body + tags +
    version. Otherwise the rendered file would be incomplete and
    re-projection on bootstrap would silently differ from live
    updates."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))

    lithos.note_read.assert_awaited_once_with(id="doc-1")


# ── Filters (D26) ──────────────────────────────────────────────────────


async def test_filters_event_with_path_outside_projects(tmp_path: Path) -> None:
    """SSE payloads from outside ``projects/`` are dropped before
    the ``note_read`` round-trip — saves the redundant lookup."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    event = _event("lithos.note.created", path="observations/inbox/foo.md")
    await handler(event, _ctx(lithos))

    lithos.note_read.assert_not_awaited()


async def test_filters_fetched_note_without_project_context_tag(
    tmp_path: Path,
) -> None:
    """The SSE payload may pass the cheap path-prefix filter but the
    fresh note from ``note_read`` may have a different tag set
    (e.g. operator removed the project-context tag). Re-check the
    authoritative tags post-fetch — drop without writing."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(tags=("other-tag",))
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))

    target = _vault_path(tmp_path, "lithos-loom/context.md")
    assert not target.exists()


async def test_filters_fetched_note_whose_path_changed(tmp_path: Path) -> None:
    """Fetched path no longer under ``projects/`` (operator moved
    the doc) → drop, don't write to a stale slug location."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(path="observations/inbox/foo.md")
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))

    assert not (tmp_path / "vault").exists() or not any(
        (tmp_path / "vault" / "_lithos" / "projects").rglob("*.md")
    )


async def test_skips_when_note_not_found_in_lithos(tmp_path: Path) -> None:
    """``note_read`` returns ``None`` (race: doc deleted between
    event and read) → skip cleanly, no crash."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = None
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))

    assert not (tmp_path / "vault").exists() or not any(
        (tmp_path / "vault" / "_lithos" / "projects").rglob("*.md")
    )


# ── Sync-state coordination ────────────────────────────────────────────


async def test_records_file_hash_version_and_path_in_sync_state(
    tmp_path: Path,
) -> None:
    """The dir-watcher (Slice 5) reads these to suppress self-writes
    and to provide ``expected_version`` for optimistic locking. The
    path map drives stale-file cleanup on path migration / filter
    rejection."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(version=7)
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))

    assert "doc-1" in sync_state.note_file_hashes
    assert sync_state.note_versions["doc-1"] == 7
    expected_path = _vault_path(tmp_path, "lithos-loom/context.md")
    assert sync_state.note_projected_paths["doc-1"] == expected_path


async def test_skips_write_when_file_hash_matches_last_write(tmp_path: Path) -> None:
    """Per-doc dedup uses the whole-file hash (frontmatter + body).
    Re-firing ``created`` with the same Note → identical render →
    skip. Important for bootstrap on cold restart (N notes →
    0 disk writes when nothing has changed)."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))
    target = _vault_path(tmp_path, "lithos-loom/context.md")
    first_mtime = target.stat().st_mtime_ns

    # Second event: same body → no-op.
    await handler(_event("lithos.note.created"), _ctx(lithos))

    assert target.stat().st_mtime_ns == first_mtime


async def test_body_change_writes_new_content(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(body="v1")
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))
    target = _vault_path(tmp_path, "lithos-loom/context.md")

    lithos.note_read.return_value = _note(body="v2")
    await handler(_event("lithos.note.updated"), _ctx(lithos))

    assert "v2" in target.read_text()
    assert "v1" not in target.read_text()


# ── Frontmatter-only updates (reviewer-finding regression) ─────────────
#
# US30 requires the projected frontmatter to mirror Lithos
# (``lithos_version``, ``lithos_updated_at``, ``status``, ``tags``).
# A version-bump-only update — same body, different metadata — MUST
# still rewrite the file. Previously the projection dedup'd on
# body-hash (frontmatter excluded), so version bumps were silently
# skipped, leaving stale frontmatter on disk and breaking Slice 5's
# optimistic-lock contract.


async def test_version_bump_with_unchanged_body_rewrites_file(
    tmp_path: Path,
) -> None:
    """A note whose body is unchanged but whose version bumped
    (Lithos-side edit that didn't touch content but did touch
    metadata) MUST trigger a rewrite — the projected
    ``lithos_version`` frontmatter is what Slice 5's note-push
    uses for optimistic locking. Without this fix, the projection
    would keep stale frontmatter on disk forever."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(version=1, body="Stable body.")
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))
    target = _vault_path(tmp_path, "lithos-loom/context.md")
    assert "lithos_version: 1" in target.read_text()
    first_mtime = target.stat().st_mtime_ns

    # Version-only bump; body identical.
    lithos.note_read.return_value = _note(version=2, body="Stable body.")
    await handler(_event("lithos.note.updated"), _ctx(lithos))

    text = target.read_text()
    assert "lithos_version: 2" in text, (
        f"frontmatter should reflect the new version; got: {text}"
    )
    assert "Stable body." in text
    assert target.stat().st_mtime_ns != first_mtime, (
        "file should have been rewritten on version bump"
    )
    assert sync_state.note_versions["doc-1"] == 2


async def test_tag_addition_with_unchanged_body_rewrites_file(
    tmp_path: Path,
) -> None:
    """A note that gains a new tag (e.g. operator adds ``track-2``
    in Lithos) must surface in the projected frontmatter so vault
    queries can filter on it. Same reason as version-bump: body-only
    dedup would silently miss this."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(tags=("project-context",))
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))
    target = _vault_path(tmp_path, "lithos-loom/context.md")

    lithos.note_read.return_value = _note(tags=("project-context", "track-2"))
    await handler(_event("lithos.note.updated"), _ctx(lithos))

    text = target.read_text()
    assert "- project-context" in text
    assert "- track-2" in text


async def test_status_change_with_unchanged_body_rewrites_file(
    tmp_path: Path,
) -> None:
    """A note transitioning ``active`` → ``archived`` in Lithos must
    have the new status reflected in projected frontmatter — the
    operator's ``status === 'active'`` Dataview queries depend on
    it."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    # Status changes are encoded directly in _note via the status arg.
    lithos.note_read.return_value = Note(
        id="doc-1",
        title="Lithos Loom",
        body="Body content.",
        version=1,
        updated_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        tags=("project-context",),
        status="active",
        note_type="concept",
        path="projects/lithos-loom/context.md",
        slug="lithos-loom",
    )
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))
    target = _vault_path(tmp_path, "lithos-loom/context.md")
    assert "status: active" in target.read_text()

    lithos.note_read.return_value = Note(
        id="doc-1",
        title="Lithos Loom",
        body="Body content.",
        version=2,
        updated_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        tags=("project-context",),
        status="archived",
        note_type="concept",
        path="projects/lithos-loom/context.md",
        slug="lithos-loom",
    )
    await handler(_event("lithos.note.updated"), _ctx(lithos))

    text = target.read_text()
    assert "status: archived" in text


# ── Stale-file cleanup (reviewer-finding regression) ───────────────────
#
# Three scenarios where a projected file would leak without explicit
# cleanup: path migration within ``projects/``, tag removal, and
# path moved out of ``projects/``. Lithos only emits ``note.deleted``
# for actual deletes, not for moves/retags — so the projection must
# detect "doc no longer qualifies" itself and clean up.


async def test_path_migration_within_projects_removes_old_file(
    tmp_path: Path,
) -> None:
    """Doc moves from ``projects/old-slug/context.md`` to
    ``projects/new-slug/context.md``. New file appears at the new
    location; old file is removed. Otherwise the moved doc would
    have a stale duplicate at the old path."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(
        path="projects/old-slug/context.md",
    )
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(
        _event("lithos.note.created", path="projects/old-slug/context.md"),
        _ctx(lithos),
    )
    old_path = _vault_path(tmp_path, "old-slug/context.md")
    assert old_path.exists()

    # Doc moves to a new slug.
    lithos.note_read.return_value = _note(
        path="projects/new-slug/context.md",
    )
    await handler(
        _event("lithos.note.updated", path="projects/new-slug/context.md"),
        _ctx(lithos),
    )

    new_path = _vault_path(tmp_path, "new-slug/context.md")
    assert new_path.exists(), "new projection must be written"
    assert not old_path.exists(), (
        "old projection must be removed — otherwise the moved doc "
        "would have a stale duplicate"
    )
    assert sync_state.note_projected_paths["doc-1"] == new_path


async def test_filename_change_within_same_slug_removes_old_file(
    tmp_path: Path,
) -> None:
    """Same slug, different filename
    (e.g. ``context.md`` → ``overview.md``). Old file removed."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(
        path="projects/lithos-loom/context.md",
    )
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))
    old_path = _vault_path(tmp_path, "lithos-loom/context.md")
    assert old_path.exists()

    lithos.note_read.return_value = _note(
        path="projects/lithos-loom/overview.md",
    )
    await handler(
        _event(
            "lithos.note.updated",
            path="projects/lithos-loom/overview.md",
        ),
        _ctx(lithos),
    )

    new_path = _vault_path(tmp_path, "lithos-loom/overview.md")
    assert new_path.exists()
    assert not old_path.exists()


async def test_tag_removed_cleans_up_stale_projection(tmp_path: Path) -> None:
    """Operator removes the ``project-context`` tag in Lithos — the
    doc no longer qualifies for projection. The previously-projected
    file must be removed, otherwise it sits indefinitely.

    Reviewer-finding: previously the handler just returned on filter
    rejection, leaving the stale file in place."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(tags=("project-context",))
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))
    target = _vault_path(tmp_path, "lithos-loom/context.md")
    assert target.exists()

    # Operator removes the project-context tag.
    lithos.note_read.return_value = _note(tags=("some-other-tag",))
    await handler(_event("lithos.note.updated"), _ctx(lithos))

    assert not target.exists(), (
        "stale projection must be cleaned up when the doc no longer "
        "qualifies (lost project-context tag)"
    )
    assert "doc-1" not in sync_state.note_file_hashes
    assert "doc-1" not in sync_state.note_projected_paths


async def test_doc_moved_out_of_projects_cleans_up_stale_projection(
    tmp_path: Path,
) -> None:
    """The doc moves from ``projects/foo/context.md`` to
    ``observations/inbox/foo.md`` — fails the path-prefix filter
    at the boundary. The stale projection must be cleaned up."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(path="projects/foo/context.md")
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(
        _event("lithos.note.created", path="projects/foo/context.md"),
        _ctx(lithos),
    )
    target = _vault_path(tmp_path, "foo/context.md")
    assert target.exists()

    # Doc moves out of projects/ — sse_path filter rejects at the
    # boundary. The handler must still know to clean up the prior
    # projection at the old location.
    await handler(
        _event(
            "lithos.note.updated",
            path="observations/inbox/foo.md",
        ),
        _ctx(lithos),
    )

    assert not target.exists()
    assert "doc-1" not in sync_state.note_projected_paths


async def test_fetched_path_out_of_projects_cleans_up_stale_projection(
    tmp_path: Path,
) -> None:
    """The post-fetch authoritative path moves out of ``projects/``
    (the SSE event path was still under projects/ but Lithos's
    canonical path has migrated). Same cleanup required."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(path="projects/foo/context.md")
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(
        _event("lithos.note.created", path="projects/foo/context.md"),
        _ctx(lithos),
    )
    target = _vault_path(tmp_path, "foo/context.md")
    assert target.exists()

    # SSE event still says projects/, but the freshly fetched note
    # has been moved (race between SSE buffer and authoritative read).
    lithos.note_read.return_value = _note(path="observations/foo.md")
    await handler(
        _event("lithos.note.updated", path="projects/foo/context.md"),
        _ctx(lithos),
    )

    assert not target.exists()
    assert "doc-1" not in sync_state.note_projected_paths


async def test_cleanup_with_no_prior_projection_is_silent_no_op(
    tmp_path: Path,
) -> None:
    """If a filter-rejected event arrives for a doc we've never
    projected, there's nothing to clean up — no crash, no
    file-not-found error."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(tags=("some-other-tag",))
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    # Filter rejects on first event — no prior projection on record.
    await handler(_event("lithos.note.created"), _ctx(lithos))

    # No exception, no state change.
    assert sync_state.note_projected_paths == {}


# ── Slug + path mapping ────────────────────────────────────────────────


async def test_slug_drives_subdirectory(tmp_path: Path) -> None:
    """Slug is the first path segment after ``projects/`` — both the
    vault subdir and the rendered ``slug:`` frontmatter."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(
        id_="doc-influx",
        path="projects/influx/context.md",
    )
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(
        _event(
            "lithos.note.created",
            id_="doc-influx",
            path="projects/influx/context.md",
        ),
        _ctx(lithos),
    )

    target = _vault_path(tmp_path, "influx/context.md")
    assert target.exists()
    fm, _ = extract_frontmatter(target.read_text())
    assert fm["slug"] == "influx"


async def test_nested_filename_preserved_under_slug(tmp_path: Path) -> None:
    """A doc at ``projects/lithos-loom/architecture/design.md``
    lands at ``<vault>/_lithos/projects/lithos-loom/architecture/design.md``
    — multi-segment filenames keep their structure."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(
        path="projects/lithos-loom/architecture/design.md"
    )
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(
        _event(
            "lithos.note.created",
            path="projects/lithos-loom/architecture/design.md",
        ),
        _ctx(lithos),
    )

    target = _vault_path(tmp_path, "lithos-loom/architecture/design.md")
    assert target.exists()


# ── Deleted events ─────────────────────────────────────────────────────


async def test_deleted_removes_local_file(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    # Create first
    await handler(_event("lithos.note.created"), _ctx(lithos))
    target = _vault_path(tmp_path, "lithos-loom/context.md")
    assert target.exists()

    # Now delete
    await handler(_event("lithos.note.deleted"), _ctx(lithos))

    assert not target.exists()


async def test_deleted_forgets_sync_state(tmp_path: Path) -> None:
    """After delete, ``forget_project_context`` must clear the
    per-doc hash so a subsequent re-creation of the same doc is
    NOT suppressed as a self-write."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.created"), _ctx(lithos))
    assert "doc-1" in sync_state.note_file_hashes

    await handler(_event("lithos.note.deleted"), _ctx(lithos))

    assert "doc-1" not in sync_state.note_file_hashes
    assert "doc-1" not in sync_state.note_versions


async def test_deleted_missing_file_is_silent(tmp_path: Path) -> None:
    """Best-effort delete — file already absent (operator removed
    manually, or earlier failed write) is fine."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    # No prior create — straight to delete.
    await handler(_event("lithos.note.deleted"), _ctx(lithos))

    # No exception, sync_state still clean.
    assert sync_state.note_file_hashes == {}


async def test_deleted_does_not_call_note_read(tmp_path: Path) -> None:
    """The note is gone from Lithos by the time we react —
    ``note_read`` would return None anyway. Skip the round-trip."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(_event("lithos.note.deleted"), _ctx(lithos))

    lithos.note_read.assert_not_awaited()


async def test_deleted_skips_path_outside_projects(tmp_path: Path) -> None:
    """A deleted event for a non-project-context doc shouldn't
    even attempt the unlink — the path is outside our managed
    directory tree."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(
        _event(
            "lithos.note.deleted",
            id_="other",
            path="observations/inbox/foo.md",
        ),
        _ctx(lithos),
    )

    # No assertion needed beyond no-crash; "outside projects/" is
    # debug-logged.


# ── Robustness ─────────────────────────────────────────────────────────


async def test_unknown_event_type_is_silently_ignored(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    await handler(
        _event("lithos.task.created"),  # wrong namespace
        _ctx(lithos),
    )

    lithos.note_read.assert_not_awaited()


async def test_malformed_payload_warns_and_returns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing ``id`` in payload → warn-log + drop, no crash."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    event = Event(
        type="lithos.note.created",
        timestamp=datetime.now(UTC),
        payload=MappingProxyType({"title": "no id"}),
    )
    with caplog.at_level(logging.WARNING, logger="test.project_context_projection"):
        await handler(event, _ctx(lithos))

    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("malformed payload" in m for m in warn_msgs), warn_msgs


async def test_write_failure_rolls_back_sync_state(tmp_path: Path) -> None:
    """If the atomic write raises on a CREATE (no prior projection),
    the per-doc hash must NOT be recorded — otherwise the next event
    would see "matches last write" and skip, leaving the disk
    content stale forever."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note()
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    # Make the projects_root unwritable to force write_file_atomic to fail.
    # mkdir-then-chmod the parent so the recursive parent creation can't
    # succeed.
    projects_root = tmp_path / "vault" / "_lithos" / "projects"
    projects_root.parent.mkdir(parents=True)
    projects_root.mkdir()
    projects_root.chmod(0o400)  # read-only

    try:
        with pytest.raises(Exception):  # noqa: B017
            await handler(_event("lithos.note.created"), _ctx(lithos))

        # State rolled back — re-firing must retry the write.
        assert "doc-1" not in sync_state.note_file_hashes
    finally:
        projects_root.chmod(0o755)  # restore for cleanup


async def test_migration_write_failure_preserves_old_file(
    tmp_path: Path,
) -> None:
    """Reviewer-finding regression: when a path migration's new
    write fails, the OLD file must remain on disk (not be unlinked
    pre-write). Earlier ordering unlinked the prior path first, so
    a transient write failure would leave the vault with neither
    the old nor new file until some later event re-projects.

    Also pins the sync_state rollback contract: prior state is
    restored (not cleared), so the next event still knows about
    the old projection and can retry the migration."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(path="projects/old/context.md")
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    # First write succeeds — projection at projects/old/context.md.
    await handler(
        _event("lithos.note.created", path="projects/old/context.md"),
        _ctx(lithos),
    )
    old_path = _vault_path(tmp_path, "old/context.md")
    assert old_path.exists()
    assert sync_state.note_projected_paths["doc-1"] == old_path
    prior_hash = sync_state.note_file_hashes["doc-1"]
    prior_version = sync_state.note_versions["doc-1"]

    # Now move the doc — but make the new target's parent unwritable
    # so write_file_atomic raises. ``projects_root`` (the new slug's
    # parent's parent) is writable, but the new slug dir doesn't
    # exist yet — chmod the projects_root so mkdir(parents=True) for
    # the new slug fails.
    projects_root = tmp_path / "vault" / "_lithos" / "projects"
    projects_root.chmod(0o500)  # read+exec only, no write

    lithos.note_read.return_value = _note(path="projects/new/context.md")
    try:
        with pytest.raises(Exception):  # noqa: B017
            await handler(
                _event(
                    "lithos.note.updated",
                    path="projects/new/context.md",
                ),
                _ctx(lithos),
            )

        # CRITICAL: old file must still exist. Earlier ordering would
        # have deleted it before the failed write, leaving the vault
        # with no projection at all.
        assert old_path.exists(), (
            "old projection must remain on disk after a failed "
            "migration — otherwise the doc vanishes from the vault"
        )

        # sync_state restored to prior values (not cleared), so the
        # next event still knows about the old path and can retry
        # the migration cleanly.
        assert sync_state.note_projected_paths["doc-1"] == old_path
        assert sync_state.note_file_hashes["doc-1"] == prior_hash
        assert sync_state.note_versions["doc-1"] == prior_version
    finally:
        projects_root.chmod(0o755)


async def test_migration_retried_after_failure_succeeds(
    tmp_path: Path,
) -> None:
    """Sequel to the failure test: after a transient migration
    failure, the next event with the new path must successfully
    write the new file AND clean up the old one. Without the
    prior-state rollback, sync_state would have lost the old path
    knowledge and the old file would linger as an orphan."""
    cfg = _cfg(tmp_path)
    lithos = AsyncMock()
    lithos.note_read.return_value = _note(path="projects/old/context.md")
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    # Initial projection.
    await handler(
        _event("lithos.note.created", path="projects/old/context.md"),
        _ctx(lithos),
    )
    old_path = _vault_path(tmp_path, "old/context.md")

    # Force a migration failure.
    projects_root = tmp_path / "vault" / "_lithos" / "projects"
    projects_root.chmod(0o500)
    lithos.note_read.return_value = _note(path="projects/new/context.md")
    try:
        with pytest.raises(Exception):  # noqa: B017
            await handler(
                _event(
                    "lithos.note.updated",
                    path="projects/new/context.md",
                ),
                _ctx(lithos),
            )
    finally:
        projects_root.chmod(0o755)

    # Retry the migration — should now succeed cleanly.
    await handler(
        _event("lithos.note.updated", path="projects/new/context.md"),
        _ctx(lithos),
    )

    new_path = _vault_path(tmp_path, "new/context.md")
    assert new_path.exists(), "retried migration must write the new file"
    assert not old_path.exists(), (
        "retried migration must clean up the old file — the prior-"
        "state rollback is what makes this work"
    )
    assert sync_state.note_projected_paths["doc-1"] == new_path


# ── make_handler defensive checks ──────────────────────────────────────


def test_make_handler_raises_without_obsidian_sync_config() -> None:
    """The spawn gate is upstream; this is a defensive belt-and-
    braces check."""
    cfg = LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="x",
            lithos_url="http://localhost:8765",
        ),
        # no obsidian_sync
    )
    with pytest.raises(RuntimeError, match="without \\[obsidian_sync\\]"):
        make_handler(cfg)


def test_make_handler_creates_fresh_sync_state_when_none() -> None:
    """Test convenience: passing None constructs a fresh state so
    tests don't have to wire one when they don't care about
    cross-handler coordination."""
    cfg = LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="x",
            lithos_url="http://localhost:8765",
        ),
        obsidian_sync=ObsidianSyncConfig(vault_path=Path("/tmp/v")),
    )
    handler = make_handler(cfg)  # no sync_state
    # Should construct without raising.
    assert callable(handler)


def test_round_trip_render_then_extract(tmp_path: Path) -> None:
    """End-to-end sanity: a rendered file parses back to recover
    the same id, version, slug, tags."""
    note = _note()
    rendered = render_doc(note)
    fm, _ = extract_frontmatter(rendered)
    assert fm["lithos_id"] == note.id
    assert fm["lithos_version"] == note.version
    assert fm["slug"] == note.slug
    assert fm["tags"] == list(note.tags)
