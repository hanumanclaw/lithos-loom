"""Tests for ``lithos_loom.subscriptions._note_conflict`` (Slice 5 US35, D29)."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lithos_loom.lithos_client import Note
from lithos_loom.render_project_context import (
    compute_body_hash,
    extract_frontmatter,
    render_doc,
)
from lithos_loom.subscriptions._note_conflict import (
    format_conflict_filename,
    resolve_conflict,
)
from lithos_loom.sync_state import ProjectionSyncState


def _note(
    *,
    doc_id: str = "doc-1",
    title: str = "Lithos Loom",
    body: str = "Server body",
    version: int = 12,
    tags: tuple[str, ...] = ("project-context",),
) -> Note:
    return Note(
        id=doc_id,
        title=title,
        body=body,
        version=version,
        updated_at=datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC),
        tags=tags,
        status="active",
        note_type="concept",
        path="",
        slug="",
    )


# ── format_conflict_filename ───────────────────────────────────────────


def test_format_conflict_filename_basic() -> None:
    ts = datetime(2026, 5, 24, 14, 30, 45, tzinfo=UTC)
    name = format_conflict_filename("lithos-loom", "context.md", ts)
    assert name == "lithos-loom.context.20260524T143045Z.md"


def test_format_conflict_filename_flattens_subdirs() -> None:
    """Filename with ``/`` collapses to a flat name with ``-``."""
    ts = datetime(2026, 5, 24, 14, 30, 45, tzinfo=UTC)
    name = format_conflict_filename("lithos-loom", "sub/notes.md", ts)
    assert name == "lithos-loom.sub-notes.20260524T143045Z.md"


def test_format_conflict_filename_preserves_md_extension_once() -> None:
    """Source already ends in ``.md`` → strip then re-add, so timestamp
    sits between basename and extension (not after a trailing ``.md``)."""
    ts = datetime(2026, 5, 24, 14, 30, 45, tzinfo=UTC)
    name = format_conflict_filename("foo", "context.md", ts)
    assert name.endswith(".md")
    assert name.count(".md") == 1


def test_format_conflict_filename_converts_naive_to_utc() -> None:
    """Non-UTC timestamp is converted to UTC before formatting."""
    # UTC equivalent: 14:30 on 2026-05-24
    from datetime import timedelta, timezone

    plus_two = timezone(timedelta(hours=2))
    ts = datetime(2026, 5, 24, 16, 30, 0, tzinfo=plus_two)
    name = format_conflict_filename("foo", "context.md", ts)
    assert "20260524T143000Z" in name


# ── resolve_conflict ───────────────────────────────────────────────────


async def test_resolve_moves_local_and_writes_canonical(
    tmp_path: Path,
) -> None:
    """Happy path: local moved to conflicts dir, canonical at original path."""
    local = tmp_path / "vault" / "_lithos" / "projects" / "loom" / "context.md"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("---\nlithos_id: doc-1\n---\n# T\n\nOperator body\n")
    conflicts_dir = tmp_path / "vault" / "_lithos" / "conflicts"
    sync_state = ProjectionSyncState()
    canonical = _note(body="Server body")

    fixed = datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC)
    conflict_path = await resolve_conflict(
        local_path=local,
        canonical_note=canonical,
        canonical_lithos_path="projects/loom/context.md",
        conflicts_dir=conflicts_dir,
        slug="loom",
        filename="context.md",
        sync_state=sync_state,
        doc_id="doc-1",
        timestamp_provider=lambda: fixed,
    )

    # Conflict file exists and carries the operator's body.
    assert conflict_path.exists()
    assert conflict_path.parent == conflicts_dir
    assert conflict_path.name == "loom.context.20260524T143000Z.md"
    assert "Operator body" in conflict_path.read_text()

    # Local path now has canonical body (rendered with frontmatter).
    assert local.exists()
    rendered = local.read_text()
    fm, body = extract_frontmatter(rendered)
    assert fm["lithos_id"] == "doc-1"
    assert fm["lithos_version"] == 12
    assert "Server body" in body


async def test_resolve_records_canonical_into_sync_state(
    tmp_path: Path,
) -> None:
    """Sync state is updated BEFORE the canonical write so a concurrent
    dir-watcher poll sees both the new bytes and the matching hash."""
    local = tmp_path / "vault" / "_lithos" / "projects" / "loom" / "context.md"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("---\nlithos_id: doc-1\n---\n# T\n\nOperator\n")
    conflicts_dir = tmp_path / "vault" / "_lithos" / "conflicts"
    sync_state = ProjectionSyncState()

    canonical = _note(body="Server", version=99)

    await resolve_conflict(
        local_path=local,
        canonical_note=canonical,
        canonical_lithos_path="projects/loom/context.md",
        conflicts_dir=conflicts_dir,
        slug="loom",
        filename="context.md",
        sync_state=sync_state,
        doc_id="doc-1",
    )

    # sync_state should match the canonical render the resolver wrote.
    rendered = local.read_text()
    expected_file_hash = hashlib.sha256(rendered.encode("utf-8")).digest()
    expected_body_hash = compute_body_hash(rendered)
    assert sync_state.note_file_hashes["doc-1"] == expected_file_hash
    assert sync_state.note_body_hashes["doc-1"] == expected_body_hash
    assert sync_state.note_versions["doc-1"] == 99
    assert sync_state.note_projected_paths["doc-1"] == local


async def test_resolve_logs_friction_breadcrumb(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The conflict surfaces a ``[Friction]`` log line at WARNING."""
    caplog.set_level(logging.WARNING)
    local = tmp_path / "vault" / "_lithos" / "projects" / "loom" / "context.md"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("---\nlithos_id: doc-1\n---\n# T\n\nOperator\n")
    conflicts_dir = tmp_path / "vault" / "_lithos" / "conflicts"
    sync_state = ProjectionSyncState()
    canonical = _note()

    await resolve_conflict(
        local_path=local,
        canonical_note=canonical,
        canonical_lithos_path="projects/loom/context.md",
        conflicts_dir=conflicts_dir,
        slug="loom",
        filename="context.md",
        sync_state=sync_state,
        doc_id="doc-1",
    )

    messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("[Friction]" in m and "doc-1" in m for m in messages)


async def test_resolve_creates_conflicts_dir_lazily(
    tmp_path: Path,
) -> None:
    """Conflicts dir doesn't exist beforehand → created on first conflict."""
    local = tmp_path / "vault" / "_lithos" / "projects" / "loom" / "context.md"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("---\nlithos_id: doc-1\n---\n# T\n\nOperator\n")
    conflicts_dir = tmp_path / "vault" / "_lithos" / "conflicts"
    assert not conflicts_dir.exists()

    await resolve_conflict(
        local_path=local,
        canonical_note=_note(),
        canonical_lithos_path="projects/loom/context.md",
        conflicts_dir=conflicts_dir,
        slug="loom",
        filename="context.md",
        sync_state=ProjectionSyncState(),
        doc_id="doc-1",
    )

    assert conflicts_dir.is_dir()


async def test_resolve_overwrites_existing_conflict_file(
    tmp_path: Path,
) -> None:
    """Timestamp collision (extreme rapid-fire) → existing conflict
    snapshot is overwritten rather than crashing the handler."""
    local = tmp_path / "vault" / "_lithos" / "projects" / "loom" / "context.md"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("---\nlithos_id: doc-1\n---\n# T\n\nSecond operator body\n")
    conflicts_dir = tmp_path / "vault" / "_lithos" / "conflicts"
    conflicts_dir.mkdir(parents=True)

    fixed = datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC)
    pre_existing = conflicts_dir / "loom.context.20260524T143000Z.md"
    pre_existing.write_text("OLD SNAPSHOT")

    conflict_path = await resolve_conflict(
        local_path=local,
        canonical_note=_note(),
        canonical_lithos_path="projects/loom/context.md",
        conflicts_dir=conflicts_dir,
        slug="loom",
        filename="context.md",
        sync_state=ProjectionSyncState(),
        doc_id="doc-1",
        timestamp_provider=lambda: fixed,
    )

    assert conflict_path == pre_existing
    # Overwritten with the new operator body, not the OLD SNAPSHOT.
    assert "Second operator body" in conflict_path.read_text()


async def test_resolve_canonical_render_uses_provided_path_and_slug(
    tmp_path: Path,
) -> None:
    """Rendered frontmatter carries the slug and the lithos_path-derived
    fields, even when the canonical Note's own ``.path`` is empty."""
    local = tmp_path / "vault" / "_lithos" / "projects" / "loom" / "context.md"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("---\nlithos_id: doc-1\n---\n# T\n\nOperator\n")
    conflicts_dir = tmp_path / "vault" / "_lithos" / "conflicts"
    sync_state = ProjectionSyncState()
    canonical = _note(body="Server body")
    # Path field empty in canonical (mirrors lithos_read's actual
    # response shape; see lithos_client.py docstring).
    assert canonical.path == ""

    await resolve_conflict(
        local_path=local,
        canonical_note=canonical,
        canonical_lithos_path="projects/loom/context.md",
        conflicts_dir=conflicts_dir,
        slug="loom",
        filename="context.md",
        sync_state=sync_state,
        doc_id="doc-1",
    )

    rendered = local.read_text()
    fm, _ = extract_frontmatter(rendered)
    assert fm.get("slug") == "loom"


# ── render byte-stability ──────────────────────────────────────────────


async def test_resolved_canonical_renders_byte_stable_with_render_doc(
    tmp_path: Path,
) -> None:
    """The canonical body written by the resolver must be byte-equal
    to ``render_doc(canonical)`` — otherwise the sync_state.file_hash
    we recorded BEFORE the write doesn't match the on-disk bytes,
    breaking dir-watcher self-write suppression."""
    import dataclasses

    local = tmp_path / "vault" / "_lithos" / "projects" / "loom" / "context.md"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("---\nlithos_id: doc-1\n---\n# T\n\nOperator\n")
    conflicts_dir = tmp_path / "vault" / "_lithos" / "conflicts"
    sync_state = ProjectionSyncState()
    canonical = _note(body="Server")

    await resolve_conflict(
        local_path=local,
        canonical_note=canonical,
        canonical_lithos_path="projects/loom/context.md",
        conflicts_dir=conflicts_dir,
        slug="loom",
        filename="context.md",
        sync_state=sync_state,
        doc_id="doc-1",
    )

    on_disk = local.read_text()
    expected = render_doc(
        dataclasses.replace(canonical, path="projects/loom/context.md", slug="loom")
    )
    assert on_disk == expected
