"""Tests for the github-watcher per-project CLI subcommands.

Per-project config lives in the project-context doc's metadata:

- ``project add-github-repo`` appends to ``github_repos``.
- ``project remove-github-repo`` drops from ``github_repos``.
- ``project enable-github`` sets ``github_watch_enabled = true`` (needs a repo).
- ``project disable-github`` sets ``github_watch_enabled = false``.
- ``project migrate-github-tags`` ports legacy tag-based config to metadata.

The repo commands share ``mutate_project_context_metadata`` which handles
read → mutate → CAS-write with version-conflict retry. Tests cover both
the pure helpers and the CLI integration with a stubbed ``LithosClient``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from lithos_loom.cli._github_metadata import (
    GITHUB_REPOS_KEY,
    GITHUB_WATCH_KEY,
    GithubMetadataError,
    extract_github_repos,
    is_github_watching,
    validate_github_repo,
)
from lithos_loom.cli.project import project_app
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import Note, NoteSummary, WriteResult

# ── Pure helpers ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "agent-lore/lithos-loom",
        "a/b",
        "Owner-1/repo_with.dots",
        "ORG/Name.With-Dashes",
    ],
)
def test_validate_github_repo_accepts_valid(value: str) -> None:
    assert validate_github_repo(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "no-slash",
        "/no-owner",
        "no-name/",
        "with spaces/repo",
        "-leading-hyphen/repo",
        "owner/",
        "double//slash",
        "owner/repo/extra",
    ],
)
def test_validate_github_repo_rejects_invalid(value: str) -> None:
    with pytest.raises(GithubMetadataError, match="invalid github repo"):
        validate_github_repo(value)


def test_extract_github_repos_reads_metadata_list() -> None:
    meta = {GITHUB_REPOS_KEY: ["agent-lore/lithos-loom", "agent-lore/lithos"]}
    assert extract_github_repos(meta) == [
        "agent-lore/lithos-loom",
        "agent-lore/lithos",
    ]


def test_extract_github_repos_empty_when_absent() -> None:
    assert extract_github_repos({}) == []
    assert extract_github_repos({"other": 1}) == []


def test_is_github_watching_reads_flag() -> None:
    assert is_github_watching({GITHUB_WATCH_KEY: True}) is True
    assert is_github_watching({GITHUB_WATCH_KEY: False}) is False
    assert is_github_watching({}) is False


# ── CLI test plumbing ─────────────────────────────────────────────────


def _write_config(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f"""
[orchestrator]
agent_id = "test-agent"
lithos_url = "http://localhost:8765"

[obsidian_sync]
vault_path = "{tmp_path / "vault"}"
""",
        encoding="utf-8",
    )
    return cfg_path


def _make_note(
    *,
    doc_id: str = "doc-1",
    metadata: dict[str, Any] | None = None,
    version: int = 1,
    path: str = "projects/my-slug/my-slug-project-context.md",
) -> Note:
    return Note(
        id=doc_id,
        title="My Slug",
        body="body",
        version=version,
        updated_at=datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        tags=("project-context",),
        status="active",
        note_type="concept",
        path=path,
        slug="my-slug",
        metadata=metadata or {},
    )


def _stub_client(
    *,
    initial_note: Note,
    write_result: WriteResult | None = None,
    note_read_sequence: list[Note] | None = None,
    write_sequence: list[WriteResult] | None = None,
) -> AsyncMock:
    """Build an AsyncMock LithosClient with the typical happy-path defaults.

    Pass ``note_read_sequence`` / ``write_sequence`` to drive CAS-retry
    scenarios (multiple reads, multiple writes).
    """
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def aexit(exc_type: type | None, exc: BaseException | None, tb: Any) -> None:
        # Mirror the anyio.TaskGroup wrap so tests can see whether
        # exceptions escape inside or outside the async-with block.
        if exc is not None:
            raise BaseExceptionGroup("simulated anyio wrap", [exc])

    client.__aexit__.side_effect = aexit

    if note_read_sequence is not None:
        client.note_read.side_effect = note_read_sequence
    else:
        client.note_read.return_value = initial_note

    if write_sequence is not None:
        client.note_write.side_effect = write_sequence
    elif write_result is not None:
        client.note_write.return_value = write_result
    else:
        client.note_write.return_value = WriteResult(
            status="updated", note=initial_note
        )
    return client


# ── add-github-repo ───────────────────────────────────────────────────


def test_add_github_repo_writes_metadata(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    client = _stub_client(initial_note=_make_note(metadata={}))
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            [
                "add-github-repo",
                "-c",
                str(cfg_path),
                "my-slug",
                "agent-lore/lithos-loom",
            ],
        )
    assert result.exit_code == 0, result.stdout
    client.note_write.assert_awaited_once()
    written = client.note_write.await_args.kwargs
    assert written["id"] == "doc-1"
    assert written["expected_version"] == 1
    assert written["metadata"][GITHUB_REPOS_KEY] == ["agent-lore/lithos-loom"]
    # Tags are not touched by the metadata write.
    assert "tags" not in written or written["tags"] is None
    assert "added" in result.stdout


def test_add_github_repo_appends_to_existing_list(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(metadata={GITHUB_REPOS_KEY: ["agent-lore/lithos-loom"]})
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["add-github-repo", "-c", str(cfg_path), "my-slug", "agent-lore/lithos"],
        )
    assert result.exit_code == 0, result.stdout
    written = client.note_write.await_args.kwargs["metadata"]
    assert written[GITHUB_REPOS_KEY] == ["agent-lore/lithos-loom", "agent-lore/lithos"]


def test_add_github_repo_idempotent_when_present(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(metadata={GITHUB_REPOS_KEY: ["agent-lore/lithos-loom"]})
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            [
                "add-github-repo",
                "-c",
                str(cfg_path),
                "my-slug",
                "agent-lore/lithos-loom",
            ],
        )
    assert result.exit_code == 0, result.stdout
    client.note_write.assert_not_called()
    assert "already mapped" in result.stdout


def test_add_github_repo_invalid_repo_format(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        project_app,
        ["add-github-repo", "-c", str(cfg_path), "my-slug", "not-a-valid-repo"],
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "invalid github repo" in combined


def test_add_github_repo_doc_not_found(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def aexit(exc_type: type | None, exc: BaseException | None, tb: Any) -> None:
        if exc is not None:
            raise BaseExceptionGroup("wrap", [exc])

    client.__aexit__.side_effect = aexit
    client.note_read.return_value = None
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["add-github-repo", "-c", str(cfg_path), "my-slug", "x/y"],
        )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "no canonical project-context doc" in combined


def test_add_github_repo_invalid_slug(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        project_app,
        ["add-github-repo", "-c", str(cfg_path), "BadSlug!", "x/y"],
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "invalid slug" in combined


# ── remove-github-repo ────────────────────────────────────────────────


def test_remove_github_repo_drops_from_list(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(
        metadata={GITHUB_REPOS_KEY: ["agent-lore/lithos-loom", "agent-lore/lithos"]}
    )
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["remove-github-repo", "-c", str(cfg_path), "my-slug", "agent-lore/lithos"],
        )
    assert result.exit_code == 0, result.stdout
    written = client.note_write.await_args.kwargs["metadata"]
    assert written[GITHUB_REPOS_KEY] == ["agent-lore/lithos-loom"]


def test_remove_github_repo_idempotent_when_absent(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(metadata={GITHUB_REPOS_KEY: ["agent-lore/lithos-loom"]})
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["remove-github-repo", "-c", str(cfg_path), "my-slug", "other/repo"],
        )
    assert result.exit_code == 0, result.stdout
    client.note_write.assert_not_called()
    assert "not mapped" in result.stdout


def test_remove_last_repo_warns_when_still_watching(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(
        metadata={
            GITHUB_REPOS_KEY: ["agent-lore/lithos-loom"],
            GITHUB_WATCH_KEY: True,
        }
    )
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            [
                "remove-github-repo",
                "-c",
                str(cfg_path),
                "my-slug",
                "agent-lore/lithos-loom",
            ],
        )
    assert result.exit_code == 0, result.stdout
    written = client.note_write.await_args.kwargs["metadata"]
    assert written[GITHUB_REPOS_KEY] == []
    assert "warning" in result.stdout


# ── enable-github ─────────────────────────────────────────────────────


def test_enable_github_sets_flag(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(metadata={GITHUB_REPOS_KEY: ["agent-lore/lithos-loom"]})
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["enable-github", "-c", str(cfg_path), "my-slug"],
        )
    assert result.exit_code == 0, result.stdout
    written = client.note_write.await_args.kwargs["metadata"]
    assert written[GITHUB_WATCH_KEY] is True
    # Repo mapping preserved.
    assert written[GITHUB_REPOS_KEY] == ["agent-lore/lithos-loom"]


def test_enable_github_idempotent_when_already_watching(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(
        metadata={
            GITHUB_REPOS_KEY: ["agent-lore/lithos-loom"],
            GITHUB_WATCH_KEY: True,
        }
    )
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["enable-github", "-c", str(cfg_path), "my-slug"],
        )
    assert result.exit_code == 0
    client.note_write.assert_not_called()
    assert "already enabled" in result.stdout


def test_enable_github_requires_repo_first(tmp_path: Path) -> None:
    """No github_repos → operator-actionable error, no write."""
    cfg_path = _write_config(tmp_path)
    client = _stub_client(initial_note=_make_note(metadata={}))
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["enable-github", "-c", str(cfg_path), "my-slug"],
        )
    assert result.exit_code == 2
    client.note_write.assert_not_called()
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "no github repos mapped" in combined


# ── disable-github ────────────────────────────────────────────────────


def test_disable_github_clears_flag(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(
        metadata={
            GITHUB_REPOS_KEY: ["agent-lore/lithos-loom"],
            GITHUB_WATCH_KEY: True,
        }
    )
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["disable-github", "-c", str(cfg_path), "my-slug"],
        )
    assert result.exit_code == 0, result.stdout
    written = client.note_write.await_args.kwargs["metadata"]
    assert written[GITHUB_WATCH_KEY] is False
    # Repo mapping preserved.
    assert written[GITHUB_REPOS_KEY] == ["agent-lore/lithos-loom"]


def test_disable_github_idempotent_when_already_disabled(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    initial = _make_note(metadata={GITHUB_REPOS_KEY: ["agent-lore/lithos-loom"]})
    client = _stub_client(initial_note=initial)
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["disable-github", "-c", str(cfg_path), "my-slug"],
        )
    assert result.exit_code == 0
    client.note_write.assert_not_called()
    assert "already disabled" in result.stdout


# ── CAS retry ─────────────────────────────────────────────────────────


def test_cas_retries_on_version_conflict(tmp_path: Path) -> None:
    """A version_conflict on first write triggers re-read + retry, and the
    re-applied mutator preserves the concurrent writer's metadata key."""
    cfg_path = _write_config(tmp_path)
    note_v1 = _make_note(version=1, metadata={})
    # Concurrent writer landed; v2 added an unrelated metadata key.
    note_v2 = _make_note(version=2, metadata={"unrelated": "value"})

    client = _stub_client(
        initial_note=note_v1,
        note_read_sequence=[note_v1, note_v2],
        write_sequence=[
            WriteResult(status="version_conflict", current_version=2),
            WriteResult(status="updated", note=note_v2),
        ],
    )
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["add-github-repo", "-c", str(cfg_path), "my-slug", "x/y"],
        )
    assert result.exit_code == 0, result.stdout
    assert client.note_read.await_count == 2
    assert client.note_write.await_count == 2
    second_write = client.note_write.await_args_list[1].kwargs["metadata"]
    assert second_write["unrelated"] == "value"
    assert second_write[GITHUB_REPOS_KEY] == ["x/y"]
    assert client.note_write.await_args_list[1].kwargs["expected_version"] == 2


def test_cas_exhausts_after_three_conflicts(tmp_path: Path) -> None:
    """Three back-to-back conflicts surface a friendly error, no spinning."""
    cfg_path = _write_config(tmp_path)
    note = _make_note()
    client = _stub_client(
        initial_note=note,
        note_read_sequence=[note, note, note],
        write_sequence=[
            WriteResult(status="version_conflict", current_version=1),
            WriteResult(status="version_conflict", current_version=1),
            WriteResult(status="version_conflict", current_version=1),
        ],
    )
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["add-github-repo", "-c", str(cfg_path), "my-slug", "x/y"],
        )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "CAS attempts" in combined


def test_unexpected_write_status_raises(tmp_path: Path) -> None:
    """A write status outside the documented set surfaces a typed error."""
    cfg_path = _write_config(tmp_path)
    note = _make_note()
    client = _stub_client(
        initial_note=note,
        write_result=WriteResult(
            status="content_too_large",
            message="body exceeds 1MB limit",
        ),
    )
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["add-github-repo", "-c", str(cfg_path), "my-slug", "x/y"],
        )
    assert result.exit_code == 1


def test_oserror_during_read_surfaces_cleanly(tmp_path: Path) -> None:
    """Transport failure during note_read still reaches the typed handler."""
    cfg_path = _write_config(tmp_path)
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def aexit(exc_type: type | None, exc: BaseException | None, tb: Any) -> None:
        if exc is not None:
            raise BaseExceptionGroup("wrap", [exc])

    client.__aexit__.side_effect = aexit
    client.note_read.side_effect = OSError("connection refused")
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["add-github-repo", "-c", str(cfg_path), "my-slug", "x/y"],
        )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "connection refused" in combined
    exit_call = client.__aexit__.await_args
    assert exit_call is not None
    assert exit_call.args[0] is None


def test_lithos_client_error_during_read_surfaces_cleanly(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def aexit(exc_type: type | None, exc: BaseException | None, tb: Any) -> None:
        if exc is not None:
            raise BaseExceptionGroup("wrap", [exc])

    client.__aexit__.side_effect = aexit
    client.note_read.side_effect = LithosClientError("invalid_input", "bad path")
    runner = CliRunner()

    with patch("lithos_loom.cli._github_metadata.LithosClient", return_value=client):
        result = runner.invoke(
            project_app,
            ["add-github-repo", "-c", str(cfg_path), "my-slug", "x/y"],
        )
    assert result.exit_code == 1


# ── migrate-github-tags ───────────────────────────────────────────────


def _migration_client(
    *,
    summaries: list[NoteSummary],
    notes_by_path: dict[str, Note],
    write_result: WriteResult | None = None,
) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def aexit(exc_type: type | None, exc: BaseException | None, tb: Any) -> None:
        if exc is not None:
            raise BaseExceptionGroup("wrap", [exc])

    client.__aexit__.side_effect = aexit
    client.note_list.return_value = summaries

    async def _read(*, id: str | None = None, path: str | None = None) -> Note | None:
        return notes_by_path.get(path or "")

    client.note_read.side_effect = _read
    client.note_write.return_value = write_result or WriteResult(status="updated")
    return client


def _legacy_summary(*, slug: str, tags: tuple[str, ...]) -> NoteSummary:
    return NoteSummary(
        id=f"doc-{slug}",
        title=slug.title(),
        version=1,
        updated_at=datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        tags=tags,
        status="active",
        note_type="concept",
        path=f"projects/{slug}/{slug}-project-context.md",
        slug=slug,
    )


def _legacy_note(
    *, slug: str, tags: tuple[str, ...], metadata: dict[str, Any] | None = None
) -> Note:
    return Note(
        id=f"doc-{slug}",
        title=slug.title(),
        body="body",
        version=1,
        updated_at=datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC),
        tags=tags,
        status="active",
        note_type="concept",
        path=f"projects/{slug}/{slug}-project-context.md",
        slug=slug,
        metadata=metadata or {},
    )


def test_migrate_github_tags_ports_tags_to_metadata(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    legacy_tags = (
        "project-context",
        "github-repo:agent-lore/lithos-loom",
        "github-repo:agent-lore/lithos",
        "github-watch",
        "github-exclude-label:automated",
    )
    summary = _legacy_summary(slug="my-slug", tags=legacy_tags)
    note = _legacy_note(slug="my-slug", tags=legacy_tags)
    client = _migration_client(
        summaries=[summary],
        notes_by_path={note.path: note},
    )
    runner = CliRunner()

    with patch(
        "lithos_loom.cli._github_tag_migration.LithosClient", return_value=client
    ):
        result = runner.invoke(
            project_app, ["migrate-github-tags", "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    written = client.note_write.await_args.kwargs
    # Both repos collected into the list.
    assert written["metadata"][GITHUB_REPOS_KEY] == [
        "agent-lore/lithos-loom",
        "agent-lore/lithos",
    ]
    assert written["metadata"][GITHUB_WATCH_KEY] is True
    assert written["metadata"]["github_exclude_labels"] == ["automated"]
    # Github tags stripped; project-context kept.
    assert written["tags"] == ["project-context"]
    assert "migrated 1 doc" in result.stdout


def test_migrate_github_tags_dry_run_writes_nothing(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    legacy_tags = ("project-context", "github-repo:o/r", "github-watch")
    client = _migration_client(
        summaries=[_legacy_summary(slug="my-slug", tags=legacy_tags)],
        notes_by_path={
            "projects/my-slug/my-slug-project-context.md": _legacy_note(
                slug="my-slug", tags=legacy_tags
            )
        },
    )
    runner = CliRunner()

    with patch(
        "lithos_loom.cli._github_tag_migration.LithosClient", return_value=client
    ):
        result = runner.invoke(
            project_app, ["migrate-github-tags", "--dry-run", "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    client.note_write.assert_not_called()
    assert "would migrate" in result.stdout


def test_migrate_github_tags_noop_when_no_legacy_tags(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    client = _migration_client(
        summaries=[_legacy_summary(slug="my-slug", tags=("project-context",))],
        notes_by_path={},
    )
    runner = CliRunner()

    with patch(
        "lithos_loom.cli._github_tag_migration.LithosClient", return_value=client
    ):
        result = runner.invoke(
            project_app, ["migrate-github-tags", "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    client.note_write.assert_not_called()
    assert "nothing to do" in result.stdout


def test_migrate_github_tags_merges_with_existing_metadata(tmp_path: Path) -> None:
    """Mixed-state doc: an operator added a second repo via add-github-repo
    (writing metadata) before migration ran. Migration must UNION the
    tag-derived repo with the existing metadata, not clobber it."""
    cfg_path = _write_config(tmp_path)
    legacy_tags = (
        "project-context",
        "github-repo:agent-lore/lithos-loom",
        "github-watch",
    )
    # Existing metadata already carries the legacy repo PLUS a post-deploy add.
    note = _legacy_note(
        slug="my-slug",
        tags=legacy_tags,
        metadata={GITHUB_REPOS_KEY: ["agent-lore/lithos-loom", "agent-lore/new-repo"]},
    )
    client = _migration_client(
        summaries=[_legacy_summary(slug="my-slug", tags=legacy_tags)],
        notes_by_path={note.path: note},
    )
    runner = CliRunner()

    with patch(
        "lithos_loom.cli._github_tag_migration.LithosClient", return_value=client
    ):
        result = runner.invoke(
            project_app, ["migrate-github-tags", "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    written = client.note_write.await_args.kwargs["metadata"]
    # The post-deploy repo is preserved; the legacy repo is not duplicated.
    assert written[GITHUB_REPOS_KEY] == [
        "agent-lore/lithos-loom",
        "agent-lore/new-repo",
    ]


def test_migrate_github_tags_existing_disable_wins_over_watch_tag(
    tmp_path: Path,
) -> None:
    """A post-deploy `disable-github` (metadata github_watch_enabled=False)
    must not be re-enabled by a stale `github-watch` tag."""
    cfg_path = _write_config(tmp_path)
    legacy_tags = ("project-context", "github-repo:o/r", "github-watch")
    note = _legacy_note(
        slug="my-slug",
        tags=legacy_tags,
        metadata={GITHUB_REPOS_KEY: ["o/r"], GITHUB_WATCH_KEY: False},
    )
    client = _migration_client(
        summaries=[_legacy_summary(slug="my-slug", tags=legacy_tags)],
        notes_by_path={note.path: note},
    )
    runner = CliRunner()

    with patch(
        "lithos_loom.cli._github_tag_migration.LithosClient", return_value=client
    ):
        result = runner.invoke(
            project_app, ["migrate-github-tags", "-c", str(cfg_path)]
        )
    assert result.exit_code == 0, result.stdout
    written = client.note_write.await_args.kwargs["metadata"]
    assert written[GITHUB_WATCH_KEY] is False


def test_migrate_github_tags_filters_to_project_context(tmp_path: Path) -> None:
    """The scan is scoped to project-context docs so github-* tags on an
    unrelated doc under projects/ are never stripped."""
    cfg_path = _write_config(tmp_path)
    client = _migration_client(summaries=[], notes_by_path={})
    runner = CliRunner()

    with patch(
        "lithos_loom.cli._github_tag_migration.LithosClient", return_value=client
    ):
        runner.invoke(project_app, ["migrate-github-tags", "-c", str(cfg_path)])
    assert client.note_list.await_args.kwargs["tags"] == ["project-context"]


def test_is_github_watching_rejects_non_bool() -> None:
    """A hand-edited string `"false"` must not read as enabled."""
    assert is_github_watching({GITHUB_WATCH_KEY: "false"}) is False
    assert is_github_watching({GITHUB_WATCH_KEY: 1}) is False


def test_extract_github_repos_ignores_non_string_junk() -> None:
    """Non-string list elements are dropped, not coerced to "None"/"123"."""
    meta = {GITHUB_REPOS_KEY: ["o/r", None, 42, "o/r", ""]}
    assert extract_github_repos(meta) == ["o/r"]
