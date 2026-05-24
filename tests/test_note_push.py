"""Tests for ``lithos_loom.subscriptions._note_push`` (Slice 5 US34).

The handler consumes ``obsidian.note.modified`` events emitted by the
dir-watcher and pushes the body to Lithos via ``note_write`` with
optimistic locking. Tests inject a fake LithosClient and exercise the
handler end-to-end without an HTTP call.
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
from lithos_loom.lithos_client import Note, WriteResult
from lithos_loom.render_project_context import extract_frontmatter
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._note_push import make_handler
from lithos_loom.sync_state import ProjectionSyncState


def _note(
    *,
    doc_id: str = "doc-1",
    title: str = "Lithos Loom",
    body: str = "Server body",
    version: int = 5,
    tags: tuple[str, ...] = ("project-context",),
    status: str | None = "active",
    note_type: str | None = "concept",
    path: str = "",
) -> Note:
    return Note(
        id=doc_id,
        title=title,
        body=body,
        version=version,
        updated_at=datetime(2026, 5, 24, 14, 30, 0, tzinfo=UTC),
        tags=tags,
        status=status,
        note_type=note_type,
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
        logger=logging.getLogger("test.note_push"),
        agent_id="lithos-orchestrator-test",
    )


def _event(
    *,
    doc_id: str = "doc-1",
    lithos_version: int = 5,
    body: str = "# Lithos Loom\n\nOperator's new body\n",
    slug: str = "lithos-loom",
    filename: str = "context.md",
    vault_path: Path | None = None,
) -> Event:
    payload = {
        "lithos_id": doc_id,
        "lithos_version": lithos_version,
        "body": body,
        "slug": slug,
        "filename": filename,
        "vault_path": str(vault_path) if vault_path else "/tmp/test.md",
    }
    return Event(
        type="obsidian.note.modified",
        timestamp=datetime.now(UTC),
        payload=MappingProxyType(payload),
    )


def _setup_local_file(tmp_path: Path) -> Path:
    """Create a vault file the handler can read+rewrite."""
    p = tmp_path / "vault" / "_lithos" / "projects" / "lithos-loom" / "context.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\nlithos_id: doc-1\nlithos_version: 5\n---\n"
        "# Lithos Loom\n\nOld local body\n",
        encoding="utf-8",
    )
    return p


# ── Happy path ─────────────────────────────────────────────────────────


async def test_updated_status_pushes_body_and_refreshes_frontmatter(
    tmp_path: Path,
) -> None:
    """Real Lithos's ``updated`` envelope is top-level
    ``{status, id, path, version, warnings}`` — no ``document``
    field, so ``WriteResult.note`` is ``None``. The handler MUST
    re-fetch via ``note_read`` to get the bumped version, otherwise
    the local frontmatter would carry the stale pre-write version
    and every subsequent edit would immediate-conflict."""
    cfg = _cfg(tmp_path)
    local = _setup_local_file(tmp_path)
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    lithos = AsyncMock()
    # Two note_read calls expected: pre-write (canonical at v5) and
    # post-write (canonical at v6 after server bump).
    lithos.note_read.side_effect = [
        _note(version=5, body="Old server body"),
        _note(version=6, body="Operator's new body"),
    ]
    # Production-shaped success: status=updated, note=None.
    lithos.note_write.return_value = WriteResult(status="updated", note=None)

    await handler(
        _event(vault_path=local, body="# Lithos Loom\n\nOperator's new body\n"),
        _ctx(lithos),
    )

    # note_write called with expected_version + the operator's body.
    lithos.note_write.assert_awaited_once()
    kwargs = lithos.note_write.await_args.kwargs
    assert kwargs["id"] == "doc-1"
    assert kwargs["expected_version"] == 5
    assert kwargs["content"] == "# Lithos Loom\n\nOperator's new body\n"
    assert kwargs["title"] == "Lithos Loom"
    assert kwargs["tags"] == ["project-context"]
    assert kwargs["note_type"] == "concept"
    assert kwargs["status"] == "active"

    # Pinned: post-write note_read happens (the bug-fix invariant).
    # Without this, the local frontmatter would have the stale v5.
    assert lithos.note_read.await_count == 2

    # Local file refreshed with the SERVER-bumped version (v6), not
    # the pre-write current.version (v5).
    rendered = local.read_text()
    fm, _ = extract_frontmatter(rendered)
    assert fm["lithos_version"] == 6

    # sync_state records the new file hash so the dir-watcher's next
    # poll absorbs this rewrite as a self-write.
    assert "doc-1" in sync_state.note_file_hashes
    assert "doc-1" in sync_state.note_body_hashes
    assert sync_state.note_versions["doc-1"] == 6


async def test_post_write_fetch_vanished_skips_refresh(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If the doc is deleted between the successful push and the
    post-write fetch (rare race), log and skip the frontmatter
    refresh rather than crashing. Operator's local file is left at
    the pre-push version; the next edit will trigger a conflict
    (which is the right outcome — the doc no longer exists, so the
    operator's edit either gets re-created or surfaces as orphan)."""
    caplog.set_level(logging.WARNING)
    cfg = _cfg(tmp_path)
    local = _setup_local_file(tmp_path)
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    lithos = AsyncMock()
    lithos.note_read.side_effect = [_note(version=5), None]
    lithos.note_write.return_value = WriteResult(status="updated", note=None)

    await handler(_event(vault_path=local), _ctx(lithos))

    assert any("vanished between successful push" in r.message for r in caplog.records)
    # sync_state not updated — we couldn't refresh.
    assert "doc-1" not in sync_state.note_file_hashes


async def test_duplicate_status_skips_rewrite(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``duplicate`` means Lithos saw the body already at this content;
    no version bump, no rewrite needed. Common when re-firing the
    same event (idempotency) — must not noisily complain."""
    caplog.set_level(logging.INFO)
    cfg = _cfg(tmp_path)
    local = _setup_local_file(tmp_path)
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    lithos = AsyncMock()
    lithos.note_read.return_value = _note(version=5)
    lithos.note_write.return_value = WriteResult(status="duplicate")

    original = local.read_text()
    await handler(_event(vault_path=local), _ctx(lithos))

    # File unchanged (no rewrite); no post-write fetch attempted.
    assert local.read_text() == original
    assert lithos.note_read.await_count == 1
    # sync_state untouched — no write happened.
    assert "doc-1" not in sync_state.note_file_hashes
    # Info-level log explains why we skipped (operator-friendly).
    assert any("duplicate" in r.message for r in caplog.records)


# ── Version conflict path ──────────────────────────────────────────────


async def test_version_conflict_invokes_resolver(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    local = _setup_local_file(tmp_path)
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    canonical = _note(version=99, body="Canonical body")
    lithos = AsyncMock()
    # First note_read: pre-write fetch for title/tags.
    # Second note_read (inside conflict path): canonical body.
    lithos.note_read.side_effect = [_note(version=5), canonical]
    lithos.note_write.return_value = WriteResult(
        status="version_conflict", current_version=99
    )

    await handler(_event(vault_path=local), _ctx(lithos))

    # Conflict file exists in conflicts dir.
    conflicts_dir = tmp_path / "vault" / "_lithos" / "conflicts"
    assert conflicts_dir.is_dir()
    conflict_files = list(conflicts_dir.glob("lithos-loom.context.*.md"))
    assert len(conflict_files) == 1
    assert "Old local body" in conflict_files[0].read_text()

    # Canonical body now at original path.
    rendered = local.read_text()
    fm, body = extract_frontmatter(rendered)
    assert fm["lithos_version"] == 99
    assert "Canonical body" in body


async def test_version_conflict_with_vanished_doc_skips_gracefully(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Doc deleted between conflict detection and canonical fetch → skip."""
    caplog.set_level(logging.WARNING)
    cfg = _cfg(tmp_path)
    local = _setup_local_file(tmp_path)
    handler = make_handler(cfg, sync_state=ProjectionSyncState())

    lithos = AsyncMock()
    # First note_read returns the doc; second (inside conflict path)
    # returns None to simulate a delete in the gap.
    lithos.note_read.side_effect = [_note(version=5), None]
    lithos.note_write.return_value = WriteResult(
        status="version_conflict", current_version=99
    )

    await handler(_event(vault_path=local), _ctx(lithos))

    assert any("vanished between" in r.message for r in caplog.records)
    # Local file unchanged (no rewrite).
    assert "Old local body" in local.read_text()


# ── Doc-not-found pre-fetch ────────────────────────────────────────────


async def test_doc_not_found_pre_fetch_skips_gracefully(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Pre-fetch returns None → log + skip, no note_write attempt."""
    caplog.set_level(logging.WARNING)
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, sync_state=ProjectionSyncState())

    lithos = AsyncMock()
    lithos.note_read.return_value = None

    await handler(_event(vault_path=Path("/tmp/x.md")), _ctx(lithos))

    lithos.note_write.assert_not_called()
    assert any("deleted between operator edit" in r.message for r in caplog.records)


# ── Error / unexpected status ──────────────────────────────────────────


async def test_invalid_input_status_leaves_file_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    cfg = _cfg(tmp_path)
    local = _setup_local_file(tmp_path)
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    lithos = AsyncMock()
    lithos.note_read.return_value = _note(version=5)
    lithos.note_write.return_value = WriteResult(
        status="invalid_input", message="body too long"
    )

    original = local.read_text()
    await handler(_event(vault_path=local), _ctx(lithos))

    assert local.read_text() == original
    assert any("invalid_input" in r.message for r in caplog.records)
    # No sync_state entry — we didn't write anything.
    assert "doc-1" not in sync_state.note_file_hashes


# ── Malformed payloads ─────────────────────────────────────────────────


async def test_malformed_payload_skips_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, sync_state=ProjectionSyncState())

    # Missing required fields.
    event = Event(
        type="obsidian.note.modified",
        timestamp=datetime.now(UTC),
        payload=MappingProxyType({"slug": "x"}),
    )
    lithos = AsyncMock()
    await handler(event, _ctx(lithos))

    lithos.note_read.assert_not_called()
    assert any("malformed payload" in r.message for r in caplog.records)


async def test_missing_vault_path_skips_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    cfg = _cfg(tmp_path)
    handler = make_handler(cfg, sync_state=ProjectionSyncState())

    event = Event(
        type="obsidian.note.modified",
        timestamp=datetime.now(UTC),
        payload=MappingProxyType(
            {
                "lithos_id": "doc-1",
                "lithos_version": 5,
                "body": "body",
                "slug": "x",
                "filename": "context.md",
                "vault_path": "",
            }
        ),
    )
    lithos = AsyncMock()
    await handler(event, _ctx(lithos))

    lithos.note_read.assert_not_called()
    assert any("missing vault_path" in r.message for r in caplog.records)


# ── Idempotency ────────────────────────────────────────────────────────


async def test_re_firing_after_successful_push_is_safe(
    tmp_path: Path,
) -> None:
    """Re-firing the same event after a successful push pushes again
    (Lithos bumps version on every write). The local frontmatter ends
    up at the latest version; sync_state tracks each rewrite."""
    cfg = _cfg(tmp_path)
    local = _setup_local_file(tmp_path)
    sync_state = ProjectionSyncState()
    handler = make_handler(cfg, sync_state=sync_state)

    lithos = AsyncMock()
    # Each push: pre-write fetch + post-write fetch. Both pushes
    # land at v6 to model the idempotent re-fire contract (in real
    # Lithos the second push would advance to v7; mocking v6 twice
    # asserts the same-hash assertion below).
    lithos.note_read.side_effect = [
        _note(version=5),
        _note(version=6, body="Operator's new body"),
        _note(version=5),
        _note(version=6, body="Operator's new body"),
    ]
    lithos.note_write.return_value = WriteResult(status="updated", note=None)

    event = _event(vault_path=local)
    await handler(event, _ctx(lithos))
    first_hash = sync_state.note_file_hashes["doc-1"]

    # Re-fire — note_write is called again, but we don't crash.
    await handler(event, _ctx(lithos))
    # sync_state is consistent (same hash because the second call
    # returns the same version).
    assert sync_state.note_file_hashes["doc-1"] == first_hash
    assert lithos.note_write.await_count == 2


# ── make_handler config gate ───────────────────────────────────────────


def test_make_handler_raises_without_obsidian_sync_config() -> None:
    """The supervisor's spawn gate should prevent this, but assert
    defensively."""
    cfg = LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="x",
            lithos_url="http://localhost:8765",
        ),
        obsidian_sync=None,
    )
    with pytest.raises(RuntimeError, match=r"without \[obsidian_sync\] config"):
        make_handler(cfg)
