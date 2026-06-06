"""Helpers for the github-watcher CLI subcommands.

The watcher's per-project config is persisted in the canonical
project-context doc's free-form Lithos metadata (the document ``extra``
dict), under these keys:

- ``github_repos`` — list of ``owner/name`` strings. A project may map
  to several repos; a non-empty list is what makes the watcher consider
  this project.
- ``github_watch_enabled`` — ``True`` while watching is on, ``False``
  when paused. Toggling it off leaves the repo list intact.
- ``github_exclude_labels`` — list of GH-label names. At import time,
  issues carrying any of these are skipped before ``task_create``.
  Already-linked tasks are unaffected (PRD: "exclude is only at import
  time"). The filter applies to every repo the project maps.
- ``github_exclude_authors`` — list of GH author logins; same shape.

This module hosts the read-mutate-write CAS loop the CLI subcommands
share. ``project.py`` consumes it via thin Typer wrappers, mirroring the
``_project_import_bulk`` / ``_regenerate_done`` extraction pattern.

Metadata values are typed JSON (lists, bools, strings), so — unlike the
earlier tag encoding — label names containing spaces are stored
verbatim.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lithos_loom.config import LoomConfig
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import LithosClient, Note

__all__ = [
    "GITHUB_EXCLUDE_AUTHORS_KEY",
    "GITHUB_EXCLUDE_LABELS_KEY",
    "GITHUB_REPOS_KEY",
    "GITHUB_WATCH_KEY",
    "GithubMetadataError",
    "NoteMutationResult",
    "extract_exclude_authors",
    "extract_exclude_labels",
    "extract_github_repos",
    "is_github_watching",
    "mutate_project_context_metadata",
    "validate_github_repo",
]

GITHUB_REPOS_KEY = "github_repos"
GITHUB_WATCH_KEY = "github_watch_enabled"
GITHUB_EXCLUDE_LABELS_KEY = "github_exclude_labels"
GITHUB_EXCLUDE_AUTHORS_KEY = "github_exclude_authors"

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
    """Raised when the CLI cannot complete a project-context mutation.

    Wraps the user-actionable cases: doc not found for the slug,
    invalid ``owner/name`` form, exhausted CAS retries. The CLI
    surfaces ``.args[0]`` directly to stderr.
    """


@dataclass(frozen=True)
class NoteMutationResult:
    """Outcome of a metadata-mutation round-trip.

    ``new_metadata`` carries the post-write metadata so the CLI can print
    a confirmation that reflects the actual state in Lithos, not the
    diff the operator requested.
    """

    note_id: str
    path: str
    new_metadata: dict[str, Any]
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


# ── Metadata inspection ───────────────────────────────────────────────


def _str_list(value: Any) -> list[str]:
    """Coerce a stored metadata value into an order-preserving, de-duped
    list of non-empty strings. Tolerates a bare string (treated as a
    one-element list) and non-list junk (treated as empty). Non-string
    list elements are ignored rather than coerced — a stray ``None`` or
    ``int`` from a hand-edited doc must not become a phantom repo/label."""
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, list):
        items = value
    else:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item or item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result


def extract_github_repos(metadata: dict[str, Any]) -> list[str]:
    """Return the ``owner/name`` repos this project maps.

    Empty list when the project has no repo mapping. Order-preserving
    and de-duped so the watcher logs a stable repo order.
    """
    return _str_list(metadata.get(GITHUB_REPOS_KEY))


def is_github_watching(metadata: dict[str, Any]) -> bool:
    # Strict identity, not truthiness: a hand-edited doc storing the
    # string "false" (or any non-bool) must NOT read as enabled. The
    # watcher's discovery filter (metadata_match) is likewise
    # type-sensitive, so only a real ``True`` enrols a project.
    return metadata.get(GITHUB_WATCH_KEY) is True


def extract_exclude_labels(metadata: dict[str, Any]) -> list[str]:
    """Return the GH-label names this project excludes at import time."""
    return _str_list(metadata.get(GITHUB_EXCLUDE_LABELS_KEY))


def extract_exclude_authors(metadata: dict[str, Any]) -> list[str]:
    """Return the GH author logins this project excludes at import time."""
    return _str_list(metadata.get(GITHUB_EXCLUDE_AUTHORS_KEY))


# ── Mutation ──────────────────────────────────────────────────────────


def _canonical_project_context_path(slug: str) -> str:
    return f"projects/{slug}/{slug}-project-context.md"


async def mutate_project_context_metadata(
    *,
    cfg: LoomConfig,
    slug: str,
    mutator: Callable[[dict[str, Any]], dict[str, Any]],
    action_summary: str,
) -> NoteMutationResult:
    """Apply ``mutator`` to a project-context doc's metadata and CAS-write.

    Wraps the full read-mutate-write loop:

    1. Open a single ``LithosClient`` context.
    2. ``note_read`` the canonical ``projects/<slug>/<slug>-project-
       context.md``.
    3. Apply ``mutator`` (which receives a copy of the current metadata
       dict and returns the new one).
    4. ``note_write`` with ``metadata=`` and ``expected_version`` for CAS
       protection.
    5. On ``version_conflict``: re-read and re-apply the mutator. Up to
       :data:`_MAX_CAS_ATTEMPTS` total attempts before giving up.

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
    mutator: Callable[[dict[str, Any]], dict[str, Any]],
    action_summary: str,
) -> NoteMutationResult:
    for _attempt in range(_MAX_CAS_ATTEMPTS):
        note = await _read_canonical_doc(client, doc_path, slug)
        current_meta = dict(note.metadata)
        new_meta = mutator(dict(current_meta))
        if new_meta == current_meta:
            # No-op: nothing to write. Skip the round-trip; the operator
            # gets idempotent semantics ("already set" is success).
            return NoteMutationResult(
                note_id=note.id,
                path=note.path,
                new_metadata=new_meta,
                changed=False,
            )
        # Pass the full desired metadata. Lithos applies a per-key merge,
        # and none of the CLI mutators delete a key (they replace list /
        # bool values), so a full-dict merge lands exactly the new state.
        # Tags are left untouched (omitted → preserved).
        write_result = await client.note_write(
            id=note.id,
            title=note.title,
            content=note.body,
            metadata=new_meta,
            expected_version=note.version,
            note_type=note.note_type or "concept",
        )
        if write_result.status in ("created", "updated"):
            return NoteMutationResult(
                note_id=note.id,
                path=note.path,
                new_metadata=new_meta,
                changed=True,
            )
        if write_result.status == "version_conflict":
            # Another writer landed between our read and write; loop
            # and re-apply the mutator to whatever metadata now exists.
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
