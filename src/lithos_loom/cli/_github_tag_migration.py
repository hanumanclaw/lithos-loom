"""One-shot migration of github-watcher config from tags → metadata.

TRANSITIONAL MODULE. This is the **only** place in the codebase that
still references the legacy ``github-*`` tag strings. It exists to move
project-context docs created under the old tag-based scheme onto the
``github_repos`` / ``github_watch_enabled`` / ``github_exclude_*``
metadata keys (see ADR 0001). Once every live project-context doc has
been migrated, this module and its CLI command can be deleted.

For each project-context doc carrying any legacy github tag it performs
one CAS write that both sets the derived metadata and strips the github
tags. It is idempotent: a second run finds no github tags and is a
no-op. ``--dry-run`` reports what would change without writing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lithos_loom.cli._github_metadata import (
    GITHUB_EXCLUDE_AUTHORS_KEY,
    GITHUB_EXCLUDE_LABELS_KEY,
    GITHUB_REPOS_KEY,
    GITHUB_WATCH_KEY,
    extract_exclude_authors,
    extract_exclude_labels,
    extract_github_repos,
    is_github_watching,
)
from lithos_loom.config import LoomConfig
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import LithosClient

# Legacy tag vocabulary — quarantined here, referenced nowhere else.
_OLD_REPO_PREFIX = "github-repo:"
_OLD_WATCH_TAG = "github-watch"
_OLD_EXCLUDE_LABEL_PREFIX = "github-exclude-label:"
_OLD_EXCLUDE_AUTHOR_PREFIX = "github-exclude-author:"

# Only project-context docs carry github-watcher config; scope the scan
# so we never strip github-* tags off an unrelated doc under projects/.
_PROJECT_CONTEXT_TAG = "project-context"

_MAX_CAS_ATTEMPTS = 3


@dataclass(frozen=True)
class MigrationItem:
    """Per-doc outcome of the tag→metadata migration."""

    slug: str
    path: str
    repos: list[str]
    watch_enabled: bool
    exclude_labels: list[str] = field(default_factory=list)
    exclude_authors: list[str] = field(default_factory=list)
    status: str = "migrated"  # migrated | would-migrate | conflict-failed


def _dedup(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v and v not in seen:
            out.append(v)
            seen.add(v)
    return out


@dataclass(frozen=True)
class _LegacyConfig:
    repos: list[str]
    watch_enabled: bool
    exclude_labels: list[str]
    exclude_authors: list[str]
    kept_tags: list[str]
    has_any: bool


def _read_legacy_tags(tags: tuple[str, ...] | list[str]) -> _LegacyConfig:
    repos: list[str] = []
    labels: list[str] = []
    authors: list[str] = []
    kept: list[str] = []
    watch = False
    has_any = False
    for tag in tags:
        if tag == _OLD_WATCH_TAG:
            watch = True
            has_any = True
        elif tag.startswith(_OLD_REPO_PREFIX):
            repos.append(tag[len(_OLD_REPO_PREFIX) :])
            has_any = True
        elif tag.startswith(_OLD_EXCLUDE_LABEL_PREFIX):
            labels.append(tag[len(_OLD_EXCLUDE_LABEL_PREFIX) :])
            has_any = True
        elif tag.startswith(_OLD_EXCLUDE_AUTHOR_PREFIX):
            authors.append(tag[len(_OLD_EXCLUDE_AUTHOR_PREFIX) :])
            has_any = True
        else:
            kept.append(tag)
    return _LegacyConfig(
        repos=_dedup(repos),
        watch_enabled=watch,
        exclude_labels=_dedup(labels),
        exclude_authors=_dedup(authors),
        kept_tags=kept,
        has_any=has_any,
    )


@dataclass(frozen=True)
class _MergedConfig:
    repos: list[str]
    watch_enabled: bool
    exclude_labels: list[str]
    exclude_authors: list[str]


def _merge_config(legacy: _LegacyConfig, existing: dict[str, object]) -> _MergedConfig:
    """Merge the tag-derived config with any github metadata already on
    the doc, existing metadata winning where it conflicts.

    A doc can be in a mixed state: an operator may have run
    ``add-github-repo`` / ``enable-github`` / ``disable-github`` (writing
    metadata) after the daemon was upgraded but before this migration
    ran. Rebuilding purely from tags would clobber those edits, so:

    - ``repos``: union (existing first, then tag repos not already
      present) — never drops a repo the operator added post-upgrade.
    - ``watch_enabled``: existing value wins when the key is present (a
      post-upgrade ``disable-github`` must not be re-enabled by a stale
      ``github-watch`` tag); otherwise the tag value.
    - exclude lists: union.
    """
    return _MergedConfig(
        repos=_dedup([*extract_github_repos(existing), *legacy.repos]),
        watch_enabled=(
            is_github_watching(existing)
            if GITHUB_WATCH_KEY in existing
            else legacy.watch_enabled
        ),
        exclude_labels=_dedup(
            [*extract_exclude_labels(existing), *legacy.exclude_labels]
        ),
        exclude_authors=_dedup(
            [*extract_exclude_authors(existing), *legacy.exclude_authors]
        ),
    )


def _to_write_metadata(merged: _MergedConfig) -> dict[str, object]:
    meta: dict[str, object] = {
        GITHUB_REPOS_KEY: merged.repos,
        GITHUB_WATCH_KEY: merged.watch_enabled,
    }
    if merged.exclude_labels:
        meta[GITHUB_EXCLUDE_LABELS_KEY] = merged.exclude_labels
    if merged.exclude_authors:
        meta[GITHUB_EXCLUDE_AUTHORS_KEY] = merged.exclude_authors
    return meta


async def migrate_github_tags(*, cfg: LoomConfig, dry_run: bool) -> list[MigrationItem]:
    """Migrate every project-context doc carrying legacy github tags.

    Returns one :class:`MigrationItem` per affected doc. Docs with no
    github tags are silently skipped. Raises ``LithosClientError`` /
    ``OSError`` only on transport failure of the initial enumeration;
    per-doc CAS exhaustion is reported as a ``conflict-failed`` item
    rather than aborting the whole run.
    """
    deferred: LithosClientError | OSError | None = None
    items: list[MigrationItem] = []
    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        try:
            summaries = await client.note_list(
                path_prefix="projects/",
                tags=[_PROJECT_CONTEXT_TAG],
                limit=1000,
            )
            for summary in summaries:
                legacy = _read_legacy_tags(summary.tags)
                if not legacy.has_any:
                    continue
                item = await _migrate_one(
                    client=client,
                    path=summary.path,
                    slug=summary.slug,
                    dry_run=dry_run,
                )
                if item is not None:
                    items.append(item)
        except (LithosClientError, OSError) as exc:
            deferred = exc
    if deferred is not None:
        raise deferred
    return items


async def _migrate_one(
    *, client: LithosClient, path: str, slug: str, dry_run: bool
) -> MigrationItem | None:
    for _attempt in range(_MAX_CAS_ATTEMPTS):
        note = await client.note_read(path=path)
        if note is None:
            return None
        legacy = _read_legacy_tags(note.tags)
        if not legacy.has_any:
            # Raced with another migrator (or already clean) — nothing to do.
            return None
        merged = _merge_config(legacy, dict(note.metadata))
        if dry_run:
            return MigrationItem(
                slug=slug,
                path=note.path,
                repos=merged.repos,
                watch_enabled=merged.watch_enabled,
                exclude_labels=merged.exclude_labels,
                exclude_authors=merged.exclude_authors,
                status="would-migrate",
            )
        write_result = await client.note_write(
            id=note.id,
            title=note.title,
            content=note.body,
            tags=legacy.kept_tags,
            metadata=_to_write_metadata(merged),
            expected_version=note.version,
            note_type=note.note_type or "concept",
        )
        if write_result.status in ("created", "updated"):
            return MigrationItem(
                slug=slug,
                path=note.path,
                repos=merged.repos,
                watch_enabled=merged.watch_enabled,
                exclude_labels=merged.exclude_labels,
                exclude_authors=merged.exclude_authors,
                status="migrated",
            )
        if write_result.status == "version_conflict":
            continue
        raise LithosClientError(
            code=write_result.status,
            message=(
                write_result.message
                or f"unexpected note_write status {write_result.status!r}"
            ),
        )
    return MigrationItem(
        slug=slug,
        path=path,
        repos=[],
        watch_enabled=False,
        status="conflict-failed",
    )
