"""Bulk-task-import helpers for ``lithos-loom project import``.

The Typer command (in ``cli/project.py``) is a thin dispatcher; the
business logic — flag validation, slug derivation, parser/graph
orchestration, dry-run rendering, async Lithos calls, partial-failure
recovery — lives here so ``project.py`` stays under the 800-line
file cap.

See ``docs/prd/archive/bulk-task-import.md`` for the original decision
table and rationale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import typer

from lithos_loom.config import LoomConfig
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import LithosClient, Task
from lithos_loom.task_graph import TaskCreatePlan
from lithos_loom.task_line_parser import ParsedTaskLine, ValidationError

_LOG = logging.getLogger(__name__)

# Prefix stripped from file stems when deriving a default slug. The
# strip is case-insensitive at the prefix boundary but the stem
# survives otherwise (e.g. ``project-Organising.md`` → ``Organising``
# before slugification). Frontmatter ``title`` is NEVER stripped —
# operator intent is respected.
_PROJECT_FILENAME_PREFIX = "project-"

# Typo-suggestion edit distance threshold. Ratio uses
# ``SequenceMatcher.ratio()`` which is 1.0 for identical strings; 0.75
# corresponds roughly to edit distance ≤ 2 for slugs of typical length.
_TYPO_SIMILARITY_THRESHOLD = 0.75


@dataclass(frozen=True)
class ImportPlan:
    """Validated, parsed, ready-to-execute bulk import."""

    source: Path
    slug: str
    title: str
    body_after_strip: str
    tags: list[str]
    plans: list[TaskCreatePlan]
    parsed_lines: list[ParsedTaskLine]
    is_tasks_only: bool
    is_force_tasks: bool
    yes: bool
    lithos_id_in_frontmatter: str | None


class TasksOnlyPreflightError(Exception):
    """Raised by :func:`check_tasks_only_preflight` for tasks-only mode failures.

    Carries the exit code the CLI should use — 1 for "project not
    found" (operator error, recoverable with greenfield), 2 for
    "lithos_id mismatch" (input validation).
    """

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


class PartialImportError(Exception):
    """Raised by :func:`create_tasks` on mid-batch failure (E1).

    The friction finding (when one was posted) is already in Lithos by
    the time this is raised; the CLI just needs to surface a recovery
    message and exit non-zero.
    """

    def __init__(self, n_created: int, n_total: int, underlying: BaseException) -> None:
        super().__init__(
            f"created {n_created}/{n_total} tasks before failure: {underlying}"
        )
        self.n_created = n_created
        self.n_total = n_total
        self.underlying = underlying


# ── E7: mutually-exclusive flag validation ──────────────────────────────


def validate_import_flags(
    *,
    tasks_only: bool,
    no_tasks: bool,
    force_tasks: bool,
    slug: str | None,
) -> None:
    """Validate the new --tasks-only / --no-tasks / --force-tasks / --slug combos.

    Raises :class:`typer.Exit` with exit code 2 on any mutual-exclusion
    violation, after echoing a helpful error to stderr.

    ``--yes`` without ``--force-tasks`` is silently a no-op (matches
    typical CLI ergonomics: the bypass flag is harmless when there's
    nothing to confirm).
    """
    if no_tasks and tasks_only:
        typer.echo(
            "lithos-loom: --no-tasks and --tasks-only are mutually exclusive "
            "(no-tasks skips task extraction; tasks-only is task extraction only)",
            err=True,
        )
        raise typer.Exit(2)
    if no_tasks and force_tasks:
        typer.echo(
            "lithos-loom: --no-tasks and --force-tasks are mutually exclusive "
            "(--force-tasks deletes existing tasks before re-importing; "
            "meaningless when --no-tasks suppresses task extraction)",
            err=True,
        )
        raise typer.Exit(2)
    if tasks_only and slug is None:
        typer.echo(
            "lithos-loom: --tasks-only requires --slug (frontmatter is ignored "
            "for routing in tasks-only mode — required to prevent silent "
            "mis-routing)",
            err=True,
        )
        raise typer.Exit(2)


# ── Prefix-strip slug derivation ────────────────────────────────────────


def resolve_default_slug_from_stem(source: Path) -> str:
    """Derive a default slug from the source file stem, stripping the
    ``project-`` prefix if present.

    When the stem starts with ``project-`` (case-insensitive), strip it
    BEFORE slugification — the prefix is a common filesystem-organisation
    convention that becomes redundant once docs live under
    ``projects/<slug>/...``.

    NOT applied to frontmatter ``title`` — that's explicit operator
    intent and is respected as-is.

    Note: this returns the post-strip stem ready for slugification; the
    caller still needs to run :func:`_slugify` against the returned
    value. Returning the raw post-strip string here (not the slug)
    keeps the function pure and lets the caller produce a consistent
    error message that names both the original stem and the resolved
    slug.
    """
    stem = source.stem
    lowered = stem.lower()
    if lowered.startswith(_PROJECT_FILENAME_PREFIX):
        return stem[len(_PROJECT_FILENAME_PREFIX) :]
    return stem


def stem_was_prefix_stripped(source: Path) -> bool:
    """Whether the ``project-`` prefix-strip applied. Used by --dry-run output."""
    return source.stem.lower().startswith(_PROJECT_FILENAME_PREFIX)


# ── Validate-all-then-abort report ──────────────────────────────────────


def render_validation_report(errors: list[ValidationError]) -> str:
    """Format every validation error as a single multi-line report.

    Operator can fix all problems in one edit cycle rather than the
    fix-retry-fix-retry death spiral.
    """
    if not errors:
        return ""
    sorted_errors = sorted(errors, key=lambda e: (e.line_number, e.kind))
    header = (
        f"lithos-loom: import refused — {len(sorted_errors)} validation "
        f"problem{'s' if len(sorted_errors) != 1 else ''} found:"
    )
    body = "\n".join(f"  {err.message}" for err in sorted_errors)
    footer = (
        "\nFix the above in the source file, then re-run. No changes were "
        "made to Lithos."
    )
    return f"{header}\n{body}{footer}"


# ── Dry-run preview ───────────────────────────────────────────────────


_DRY_RUN_BANNER = "NO CHANGES MADE — re-run without --dry-run to apply"


def render_dry_run_plan(plan: ImportPlan, *, project_existed: bool) -> str:
    """Format the dry-run plan with NO CHANGES MADE markers at start AND end."""
    lines: list[str] = [_DRY_RUN_BANNER, ""]

    if plan.is_tasks_only:
        state = (
            "existing" if project_existed else "NOT FOUND — re-run would fail preflight"
        )
        lines.append(f"Project: {plan.slug} ({state}) — doc unchanged")
    else:
        body_chars = len(plan.body_after_strip)
        n_stripped = sum(1 for line in plan.parsed_lines if not line.is_empty)
        body_descr = (
            f"<{body_chars} chars after stripping {n_stripped} task line"
            f"{'s' if n_stripped != 1 else ''}>"
        )
        prefix_strip_note = (
            f" (stripped leading 'project-' from stem '{plan.source.stem}'; "
            f"override with --slug)"
            if stem_was_prefix_stripped(plan.source)
            else ""
        )
        lines.extend(
            [
                "WOULD CREATE project:",
                f"  slug={plan.slug}{prefix_strip_note}",
                f"  title={plan.title}",
                f"  tags={plan.tags}",
                f"  body={body_descr}",
            ]
        )

    lines.append("")

    if plan.plans:
        if plan.is_tasks_only:
            lines.append(f"WOULD CREATE {len(plan.plans)} tasks:")
        else:
            lines.append(
                f"WOULD CREATE {len(plan.plans)} tasks "
                f"(top-level: flat; nested: depends_on parent):"
            )
        for entry in _format_task_tree(plan.plans, slug=plan.slug):
            lines.append(f"  {entry}")
    else:
        lines.append("WOULD CREATE 0 tasks (no `- [ ]` lines in source body)")

    lines.extend(["", _DRY_RUN_BANNER])
    return "\n".join(lines)


def _format_task_tree(plans: list[TaskCreatePlan], *, slug: str) -> list[str]:
    """Render the task list as an indented tree for --dry-run output.

    Children (anything with non-zero indent) are nested under their
    parent for visual clarity. Each line shows description + tags +
    priority + parallelizable flag + depends_on hint.

    The auto-added ``#project/<slug>`` tag is included in the rendered
    tag list so the preview is a faithful reflection of what gets
    written. The parser strips a matching project tag from ``line.tags``
    (its job is cross-project detection) and
    :func:`create_tasks` re-adds it at write time; here we mirror that
    re-addition for preview fidelity.
    """
    project_tag = f"project/{slug}"
    parent_label: dict[int, str] = {}
    counters: dict[int, int] = {}
    out: list[str] = []
    top_level_count = 0
    for plan in plans:
        line = plan.line
        if line.indent == 0:
            top_level_count += 1
            label = str(top_level_count)
            parent_label[line.line_number] = label
            counters[line.line_number] = 0
            indent_str = ""
        else:
            # Find the containing parent (a previous plan whose
            # line_number is in our depends_on chain — but it's easier
            # to walk back through the plans list for the most-recent
            # shallower indent).
            parent_ln = _find_parent_line_number(plans, plan)
            parent_lbl = parent_label.get(parent_ln, "?")
            counters[parent_ln] = counters.get(parent_ln, 0) + 1
            child_letter = chr(ord("a") + counters[parent_ln] - 1)
            label = f"{parent_lbl}{child_letter}"
            parent_label[line.line_number] = label
            counters[line.line_number] = 0
            indent_str = "  " * (line.indent and 1)

        # Mirror create_tasks's tag composition: user tags + auto-added project tag.
        rendered_tags = list(line.tags)
        if project_tag not in rendered_tags:
            rendered_tags.append(project_tag)
        tag_part = " " + " ".join(f"#{t}" for t in rendered_tags)
        priority_part = f" priority={line.priority}" if line.priority else ""
        parallel_part = " parallelizable=true" if plan.parallelizable else ""
        depends_part = ""
        if plan.depends_on_line_numbers:
            dep_labels = [
                parent_label.get(ln, f"line-{ln}")
                for ln in plan.depends_on_line_numbers
            ]
            depends_part = f"  (depends_on=#{','.join(dep_labels)})"

        desc = line.description if line.description else "(empty)"
        meta = f"{tag_part}{priority_part}{parallel_part}{depends_part}"
        out.append(f'{indent_str}{label}. "{desc}"{meta}')
    return out


def _find_parent_line_number(plans: list[TaskCreatePlan], child: TaskCreatePlan) -> int:
    """Return the line_number of the parent of `child`.

    Mirrors the stack-walk in :func:`task_graph.build_plan` but for
    rendering only.
    """
    for prev in reversed(plans):
        if prev.line.line_number >= child.line.line_number:
            continue
        if prev.line.indent < child.line.indent:
            return prev.line.line_number
    return -1  # shouldn't happen for non-top-level lines


# ── Typo hint for tasks-only "project not found" ───────────────────────


def render_typo_hint(unknown_slug: str, known_slugs: list[str]) -> str:
    """Return a "did you mean:" suffix listing slugs within edit distance ≤ 2.

    Returns an empty string when there are no matches.
    """
    matches: list[tuple[float, str]] = []
    for known in known_slugs:
        ratio = SequenceMatcher(None, unknown_slug, known).ratio()
        if ratio >= _TYPO_SIMILARITY_THRESHOLD:
            matches.append((ratio, known))
    if not matches:
        return ""
    matches.sort(key=lambda item: (-item[0], item[1]))
    candidates = ", ".join(slug for _, slug in matches[:3])
    return f"; did you mean: {candidates}?"


# ── Tasks-only preflight ────────────────────────────────────────────────


async def check_tasks_only_preflight(
    *,
    cfg: LoomConfig,
    slug: str,
    lithos_id_in_frontmatter: str | None,
) -> tuple[str, list[Task]]:
    """Verify the project exists and (if frontmatter has lithos_id) it matches.

    Returns ``(project_id, existing_open_tasks_tagged_project_slug)``.

    Raises:
        TasksOnlyPreflightError: project doesn't exist, OR lithos_id
            in frontmatter doesn't resolve to ``slug``.
    """
    project_id: str | None = None
    existing_tasks: list[Task] = []
    preflight_error: TasksOnlyPreflightError | None = None
    deferred_error: OSError | LithosClientError | None = None

    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        try:
            project_id = await _resolve_project_id(client, slug)
            if project_id is None:
                # Typo hint: suggest close slug matches.
                all_projects = await client.note_list(
                    path_prefix="projects/", limit=500
                )
                known_slugs = sorted({s.slug for s in all_projects if s.slug})
                hint = render_typo_hint(slug, known_slugs)
                preflight_error = TasksOnlyPreflightError(
                    f"lithos-loom: no project at slug={slug!r}{hint}. "
                    f"Use 'project import' (without --tasks-only) to create a "
                    f"new project at this slug.",
                    exit_code=1,
                )
            elif lithos_id_in_frontmatter is not None:
                # Verify lithos_id in the frontmatter resolves to the named slug.
                frontmatter_note = await client.note_read(id=lithos_id_in_frontmatter)
                if frontmatter_note is None:
                    preflight_error = TasksOnlyPreflightError(
                        f"lithos-loom: frontmatter lithos_id "
                        f"{lithos_id_in_frontmatter!r} not found in Lithos",
                        exit_code=2,
                    )
                elif frontmatter_note.slug != slug:
                    preflight_error = TasksOnlyPreflightError(
                        f"lithos-loom: frontmatter lithos_id resolves to "
                        f"project {frontmatter_note.slug!r}; --slug={slug!r} — "
                        f"refusing (would file tasks against the wrong project)",
                        exit_code=2,
                    )

            if preflight_error is None:
                existing_tasks = await _list_existing_tasks_for_project(client, slug)
        except (OSError, LithosClientError) as exc:
            deferred_error = exc

    if deferred_error is not None:
        raise deferred_error
    if preflight_error is not None:
        raise preflight_error
    assert project_id is not None
    return project_id, existing_tasks


async def _resolve_project_id(client: LithosClient, slug: str) -> str | None:
    """Look up the canonical project doc id for ``slug``.

    Mirrors the canonical-doc picker in ``project_list``: prefer
    ``projects/<slug>/<slug>-project-context.md`` and fall back to the
    lexicographically-smallest path under ``projects/<slug>/``. Returns
    ``None`` if no doc exists.
    """
    summaries = await client.note_list(path_prefix=f"projects/{slug}/", limit=50)
    if not summaries:
        return None
    canonical_path = f"projects/{slug}/{slug}-project-context.md"
    for summary in summaries:
        if summary.path == canonical_path:
            return summary.id
    canonical = min(summaries, key=lambda s: s.path)
    return canonical.id


async def _list_existing_tasks_for_project(
    client: LithosClient, slug: str
) -> list[Task]:
    """Return all tasks (any status) whose ``metadata.project == slug``.

    The existence check that gates ``--tasks-only`` refusal counts ALL
    existing project tasks — open, completed, and cancelled. The render
    layer reads ``metadata["project"]`` for the canonical project
    association (``render.py:119``); the ``#project/<slug>`` tag is also
    written to the task at creation time so Lithos-side ``task_list`` tag
    filters find them.
    """
    all_tasks = await client.task_list()
    return [task for task in all_tasks if task.metadata.get("project") == slug]


# ── E5 + E6: force-tasks cleanup ───────────────────────────────────────


async def force_tasks_cleanup(*, cfg: LoomConfig, existing_tasks: list[Task]) -> int:
    """Cancel every OPEN task in ``existing_tasks``. Returns count cancelled.

    Already-resolved tasks (status=completed or status=cancelled) are
    skipped — they're history, and Lithos has no hard-delete primitive
    today (E5). Cancelling a completed task would rewrite history from
    "done" to "cancelled", which is wrong. The new import creates a
    fresh set of open tasks; the historical record stays intact.

    The interactive confirm prompt is the CLI's responsibility (must
    happen outside the async context); this helper only does the
    mutations.
    """
    cancelled = 0
    deferred_error: OSError | LithosClientError | None = None
    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        try:
            for task in existing_tasks:
                if task.status != "open":
                    continue
                await client.task_cancel(
                    task_id=task.id, reason="bulk-import --force-tasks"
                )
                cancelled += 1
        except (OSError, LithosClientError) as exc:
            deferred_error = exc
    if deferred_error is not None:
        raise deferred_error
    return cancelled


# ── E4 + E1: bulk task creation ────────────────────────────────────────


async def create_tasks(
    *,
    cfg: LoomConfig,
    slug: str,
    plans: list[TaskCreatePlan],
    source: Path,
) -> int:
    """Create all tasks per ``plans``. Returns count created on success.

    On mid-batch failure:

    * Posts a ``[Friction]`` finding against the first
      successfully-created task with the recovery command embedded in
      the summary (E1).
    * If no tasks were created before the failure, logs a
      ``[Friction]`` WARNING instead (no task to attach to; mirrors
      the precedent in ``_note_conflict.py``).
    * Raises :class:`PartialImportError` so the CLI surfaces a
      non-zero exit with the recovery command.

    ``source`` is captured into the recovery command so the operator
    can copy/paste it directly.
    """
    sorted_plans = _topologically_sort(plans)
    line_to_id: dict[int, str] = {}
    first_created: str | None = None
    failure: BaseException | None = None
    n_total = len(sorted_plans)
    project_tag = f"project/{slug}"

    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        for plan in sorted_plans:
            metadata: dict[str, object] = {"project": slug}
            if plan.line.priority is not None:
                metadata["priority"] = plan.line.priority
            if plan.depends_on_line_numbers:
                metadata["depends_on"] = [
                    line_to_id[ln] for ln in plan.depends_on_line_numbers
                ]
            if plan.parallelizable:
                metadata["parallelizable"] = True

            # Auto-add the project routing tag if the source line didn't
            # already carry it. The parser strips
            # ``#project/<slug>`` from the per-line tag list (since
            # it's a routing concern, not user metadata), so this is
            # always the canonical write site for it. Set on the task
            # entity (not just inferred from metadata) so Lithos-side
            # ``task_list`` tag-filter queries find these tasks.
            tags = list(plan.line.tags)
            if project_tag not in tags:
                tags.append(project_tag)

            try:
                task_id = await client.task_create(
                    title=plan.line.description or "(no description)",
                    tags=tags,
                    metadata=metadata,
                )
            except (OSError, LithosClientError) as exc:
                failure = exc
                break

            line_to_id[plan.line.line_number] = task_id
            if first_created is None:
                first_created = task_id

        if failure is not None:
            n_created = len(line_to_id)
            recovery = (
                f"uv run lithos-loom project import {source} "
                f"--tasks-only --slug {slug} --force-tasks"
            )
            summary = (
                f"[Friction] bulk-import partial-failure: project={slug} "
                f"tasks_created={n_created}/{n_total} error={failure}; "
                f"recovery: {recovery}"
            )
            if first_created is not None:
                try:
                    await client.finding_post(task_id=first_created, summary=summary)
                except (OSError, LithosClientError) as finding_exc:
                    # Don't mask the original failure
                    _LOG.warning(
                        "create_tasks: failed to post recovery finding (%s); "
                        "original error: %s",
                        finding_exc,
                        failure,
                    )
            else:
                # E1 fallback: no task to attach to
                _LOG.warning("%s", summary)

    if failure is not None:
        raise PartialImportError(
            n_created=len(line_to_id), n_total=n_total, underlying=failure
        )
    return len(line_to_id)


def _topologically_sort(plans: list[TaskCreatePlan]) -> list[TaskCreatePlan]:
    """Sort plans so all of a plan's dependencies appear before it.

    E4: children before parents (parents reference child line_numbers
    in their depends_on). Sequential siblings: each child references
    the previous child. The graph is always a DAG when built by
    :func:`task_graph.build_plan`.

    O(n²) but n is small (typically <50 tasks per import).
    """
    remaining = list(plans)
    done: set[int] = set()
    out: list[TaskCreatePlan] = []
    while remaining:
        next_round: list[TaskCreatePlan] = []
        progressed = False
        for plan in remaining:
            if all(dep in done for dep in plan.depends_on_line_numbers):
                out.append(plan)
                done.add(plan.line.line_number)
                progressed = True
            else:
                next_round.append(plan)
        if not progressed:
            unresolved = [p.line.line_number for p in next_round]
            raise RuntimeError(
                f"cycle in task graph; remaining line_numbers={unresolved!r} "
                f"(this is a bug in task_graph.build_plan — please report)"
            )
        remaining = next_round
    return out
