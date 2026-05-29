"""``lithos-loom project`` sub-app.

Lithos is the canonical project registry (slug, status, tags, context
body) and the TOML ``[projects.<slug>]`` table is a host-local automation
overlay (working-tree path, tool-config overrides). The intersection is
the slug.

The default ``project list`` shape enumerates Lithos via
``lithos_list(path_prefix="projects/", tags=["project-context"])`` and
marks each row with whether the local TOML has an automation entry for
that slug::

    slug              status    local
    lithos-loom       active    ✓ (/home/dns/projects/lithos/code/lithos-loom)
    influx            active    ✓ (/home/dns/projects/lithos/code/influx)
    edgelands         active    ✗ (no TOML entry on this host)
    old-experiment    archived  ✓ (/home/dns/projects/old-experiment)

``--source toml`` falls back to the local TOML ``[projects]`` table
for hosts without a Lithos connection or when the operator wants to
inspect their host-local overlay in isolation. The capture macro's
``--format json`` invocation gets a stable contract on both sources:
a JSON array of slug strings, in alphabetical order.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import typer

from lithos_loom.cli._project_import_bulk import (
    ImportPlan,
    PartialImportError,
    TasksOnlyPreflightError,
    check_tasks_only_preflight,
    create_tasks,
    force_tasks_cleanup,
    render_dry_run_plan,
    render_validation_report,
    resolve_default_slug_from_stem,
    validate_import_flags,
)
from lithos_loom.cli._regenerate_done import (
    build_done_content,
    collect_resolved_lines,
    render_dry_run,
)
from lithos_loom.config import LoomConfig, load_config
from lithos_loom.errors import LithosClientError, LithosLoomError
from lithos_loom.lithos_client import LithosClient, NoteSummary, WriteResult
from lithos_loom.render_project_context import extract_frontmatter
from lithos_loom.subscriptions._atomic_write import write_file_atomic
from lithos_loom.task_graph import build_plan
from lithos_loom.task_line_parser import parse_doc

project_app = typer.Typer(
    name="project",
    help="Project-config-aware CLI helpers.",
    no_args_is_help=True,
)


# Output formats; explicit enum strings give Typer a stable
# completion list and prevent typos from silently falling through to
# the plain-text default.
_FORMAT_TEXT = "text"
_FORMAT_JSON = "json"

# Source modes. ``lithos`` (default) enumerates from the canonical KB
# registry. ``toml`` falls back to the local TOML ``[projects]`` table
# — for hosts without a Lithos connection or when the operator wants
# to inspect their host-local overlay in isolation.
_SOURCE_LITHOS = "lithos"
_SOURCE_TOML = "toml"

_PROJECTS_PATH_PREFIX = "projects/"
_PROJECT_CONTEXT_TAG = "project-context"
# The single file the project-create command writes per project.
# ``project list`` recognises BOTH ``<slug>/context.md`` and
# ``<slug>/<slug>-project-context.md`` — the latter is the prod
# convention. We default-create the latter so ``project create``
# lands the doc at the path ``project list`` will pick up as the
# project's canonical context entry.
_DEFAULT_DOC_FILENAME_TEMPLATE = "{slug}-project-context.md"
# Validation: a slug must start AND end with [a-z0-9], with hyphens
# allowed only in between. Matches Lithos's directory-name convention
# and avoids edge cases (leading/trailing hyphens that break path
# parsing, double-hyphens that look like a delete marker).
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


@dataclass(frozen=True)
class _ProjectRow:
    """One row of ``project list`` output.

    Carries the union of Lithos-side and TOML-side data so the
    formatters (text / json) can decide what to render without
    re-merging.
    """

    slug: str
    status: str | None  # Lithos status; None for TOML-only rows or unknown
    local: bool  # has a TOML entry on this host
    repo: str | None  # local working-tree path; None when no TOML entry


@project_app.command("list")
def project_list(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path (overrides LITHOS_LOOM_CONFIG).",
    ),
    output_format: str = typer.Option(
        _FORMAT_TEXT,
        "--format",
        "-f",
        help="Output format: 'text' (aligned columns) or 'json' "
        "(array of slugs — stable shape for the capture macro).",
    ),
    source: str = typer.Option(
        _SOURCE_LITHOS,
        "--source",
        "-s",
        help=(
            "Where to enumerate from: 'lithos' (default) queries Lithos's "
            "projects/ KB. 'toml' falls back to the local [projects] table "
            "— useful when Lithos is unreachable or you want to inspect "
            "host-local overlay only."
        ),
    ),
) -> None:
    """List projects with their Lithos-canonical status + TOML-local overlay.

    Default (``--source lithos``) queries
    ``lithos_list(path_prefix="projects/", tags=["project-context"])``
    and joins the result against the local TOML ``[projects]`` table
    to mark which slugs have automation configured on this host. Slugs
    present only in TOML (no Lithos doc) are NOT listed here — they're
    surfaced by ``lithos-loom doctor`` instead, which calls them out
    as misconfigured (a TOML entry referencing a slug Lithos doesn't
    know about).

    ``--source toml`` enumerates TOML slugs only — same shape as the
    pre-Slice-4 command, useful for offline hosts.
    """
    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: {exc}", err=True)
        sys.exit(1)

    if source == _SOURCE_TOML:
        rows = _rows_from_toml(cfg)
    elif source == _SOURCE_LITHOS:
        try:
            rows = asyncio.run(_rows_from_lithos(cfg))
        except OSError as exc:
            typer.echo(
                f"lithos-loom: could not reach Lithos at "
                f"{cfg.orchestrator.lithos_url} ({exc}); try --source toml "
                f"to fall back to the local [projects] table",
                err=True,
            )
            sys.exit(1)
        except LithosClientError as exc:
            typer.echo(f"lithos-loom: lithos_list failed: {exc}", err=True)
            sys.exit(1)
    else:
        typer.echo(
            f"lithos-loom: unknown --source {source!r} "
            f"(expected one of: {_SOURCE_LITHOS}, {_SOURCE_TOML})",
            err=True,
        )
        sys.exit(2)

    if output_format == _FORMAT_JSON:
        # Stable shape across both sources: a JSON array of slug
        # strings. The capture macro's existing
        # ``JSON.parse(... project list --format json)`` consumer
        # works unchanged.
        typer.echo(json.dumps([row.slug for row in rows]))
        return
    if output_format == _FORMAT_TEXT:
        _print_text_rows(rows)
        return
    typer.echo(
        f"lithos-loom: unknown --format {output_format!r} "
        f"(expected one of: {_FORMAT_TEXT}, {_FORMAT_JSON})",
        err=True,
    )
    sys.exit(2)


def _rows_from_toml(cfg: LoomConfig) -> list[_ProjectRow]:
    """Pre-Slice-4 enumeration path. Slugs from ``cfg.projects.keys()``,
    alphabetised, no Lithos round-trip. ``status`` is ``None`` because
    we don't know it without asking Lithos."""
    return [
        _ProjectRow(
            slug=slug,
            status=None,
            local=True,
            repo=str(cfg.projects[slug].repo),
        )
        for slug in sorted(cfg.projects)
    ]


async def _rows_from_lithos(cfg: LoomConfig) -> list[_ProjectRow]:
    """Enumerates Lithos via
    ``note_list(path_prefix="projects/", tags=["project-context"])``
    and joins against ``cfg.projects`` to mark local-overlay rows.

    The async wrapper exists because :class:`LithosClient` is an
    async context manager — Typer's command is sync, so we wrap with
    ``asyncio.run`` at the call site (same pattern as ``task create``).
    """
    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        summaries = await client.note_list(
            path_prefix=_PROJECTS_PATH_PREFIX,
            tags=[_PROJECT_CONTEXT_TAG],
        )
    return _merge_lithos_with_toml(summaries, cfg.projects)


def _merge_lithos_with_toml(
    summaries: list[NoteSummary],
    toml_projects: Mapping[str, object],
) -> list[_ProjectRow]:
    """Join Lithos's per-doc summaries with the host-local TOML map.

    Lithos-side slugs are derived from the doc path's first segment
    after ``projects/`` (see :func:`lithos_client._slug_from_path`).
    Empty slugs (path didn't match the expected shape) are dropped
    — there's nothing for the operator to act on.

    Multiple Lithos docs may share a slug (a project with both a
    ``<slug>-project-context.md`` and an ``architecture.md`` under
    the same slug directory). We collapse on the slug — one row per
    slug. The status column reflects the **canonical project context
    doc** (``projects/<slug>/<slug>-project-context.md``) when one
    exists for the slug. That's the doc the project-context registry
    actually means by "the project's status"; other docs
    (architecture, roadmap, etc.) live alongside but aren't the
    registry entry, so their status flips wouldn't reflect what the
    operator means by "is this project active".

    The ``<slug>-project-context.md`` naming convention matches what
    real prod project-context docs use today (e.g.
    ``projects/lithos-loom/lithos-loom-project-context.md``).
    Earlier the picker looked for literal ``context.md`` — a clean
    name in isolation, but it never matched prod docs, so the
    canonical preference silently became dead code in practice. See
    the soak-phase note in ``examples/slice-4-test/MANUAL_TEST.md``.

    When no ``<slug>-project-context.md`` is present for the slug
    (operator structured the project differently, or this is a
    test fixture), we fall back to the summary with the
    lexicographically-smallest path so the choice is deterministic
    regardless of Lithos's response order. Without this rule the
    displayed status was list-order dependent; could flip between
    ``active`` and ``archived`` on the same operator state if Lithos
    returned summaries in a different order.

    Per-doc visibility lives in a separate command (``project docs
    <slug>`` — future).
    """
    # Group all summaries by slug first; we need to inspect all the
    # candidates for a slug before picking the canonical one rather
    # than committing to the first-seen.
    by_slug: dict[str, list[NoteSummary]] = {}
    for summary in summaries:
        slug = summary.slug
        if not slug:
            continue
        by_slug.setdefault(slug, []).append(summary)

    rows: list[_ProjectRow] = []
    for slug in sorted(by_slug):
        canonical = _pick_canonical_summary(slug, by_slug[slug])
        is_local = slug in toml_projects
        repo = str(getattr(toml_projects[slug], "repo", "")) if is_local else None
        rows.append(
            _ProjectRow(
                slug=slug,
                status=canonical.status,
                local=is_local,
                repo=repo,
            )
        )
    return rows


def _pick_canonical_summary(slug: str, candidates: list[NoteSummary]) -> NoteSummary:
    """Pick the project-context doc whose status represents the slug.

    Preference order:

    1. ``projects/<slug>/<slug>-project-context.md`` — the prod
       convention for canonical project context registry entries
       (e.g. ``projects/lithos-loom/lithos-loom-project-context.md``).
       Other doctypes alongside it (``architecture.md``,
       ``roadmap.md``, ad-hoc notes) are supplementary; their status
       flips don't represent "is the project active".
    2. Lexicographically-smallest path among the remaining
       candidates. Deterministic regardless of Lithos's response
       order — without this fallback, two docs both labelled
       supplementary (no ``<slug>-project-context.md``) would expose
       the order-dependent bug the canonical-preference rule was
       added to fix.

    Pre: ``candidates`` is non-empty (caller filtered empty slugs).
    """
    canonical_path = f"{_PROJECTS_PATH_PREFIX}{slug}/{slug}-project-context.md"
    for candidate in candidates:
        if candidate.path == canonical_path:
            return candidate
    return min(candidates, key=lambda c: c.path)


@project_app.command("create")
def project_create(
    title: str = typer.Option(
        ...,
        "--title",
        "-t",
        help="Project title (used for the H1 + frontmatter title).",
    ),
    slug: str | None = typer.Option(
        None,
        "--slug",
        "-s",
        help=(
            "Project slug (directory name under projects/). "
            "Defaults to a slugified version of --title."
        ),
    ),
    tags: str | None = typer.Option(
        None,
        "--tags",
        help="Comma-separated extra tags (project-context is added automatically).",
    ),
    body: str | None = typer.Option(
        None,
        "--body",
        "-b",
        help="Inline body text. Mutually exclusive with --body-file.",
    ),
    body_file: Path | None = typer.Option(
        None,
        "--body-file",
        help=(
            "Read body from this file. Useful for multiline content "
            "without shell-escape pain (the create-project macro uses this)."
        ),
    ),
    output_format: str = typer.Option(
        _FORMAT_TEXT,
        "--format",
        "-f",
        help=(
            "Output format: 'text' (just the projected vault path on stdout) "
            "or 'json' ({id, slug, vault_path}) for scripted consumers."
        ),
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path (overrides LITHOS_LOOM_CONFIG).",
    ),
) -> None:
    """Create a new Lithos project-context doc.

    Writes ``projects/<slug>/<slug>-project-context.md`` in Lithos. The
    obsidian-sync child's project-context-projection then projects it
    into the vault at ``<vault>/<projects_dir>/<slug>/<slug>-project-
    context.md`` within ~250ms.

    Exit codes:
    * 0 — success.
    * 1 — Lithos call / config-load failure / slug collision.
    * 2 — input validation error (invalid slug, mutually exclusive
      flags, --body-file unreadable).
    """
    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: {exc}", err=True)
        sys.exit(1)

    if cfg.obsidian_sync is None:
        typer.echo(
            "lithos-loom: project create requires [obsidian_sync] in config "
            "(needed to compute the projected vault path for output)",
            err=True,
        )
        sys.exit(2)

    if body is not None and body_file is not None:
        typer.echo(
            "lithos-loom: --body and --body-file are mutually exclusive",
            err=True,
        )
        sys.exit(2)

    body_text = _read_body(body, body_file)
    if body_text is None:
        # _read_body already printed the error
        sys.exit(2)

    resolved_slug = slug if slug is not None else _slugify(title)
    if not _SLUG_RE.match(resolved_slug):
        typer.echo(
            f"lithos-loom: invalid slug {resolved_slug!r}; must match "
            f"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$ "
            f"(lowercase alphanumerics + hyphens, must start+end alphanumeric)",
            err=True,
        )
        sys.exit(2)

    tag_list = _project_tags(tags)
    filename = _DEFAULT_DOC_FILENAME_TEMPLATE.format(slug=resolved_slug)

    try:
        result = asyncio.run(
            _create_project_async(
                cfg=cfg,
                slug=resolved_slug,
                title=title,
                content=body_text,
                tags=tag_list,
                filename=filename,
            )
        )
    except _SlugCollisionError as exc:
        typer.echo(
            f"lithos-loom: slug {resolved_slug!r} already exists at "
            f"doc id {exc.existing_id} ({exc.existing_path})",
            err=True,
        )
        sys.exit(1)
    except OSError as exc:
        typer.echo(
            f"lithos-loom: could not reach Lithos at "
            f"{cfg.orchestrator.lithos_url} ({exc})",
            err=True,
        )
        sys.exit(1)
    except LithosClientError as exc:
        typer.echo(f"lithos-loom: note_write failed: {exc}", err=True)
        sys.exit(1)

    obs = cfg.obsidian_sync
    vault_path = obs.vault_path / obs.projects_dir / resolved_slug / filename

    if output_format == _FORMAT_JSON:
        typer.echo(
            json.dumps(
                {
                    "id": result.id,
                    "slug": resolved_slug,
                    "vault_path": str(vault_path),
                }
            )
        )
        return
    if output_format == _FORMAT_TEXT:
        # Single-line stdout — macro reads it directly.
        typer.echo(str(vault_path))
        return
    typer.echo(
        f"lithos-loom: unknown --format {output_format!r} "
        f"(expected one of: {_FORMAT_TEXT}, {_FORMAT_JSON})",
        err=True,
    )
    sys.exit(2)


@dataclass(frozen=True)
class _CreateProjectResult:
    """Return value of :func:`_create_project_async`."""

    id: str
    slug: str


class _SlugCollisionError(Exception):
    """Raised by :func:`_create_project_async` when the pre-flight
    ``note_list`` finds a doc already at ``projects/<slug>/``."""

    def __init__(self, existing_id: str, existing_path: str) -> None:
        super().__init__(f"slug already exists at {existing_id} ({existing_path})")
        self.existing_id = existing_id
        self.existing_path = existing_path


async def _create_project_async(
    *,
    cfg: LoomConfig,
    slug: str,
    title: str,
    content: str,
    tags: list[str],
    filename: str,
) -> _CreateProjectResult:
    """One-shot Lithos write for a new project-context doc.

    Pre-flight check: ``note_list(path_prefix=f"projects/{slug}/")`` —
    if anything exists we raise :class:`_SlugCollisionError` with the
    existing doc's id and path so the caller can surface a clear
    message instead of catching ``slug_collision`` from
    :meth:`LithosClient.note_write` (which would also work but the
    pre-flight gives a cheaper, single-source-of-truth check that's
    easier to test).

    Shared with :func:`project_import` so both entry points share the
    same validation + write semantics.

    **All typed exceptions are raised AFTER the ``async with
    LithosClient`` block exits**, not inside it. ``LithosClient.__aexit__``
    runs the SSE-transport cleanup inside an ``anyio.create_task_group``;
    any exception raised inside the block triggers that task group to
    wrap the original exception in a :class:`BaseExceptionGroup` on
    cancellation, and the caller's typed ``except`` clauses
    (``_SlugCollisionError``, ``OSError``, ``LithosClientError``) no
    longer match — so the CLI would dump a raw Rich traceback instead
    of the intended error message.

    Three cases are deferred:

    * ``_SlugCollisionError`` — domain exception we raise ourselves
      when the pre-flight ``note_list`` finds an existing doc.
    * ``OSError`` — transport failure (connection refused, DNS, etc.)
      raised by ``note_list`` / ``note_write`` underneath.
    * ``LithosClientError`` — Lithos-returned error envelope that the
      typed-client converts to an exception (e.g. ``content_too_large``,
      RPC errors, malformed responses).

    All three are caught inside the block, stored as locals, and
    re-raised once the context closes cleanly.
    """
    doc_path = f"{_PROJECTS_PATH_PREFIX}{slug}/{filename}"
    collision: _SlugCollisionError | None = None
    deferred_error: OSError | LithosClientError | None = None
    result: WriteResult | None = None
    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        try:
            existing = await client.note_list(
                path_prefix=f"{_PROJECTS_PATH_PREFIX}{slug}/", limit=1
            )
            if existing:
                collision = _SlugCollisionError(
                    existing_id=existing[0].id,
                    existing_path=existing[0].path,
                )
            else:
                result = await client.note_write(
                    path=doc_path,
                    title=title,
                    content=content,
                    tags=tags,
                    note_type="concept",
                )
        except (OSError, LithosClientError) as exc:
            deferred_error = exc
    if deferred_error is not None:
        raise deferred_error
    if collision is not None:
        raise collision
    assert result is not None  # narrowed by the else / no-error branch above
    if result.status not in ("created", "updated"):
        raise LithosClientError(
            code=result.status,
            message=result.message or f"note_write returned status={result.status!r}",
        )
    # ``note_write`` now stitches the top-level response's id/path/
    # version with the request's title/tags/etc into ``result.note``
    # (see the note_write fix-up block). ``result.note`` is guaranteed
    # populated on created/updated outcomes; ``id`` is the canonical
    # Lithos doc id.
    if result.note is None:
        raise LithosClientError(
            code="invalid_response",
            message=(
                f"note_write status={result.status!r} for {doc_path!r} but "
                f"response carried neither a 'document' field nor a top-level "
                f"id — Lithos may have changed its response shape"
            ),
        )
    return _CreateProjectResult(id=result.note.id, slug=slug)


async def _check_slug_collision_async(*, cfg: LoomConfig, slug: str) -> None:
    """Read-only slug-collision pre-flight for ``--dry-run`` greenfield previews.

    Raises :class:`_SlugCollisionError` when a doc already exists at
    ``projects/<slug>/``; returns normally otherwise. Mirrors the
    pre-flight half of :func:`_create_project_async` (same exception
    deferral pattern to avoid anyio task-group wrapping
    :class:`_SlugCollisionError` / :class:`OSError` /
    :class:`LithosClientError` in a :class:`BaseExceptionGroup`).

    ``--dry-run`` runs the same collision check the real run would,
    so the operator catches "slug already exists" before committing
    to the real run.
    """
    collision: _SlugCollisionError | None = None
    deferred_error: OSError | LithosClientError | None = None
    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        try:
            existing = await client.note_list(
                path_prefix=f"{_PROJECTS_PATH_PREFIX}{slug}/", limit=1
            )
            if existing:
                collision = _SlugCollisionError(
                    existing_id=existing[0].id,
                    existing_path=existing[0].path,
                )
        except (OSError, LithosClientError) as exc:
            deferred_error = exc
    if deferred_error is not None:
        raise deferred_error
    if collision is not None:
        raise collision


def _slugify(value: str) -> str:
    """Pure slugify: lowercase + ASCII-fold + non-alphanumeric → hyphen.

    Drops anything that isn't ASCII alphanumeric after NFKD-folding
    (so ``café`` → ``cafe``, ``Łódź`` → ``odz``), collapses runs of
    non-alphanumeric into single hyphens, strips leading/trailing
    hyphens. Empty / all-unrepresentable input returns ``""`` — the
    caller is responsible for validating against :data:`_SLUG_RE`
    which rejects empty strings.

    Why ``unicodedata`` rather than a slugify library: standard-library
    only (we already depend on ``re``), no extra package, and the
    behaviour is fully tested below. ``slugify`` PyPI packages have
    locale-dependent transliteration tables that drift between
    versions; we want byte-stable slugs.
    """
    folded = unicodedata.normalize("NFKD", value)
    ascii_only = folded.encode("ascii", errors="ignore").decode("ascii")
    lower = ascii_only.lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lower)
    return hyphenated.strip("-")


def _project_tags(raw: str | None) -> list[str]:
    """Build the tag list for a new project doc.

    Always includes ``project-context`` (the filter tag the projection
    uses; without it the doc would never be projected to the vault).
    Operator-supplied tags are deduplicated against it so an explicit
    ``--tags project-context,foo`` doesn't produce two copies.

    Returns ``["project-context"]`` for empty / None input.
    """
    extra = [part.strip() for part in (raw or "").split(",") if part.strip()]
    out = list(extra)
    if _PROJECT_CONTEXT_TAG not in out:
        out.append(_PROJECT_CONTEXT_TAG)
    return out


def _read_body(body: str | None, body_file: Path | None) -> str | None:
    """Pick the body content from --body / --body-file / neither.

    Returns the body string, or ``None`` if the operator passed a
    ``--body-file`` that couldn't be read (caller exits 2). Neither
    flag → empty string (operator fills in via Obsidian after the
    projection writes the file).
    """
    if body is not None:
        return body
    if body_file is not None:
        try:
            return body_file.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(
                f"lithos-loom: could not read --body-file {body_file}: {exc}",
                err=True,
            )
            return None
    return ""


@project_app.command("import")
def project_import(
    source: Path = typer.Argument(
        ...,
        help="Path to a local Markdown file to import as a project doc + tasks.",
    ),
    slug: str | None = typer.Option(
        None,
        "--slug",
        "-s",
        help=(
            "Project slug. In greenfield mode (default), optional — defaults "
            "to the slugified frontmatter title (or file stem with leading "
            "'project-' stripped). In --tasks-only mode, REQUIRED."
        ),
    ),
    tags: str | None = typer.Option(
        None,
        "--tags",
        help=(
            "Extra comma-separated tags for the project doc (greenfield "
            "mode only — ignored with --tasks-only). Union'd with "
            "frontmatter tags + project-context (no duplicates)."
        ),
    ),
    tasks_only: bool = typer.Option(
        False,
        "--tasks-only",
        help=(
            "Skip project doc creation; just import tasks against an "
            "existing project. Requires --slug. Project must already exist "
            "in Lithos."
        ),
    ),
    no_tasks: bool = typer.Option(
        False,
        "--no-tasks",
        help=(
            "Skip task extraction entirely; import only the project doc body. "
            "Mutually exclusive with --tasks-only."
        ),
    ),
    force_tasks: bool = typer.Option(
        False,
        "--force-tasks",
        help=(
            "Delete all existing open tasks for this project before importing. "
            "Gated by an interactive y/N prompt unless --yes is also passed. "
            "Cancelled tasks remain in the Lithos entity store (no hard-delete)."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Suppress the --force-tasks interactive confirmation. For scripted use.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Print the full plan (doc + tasks + dependency edges) and exit "
            "without writing to Lithos. Only Lithos call made is the "
            "slug-collision pre-flight (read-only)."
        ),
    ),
    output_format: str = typer.Option(
        _FORMAT_TEXT,
        "--format",
        "-f",
        help=(
            "Output format: 'text' (vault path on stdout) or 'json' "
            "({id, slug, vault_path, tasks_created})."
        ),
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path (overrides LITHOS_LOOM_CONFIG).",
    ),
) -> None:
    """Import a local Markdown file as a Lithos project — including its open tasks.

    By default extracts ``- [ ]`` lines from the source body as Lithos
    task entities (with dependency edges from indentation), strips them
    from the persisted doc body, and creates the project doc + tasks
    (validate-all-then-abort on validation failures). Pass ``--no-tasks``
    to suppress task extraction.

    Two modes:

    * **Greenfield** (default): creates the project doc AND the tasks.
      Refuses if the slug already exists.
    * **--tasks-only**: just creates tasks against an existing
      project. Requires --slug. Refuses if the project doesn't exist
      (suggests typo matches) or has existing tasks (unless
      --force-tasks is passed).

    Use ``--dry-run`` to preview the plan with no Lithos writes.

    Exit codes mirror ``project create`` (0 / 1 / 2). See
    ``docs/prd/bulk-task-import.md`` for the full decision table.
    """
    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: {exc}", err=True)
        sys.exit(1)

    if cfg.obsidian_sync is None:
        typer.echo(
            "lithos-loom: project import requires [obsidian_sync] in config",
            err=True,
        )
        sys.exit(2)

    # E7: mutually-exclusive flag validation (raises typer.Exit on conflict)
    validate_import_flags(
        tasks_only=tasks_only,
        no_tasks=no_tasks,
        force_tasks=force_tasks,
        slug=slug,
    )

    # Read source
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"lithos-loom: could not read {source}: {exc}", err=True)
        sys.exit(2)

    frontmatter, body = extract_frontmatter(raw)
    lithos_id_in_frontmatter = (
        frontmatter.get("lithos_id") if isinstance(frontmatter, dict) else None
    )
    if not isinstance(lithos_id_in_frontmatter, str):
        lithos_id_in_frontmatter = None

    # Greenfield: refuse already-projected files (existing behaviour)
    if not tasks_only and lithos_id_in_frontmatter is not None:
        typer.echo(
            f"lithos-loom: {source} already carries lithos_id "
            f"{lithos_id_in_frontmatter!r} in frontmatter — refusing to "
            "re-import in greenfield mode (would create a duplicate doc). "
            "Use --tasks-only --slug <slug> to add tasks against the "
            "existing project, or edit the original doc in Lithos.",
            err=True,
        )
        sys.exit(2)

    # Title + slug derivation
    fm_title = frontmatter.get("title") if isinstance(frontmatter, dict) else None
    if isinstance(fm_title, str) and fm_title:
        title = str(fm_title)
    else:
        title = _title_from_stem(source.stem)

    if slug is not None:
        resolved_slug = slug
    elif isinstance(fm_title, str) and fm_title:
        # Frontmatter title is explicit operator intent, NOT prefix-stripped.
        resolved_slug = _slugify(title)
    else:
        # Default-slug from stem, prefix-stripped.
        resolved_slug = _slugify(resolve_default_slug_from_stem(source))

    if not _SLUG_RE.match(resolved_slug):
        typer.echo(
            f"lithos-loom: invalid slug {resolved_slug!r}; must match "
            f"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$ "
            f"(lowercase alphanumerics + hyphens, must start+end alphanumeric)",
            err=True,
        )
        sys.exit(2)

    fm_tags = frontmatter.get("tags") if isinstance(frontmatter, dict) else None
    fm_tag_list = [str(t) for t in fm_tags] if isinstance(fm_tags, list) else []
    tag_list = _merge_tags(fm_tag_list, tags)

    # Parse + plan tasks (unless --no-tasks)
    plans: list = []
    parsed_lines: list = []
    stripped_body = body
    if not no_tasks:
        parsed_lines, parse_errors, stripped_body = parse_doc(body, resolved_slug)
        graph_plans, graph_errors = build_plan(parsed_lines)
        all_errors = parse_errors + graph_errors
        if all_errors:
            typer.echo(render_validation_report(all_errors), err=True)
            sys.exit(2)
        plans = graph_plans

    # --dry-run: run the same read-only pre-flight the real run would,
    # then print the plan. Tasks-only verifies project exists +
    # lithos_id consistency; greenfield verifies slug doesn't collide.
    # Both paths exit non-zero on pre-flight failure so the operator
    # catches the problem before committing.
    if dry_run:
        project_existed = False
        if tasks_only:
            try:
                project_id, _ = asyncio.run(
                    check_tasks_only_preflight(
                        cfg=cfg,
                        slug=resolved_slug,
                        lithos_id_in_frontmatter=lithos_id_in_frontmatter,
                    )
                )
                project_existed = project_id is not None
            except TasksOnlyPreflightError as exc:
                typer.echo(exc.message, err=True)
                sys.exit(exc.exit_code)
            except OSError as exc:
                typer.echo(
                    f"lithos-loom: could not reach Lithos at "
                    f"{cfg.orchestrator.lithos_url} ({exc})",
                    err=True,
                )
                sys.exit(1)
            except LithosClientError as exc:
                typer.echo(f"lithos-loom: lithos call failed: {exc}", err=True)
                sys.exit(1)
        else:
            try:
                asyncio.run(_check_slug_collision_async(cfg=cfg, slug=resolved_slug))
            except _SlugCollisionError as exc:
                typer.echo(
                    f"lithos-loom: slug {resolved_slug!r} already exists at "
                    f"doc id {exc.existing_id} ({exc.existing_path}); did you "
                    f"mean --tasks-only --slug {resolved_slug}?",
                    err=True,
                )
                sys.exit(1)
            except OSError as exc:
                typer.echo(
                    f"lithos-loom: could not reach Lithos at "
                    f"{cfg.orchestrator.lithos_url} ({exc})",
                    err=True,
                )
                sys.exit(1)
            except LithosClientError as exc:
                typer.echo(f"lithos-loom: note_list failed: {exc}", err=True)
                sys.exit(1)

        plan = ImportPlan(
            source=source,
            slug=resolved_slug,
            title=title,
            body_after_strip=stripped_body,
            tags=tag_list,
            plans=plans,
            parsed_lines=parsed_lines,
            is_tasks_only=tasks_only,
            is_force_tasks=force_tasks,
            yes=yes,
            lithos_id_in_frontmatter=lithos_id_in_frontmatter,
        )
        typer.echo(render_dry_run_plan(plan, project_existed=project_existed))
        return

    # ── Real execution ──
    filename = _DEFAULT_DOC_FILENAME_TEMPLATE.format(slug=resolved_slug)
    obs = cfg.obsidian_sync
    vault_path = obs.vault_path / obs.projects_dir / resolved_slug / filename
    project_id: str

    if tasks_only:
        # Tasks-only mode: verify project exists; deal with existing tasks.
        try:
            project_id, existing_tasks = asyncio.run(
                check_tasks_only_preflight(
                    cfg=cfg,
                    slug=resolved_slug,
                    lithos_id_in_frontmatter=lithos_id_in_frontmatter,
                )
            )
        except TasksOnlyPreflightError as exc:
            typer.echo(exc.message, err=True)
            sys.exit(exc.exit_code)
        except OSError as exc:
            typer.echo(
                f"lithos-loom: could not reach Lithos at "
                f"{cfg.orchestrator.lithos_url} ({exc})",
                err=True,
            )
            sys.exit(1)
        except LithosClientError as exc:
            typer.echo(f"lithos-loom: lithos call failed: {exc}", err=True)
            sys.exit(1)

        if existing_tasks:
            open_count = sum(1 for t in existing_tasks if t.status == "open")
            resolved_count = len(existing_tasks) - open_count
            if not force_tasks:
                breakdown = f"{open_count} open" + (
                    f" + {resolved_count} resolved (history)" if resolved_count else ""
                )
                typer.echo(
                    f"lithos-loom: project {resolved_slug!r} already has "
                    f"{len(existing_tasks)} existing task"
                    f"{'s' if len(existing_tasks) != 1 else ''} on record "
                    f"({breakdown}); refusing to add more (would duplicate). "
                    f"Re-run with --force-tasks to cancel open tasks and "
                    f"re-import (resolved history is preserved).",
                    err=True,
                )
                sys.exit(1)

            history_note = (
                f" ({resolved_count} resolved task"
                f"{'s' if resolved_count != 1 else ''} will remain as history)"
                if resolved_count
                else ""
            )
            if not yes and not typer.confirm(
                f"Cancel {open_count} open task"
                f"{'s' if open_count != 1 else ''}{history_note} and create "
                f"{len(plans)} new ones?",
                default=False,
            ):
                typer.echo("aborted; no changes made", err=True)
                sys.exit(0)
            try:
                asyncio.run(force_tasks_cleanup(cfg=cfg, existing_tasks=existing_tasks))
            except OSError as exc:
                typer.echo(
                    f"lithos-loom: could not reach Lithos at "
                    f"{cfg.orchestrator.lithos_url} ({exc})",
                    err=True,
                )
                sys.exit(1)
            except LithosClientError as exc:
                typer.echo(f"lithos-loom: task_cancel failed: {exc}", err=True)
                sys.exit(1)
    else:
        # Greenfield: create the project doc
        try:
            project_result = asyncio.run(
                _create_project_async(
                    cfg=cfg,
                    slug=resolved_slug,
                    title=title,
                    content=stripped_body,
                    tags=tag_list,
                    filename=filename,
                )
            )
        except _SlugCollisionError as exc:
            typer.echo(
                f"lithos-loom: slug {resolved_slug!r} already exists at "
                f"doc id {exc.existing_id} ({exc.existing_path}); did you "
                f"mean --tasks-only --slug {resolved_slug}?",
                err=True,
            )
            sys.exit(1)
        except OSError as exc:
            typer.echo(
                f"lithos-loom: could not reach Lithos at "
                f"{cfg.orchestrator.lithos_url} ({exc})",
                err=True,
            )
            sys.exit(1)
        except LithosClientError as exc:
            typer.echo(f"lithos-loom: note_write failed: {exc}", err=True)
            sys.exit(1)
        project_id = project_result.id

    # Create the tasks (if any)
    n_tasks_created = 0
    if plans:
        try:
            n_tasks_created = asyncio.run(
                create_tasks(cfg=cfg, slug=resolved_slug, plans=plans, source=source)
            )
        except PartialImportError as exc:
            typer.echo(
                f"lithos-loom: partial import — created {exc.n_created}/"
                f"{exc.n_total} tasks before failure ({exc.underlying}). "
                f"A [Friction] finding has been posted with the recovery "
                f"command; re-run with --tasks-only --slug {resolved_slug} "
                f"--force-tasks to complete.",
                err=True,
            )
            sys.exit(1)
        except OSError as exc:
            typer.echo(
                f"lithos-loom: could not reach Lithos at "
                f"{cfg.orchestrator.lithos_url} ({exc})",
                err=True,
            )
            sys.exit(1)
        except LithosClientError as exc:
            typer.echo(f"lithos-loom: task_create failed: {exc}", err=True)
            sys.exit(1)

    # Output
    if output_format == _FORMAT_JSON:
        typer.echo(
            json.dumps(
                {
                    "id": project_id,
                    "slug": resolved_slug,
                    "vault_path": str(vault_path),
                    "tasks_created": n_tasks_created,
                }
            )
        )
        return
    if output_format == _FORMAT_TEXT:
        typer.echo(str(vault_path))
        return
    typer.echo(
        f"lithos-loom: unknown --format {output_format!r} "
        f"(expected one of: {_FORMAT_TEXT}, {_FORMAT_JSON})",
        err=True,
    )
    sys.exit(2)


def _title_from_stem(stem: str) -> str:
    """Convert a file stem into a human-readable title.

    ``"my-project"`` → ``"My Project"``; ``"foo_bar"`` → ``"Foo Bar"``.
    Used when frontmatter has no explicit title.
    """
    cleaned = re.sub(r"[-_]+", " ", stem).strip()
    return cleaned.title()


def _merge_tags(frontmatter_tags: list[str], extra_csv: str | None) -> list[str]:
    """Union frontmatter tags + --tags + project-context, preserving
    order, no duplicates.

    Order matters because the operator-visible tag order in Lithos
    reflects this list as-is; preserving frontmatter order first
    (operator already curated it) then appending CLI-extra tags then
    the required ``project-context`` is the least-surprising sequence.
    """
    extra = [part.strip() for part in (extra_csv or "").split(",") if part.strip()]
    out: list[str] = []
    for tag in [*frontmatter_tags, *extra, _PROJECT_CONTEXT_TAG]:
        if tag and tag not in out:
            out.append(tag)
    return out


def _print_text_rows(rows: list[_ProjectRow]) -> None:
    """Render rows as an aligned three-column table to stdout.

    Empty result prints nothing (no header) so scripted callers
    piping into ``wc -l`` get a meaningful zero. The header is only
    rendered when there's at least one row to keep the output
    self-describing when the operator runs the command interactively.
    """
    if not rows:
        return
    slug_width = max(len("slug"), max(len(r.slug) for r in rows))
    status_width = max(len("status"), max(len(r.status or "—") for r in rows))
    typer.echo(f"{'slug':<{slug_width}}  {'status':<{status_width}}  local")
    for row in rows:
        status = row.status or "—"
        local_mark = (
            f"✓ ({row.repo})" if row.local and row.repo else "✓" if row.local else "✗"
        )
        typer.echo(f"{row.slug:<{slug_width}}  {status:<{status_width}}  {local_mark}")


@project_app.command("regenerate-done")
def project_regenerate_done(
    slug: str = typer.Option(
        ...,
        "--slug",
        "-s",
        help="Project slug whose <slug>-done.md archive to rebuild.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview the rebuilt file (line count + lines); write nothing.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the overwrite confirmation prompt.",
    ),
    output_format: str = typer.Option(
        _FORMAT_TEXT,
        "--format",
        "-f",
        help=f"Output format ({_FORMAT_TEXT} | {_FORMAT_JSON}).",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path.",
    ),
) -> None:
    """Rebuild a project's task-archive done file from Lithos.

    Queries every resolved (completed + cancelled) Lithos task carrying
    ``metadata.project == <slug>`` and overwrites
    ``<vault>/_lithos/projects/<slug>/<slug>-done.md`` with one
    Tasks-plugin line per task, sorted oldest-first by resolution date.

    Unlike the live ``task-archive`` subscription, this writes ALL
    resolved tasks for the slug — the "was this surfaced to the operator"
    signal is ephemeral and can't be reconstructed, so a rebuild is a
    complete-history snapshot rather than the surfaced-only set. Use it
    to backfill history that predates the archiver, or to rebuild a
    deleted/damaged file. It OVERWRITES the existing file (discarding any
    manual edits) — that's the point of a regenerate.
    """
    if output_format not in (_FORMAT_TEXT, _FORMAT_JSON):
        typer.echo(
            f"lithos-loom: unknown --format {output_format!r} "
            f"(expected one of: {_FORMAT_TEXT}, {_FORMAT_JSON})",
            err=True,
        )
        sys.exit(2)

    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: {exc}", err=True)
        sys.exit(1)

    obs = cfg.obsidian_sync
    if obs is None:
        typer.echo(
            "lithos-loom: regenerate-done needs an [obsidian_sync] section "
            "(it writes a per-project file under the vault); none configured",
            err=True,
        )
        sys.exit(1)

    if not _SLUG_RE.match(slug):
        typer.echo(
            f"lithos-loom: invalid slug {slug!r}; must match "
            f"{_SLUG_RE.pattern} (lowercase alphanumerics + hyphens, must "
            f"start+end alphanumeric)",
            err=True,
        )
        sys.exit(2)

    # Done-file path convention mirrors the task-archive subscription's
    # ``_done_file`` (``<projects_root>/<slug>/<slug>-done.md``); inlined
    # here so the CLI doesn't reach into a daemon-layer private. The slug
    # is already ``_SLUG_RE``-validated above, so the path is safe.
    done_path = obs.vault_path / obs.projects_dir / slug / f"{slug}-done.md"

    is_json = output_format == _FORMAT_JSON

    def _emit_json(action: str, *, count: int, written: bool) -> None:
        # Single-line JSON envelope shared by every 0-exit path so
        # scripted callers get a parseable result on the no-write paths
        # (dry-run / no-op / aborted) too, not just on a real write.
        typer.echo(
            json.dumps(
                {
                    "slug": slug,
                    "path": str(done_path),
                    "action": action,
                    "count": count,
                    "written": written,
                }
            )
        )

    try:
        lines = asyncio.run(collect_resolved_lines(cfg=cfg, slug=slug))
    except OSError as exc:
        typer.echo(
            f"lithos-loom: could not reach Lithos at "
            f"{cfg.orchestrator.lithos_url} ({exc})",
            err=True,
        )
        sys.exit(1)
    except LithosClientError as exc:
        typer.echo(f"lithos-loom: regenerate-done failed: {exc}", err=True)
        sys.exit(1)

    if dry_run:
        if is_json:
            _emit_json("dry-run", count=len(lines), written=False)
        else:
            typer.echo(render_dry_run(slug, done_path, lines))
        return

    file_exists = done_path.exists()
    if not lines and not file_exists:
        if is_json:
            _emit_json("noop", count=0, written=False)
        else:
            typer.echo(
                f"lithos-loom: no resolved tasks for {slug!r}; nothing to write",
                err=True,
            )
        return

    if file_exists and not yes:
        try:
            existing = done_path.read_text(encoding="utf-8")
            n = sum(1 for ln in existing.splitlines() if ln)
        except OSError:
            n = 0  # count is cosmetic; still prompt before clobbering
        action = "clear" if not lines else f"overwrite ({len(lines)} line(s))"
        if not typer.confirm(
            f"{action} {done_path.name} (currently {n} line(s))?",
            default=False,
        ):
            if is_json:
                _emit_json("aborted", count=len(lines), written=False)
            else:
                typer.echo("aborted; no changes made", err=True)
            return

    try:
        asyncio.run(write_file_atomic(done_path, build_done_content(lines)))
    except OSError as exc:
        typer.echo(f"lithos-loom: could not write {done_path} ({exc})", err=True)
        sys.exit(1)

    if is_json:
        _emit_json("written", count=len(lines), written=True)
        return
    typer.echo(str(done_path))
