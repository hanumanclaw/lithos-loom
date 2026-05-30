"""Helpers for the github-watcher CLI subcommands.

The watcher's per-project config (which repo, watching on/off, exclude
filters) is persisted as **tags** on the canonical project-context
doc. Tags are the only field on Lithos's note-write MCP surface that
accepts free-form values today, so they're the storage layer:

- ``github-repo:<owner>/<name>`` — exactly one per project; presence
  is what makes the watcher consider this project.
- ``github-watch`` — presence means watching is enabled; absence
  means paused. Removing it lets the operator stop the watcher
  without losing the repo mapping.
- ``github-exclude-label:<name>`` — at import time, issues carrying
  this label are skipped before ``task_create``. Already-linked tasks
  are unaffected (PRD: "exclude is only at import time").
- ``github-exclude-author:<login>`` — same shape; filters by GH author.

This module hosts the read-mutate-write CAS loop the three CLI
subcommands share. ``project.py`` consumes it via thin Typer
wrappers, mirroring the ``_project_import_bulk`` / ``_regenerate_done``
extraction pattern.

Exclude tag values are exact-match strings; GitHub label names that
contain characters outside the tag-safe set (e.g. spaces) cannot be
filtered today — operators with such labels rename them or rely on
the broader author filter.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from lithos_loom.config import LoomConfig
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import LithosClient, Note

__all__ = [
    "GITHUB_EXCLUDE_AUTHOR_PREFIX",
    "GITHUB_EXCLUDE_LABEL_PREFIX",
    "GITHUB_REPO_TAG_PREFIX",
    "GITHUB_WATCH_TAG",
    "GithubMetadataError",
    "NoteMutationResult",
    "extract_exclude_authors",
    "extract_exclude_labels",
    "extract_github_repo",
    "is_github_watching",
    "mutate_project_context_tags",
    "validate_github_repo",
]

GITHUB_REPO_TAG_PREFIX = "github-repo:"
GITHUB_WATCH_TAG = "github-watch"
GITHUB_EXCLUDE_LABEL_PREFIX = "github-exclude-label:"
GITHUB_EXCLUDE_AUTHOR_PREFIX = "github-exclude-author:"

# GitHub's repo-name rules: owner is 1–39 alphanumerics with hyphens
# allowed inside (no leading/trailing hyphen); name is 1–100 chars of
# alphanumerics, hyphens, underscores, dots. We enforce the shape at
# CLI input time so a typo doesn't sit on the doc waiting for the next
# poll to surface "repo not found".
_REPO_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?"
    r"/"
    r"[A-Za-z0-9_.](?:[A-Za-z0-9_.-]{0,99})?$"
)

# CAS retries before declaring the doc too contended to mutate. Two
# attempts covers the realistic case of a single concurrent writer.
_MAX_CAS_ATTEMPTS = 3


class GithubMetadataError(Exception):
    """Raised when the CLI cannot complete a project-context tag mutation.

    Wraps the user-actionable cases: doc not found for the slug,
    invalid ``owner/name`` form, exhausted CAS retries. The CLI
    surfaces ``.args[0]`` directly to stderr.
    """


@dataclass(frozen=True)
class NoteMutationResult:
    """Outcome of a tag-mutation round-trip.

    ``new_tags`` carries the post-write tag list so the CLI can print
    a confirmation that reflects the actual state in Lithos, not the
    diff the operator requested.
    """

    note_id: str
    path: str
    new_tags: tuple[str, ...]
    changed: bool


# ── Validation ────────────────────────────────────────────────────────


def validate_github_repo(value: str) -> str:
    """Return ``value`` if it matches ``owner/name``, else raise.

    Strict by design: a typo like ``owner.name`` (no slash) would
    silently get stored and surface much later as a 404 from the
    watcher. Failing at CLI input keeps the blame close to the cause.
    """
    if not _REPO_RE.match(value):
        raise GithubMetadataError(
            f"invalid github repo {value!r}: expected 'owner/name' "
            "(alphanumerics, hyphens; name may also contain _ and .)"
        )
    return value


# ── Tag inspection ────────────────────────────────────────────────────


def extract_github_repo(tags: tuple[str, ...] | list[str]) -> str | None:
    """Return the ``owner/name`` carried by a ``github-repo:`` tag.

    Returns ``None`` when no such tag is present. If multiple are
    present (the CLI never writes more than one but operator drift
    could), the first wins — the watcher loops on the same canonical
    list every poll so an ambiguous repo set would be visible from
    the doc anyway.
    """
    for tag in tags:
        if tag.startswith(GITHUB_REPO_TAG_PREFIX):
            return tag[len(GITHUB_REPO_TAG_PREFIX) :]
    return None


def is_github_watching(tags: tuple[str, ...] | list[str]) -> bool:
    return GITHUB_WATCH_TAG in tags


def extract_exclude_labels(tags: tuple[str, ...] | list[str]) -> list[str]:
    """Return the GH-label names this project excludes at import time.

    Order-preserving so the operator's tag order in the doc is the
    order shown in logs. Duplicate tags collapse — re-adding the same
    exclude in the doc is a no-op rather than counted twice.
    """
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        if tag.startswith(GITHUB_EXCLUDE_LABEL_PREFIX):
            value = tag[len(GITHUB_EXCLUDE_LABEL_PREFIX) :]
            if value and value not in seen:
                result.append(value)
                seen.add(value)
    return result


def extract_exclude_authors(tags: tuple[str, ...] | list[str]) -> list[str]:
    """Return the GH author logins this project excludes at import time."""
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        if tag.startswith(GITHUB_EXCLUDE_AUTHOR_PREFIX):
            value = tag[len(GITHUB_EXCLUDE_AUTHOR_PREFIX) :]
            if value and value not in seen:
                result.append(value)
                seen.add(value)
    return result


# ── Mutation ──────────────────────────────────────────────────────────


def _canonical_project_context_path(slug: str) -> str:
    return f"projects/{slug}/{slug}-project-context.md"


async def mutate_project_context_tags(
    *,
    cfg: LoomConfig,
    slug: str,
    mutator: Callable[[list[str]], list[str]],
    action_summary: str,
) -> NoteMutationResult:
    """Apply ``mutator`` to a project-context doc's tag list and CAS-write.

    Wraps the full read-mutate-write loop:

    1. Open a single ``LithosClient`` context.
    2. ``note_read`` the canonical ``projects/<slug>/<slug>-project-
       context.md``.
    3. Apply ``mutator`` (which receives a copy of the current tags
       and returns the new list).
    4. ``note_write`` with ``expected_version`` for CAS protection.
    5. On ``version_conflict``: re-read and re-apply the mutator. Up
       to :data:`_MAX_CAS_ATTEMPTS` total attempts before giving up.

    ``action_summary`` is interpolated into the version-exhaustion
    error so the caller doesn't have to reconstruct it.

    Errors are deferred outside the ``async with`` block so the
    typed exceptions reach the caller without anyio's task-group
    BaseExceptionGroup wrap (same defence as
    ``_create_project_async`` in ``project.py``).
    """
    doc_path = _canonical_project_context_path(slug)
    deferred_error: GithubMetadataError | LithosClientError | OSError | None = None
    result: NoteMutationResult | None = None

    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        try:
            result = await _mutate_with_cas(
                client=client,
                doc_path=doc_path,
                slug=slug,
                mutator=mutator,
                action_summary=action_summary,
            )
        except (GithubMetadataError, LithosClientError, OSError) as exc:
            deferred_error = exc

    if deferred_error is not None:
        raise deferred_error
    assert result is not None
    return result


async def _mutate_with_cas(
    *,
    client: LithosClient,
    doc_path: str,
    slug: str,
    mutator: Callable[[list[str]], list[str]],
    action_summary: str,
) -> NoteMutationResult:
    for _attempt in range(_MAX_CAS_ATTEMPTS):
        note = await _read_canonical_doc(client, doc_path, slug)
        current_tags = list(note.tags)
        new_tags = mutator(list(current_tags))
        if new_tags == current_tags:
            # No-op: nothing to write. Skip the round-trip; the operator
            # gets idempotent semantics ("already set" is success).
            return NoteMutationResult(
                note_id=note.id,
                path=note.path,
                new_tags=tuple(new_tags),
                changed=False,
            )
        write_result = await client.note_write(
            id=note.id,
            title=note.title,
            content=note.body,
            tags=new_tags,
            expected_version=note.version,
            note_type=note.note_type or "concept",
        )
        if write_result.status in ("created", "updated"):
            return NoteMutationResult(
                note_id=note.id,
                path=note.path,
                new_tags=tuple(new_tags),
                changed=True,
            )
        if write_result.status == "version_conflict":
            # Another writer landed between our read and write; loop
            # and re-apply the mutator to whatever tags now exist.
            continue
        # Any other status is an unrecoverable error.
        raise LithosClientError(
            code=write_result.status,
            message=(
                write_result.message
                or f"unexpected note_write status {write_result.status!r}"
            ),
        )
    raise GithubMetadataError(
        f"could not {action_summary} for slug {slug!r}: "
        f"{_MAX_CAS_ATTEMPTS} CAS attempts all hit version_conflict"
    )


async def _read_canonical_doc(client: LithosClient, doc_path: str, slug: str) -> Note:
    note = await client.note_read(path=doc_path)
    if note is None:
        raise GithubMetadataError(
            f"no canonical project-context doc at {doc_path!r} "
            f"(create one first with `lithos-loom project create --slug {slug}`)"
        )
    return note
