"""Logic for ``lithos-loom project regenerate-done`` (rebuild a per-project
task-archive done file from Lithos).

The ``task-archive`` subscription only appends tasks the operator
*surfaced* in ``_lithos/tasks.md``, and only from the moment the daemon
started archiving. This rebuilds ``<vault>/_lithos/projects/<slug>/<slug>-done.md``
from scratch by querying Lithos for **every** resolved (completed +
cancelled) task carrying ``metadata.project == <slug>``.

"Surfaced" is an ephemeral, in-memory daemon signal that can't be
reconstructed after the fact, so a regeneration deliberately writes ALL
resolved tasks for the slug — a superset of what the live archiver would
have captured. The operator accepts that trade for a complete history
(e.g. backfilling work that predates the archiver, or rebuilding a
deleted file).

The thin Typer command lives in :mod:`lithos_loom.cli.project`; the
testable I/O-light logic lives here (mirrors the
``_project_import_bulk`` split).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from lithos_loom.config import LoomConfig
from lithos_loom.lithos_client import LithosClient, Task
from lithos_loom.render import render_resolved_line

__all__ = [
    "build_done_content",
    "collect_resolved_lines",
    "render_dry_run",
]

_DRY_RUN_BANNER = "NO CHANGES MADE — re-run without --dry-run to apply"

# Lithos terminal statuses. Order is irrelevant — lines are sorted by
# resolution date, not status.
_TERMINAL_STATUSES = ("completed", "cancelled")


def _resolved_date(task: Task) -> date:
    """Pick the resolution date for a resolved task's archive line.

    Prefers Lithos's canonical ``resolved_at`` (lithos#286), falling
    back to ``created_at`` then today only defensively — a task returned
    under a terminal-status filter should always carry ``resolved_at``,
    but we never want a missing timestamp to abort the whole rebuild.
    Returns a local-tz ``date`` so the ✅/❌ marker matches the
    operator's calendar (same convention as the projection's
    ``_resolved_at_for``).
    """
    stamp = task.resolved_at or task.created_at
    if stamp is not None:
        return stamp.astimezone().date()
    return date.today()


async def collect_resolved_lines(*, cfg: LoomConfig, slug: str) -> list[str]:
    """Fetch every resolved task for ``slug`` and render its archive line.

    Queries Lithos for completed + cancelled tasks (unbounded — no
    ``resolved_since`` window, so the full retained history is returned),
    keeps those whose ``metadata.project`` equals ``slug``, dedups by
    task id, and renders each via :func:`render_resolved_line`. Lines are
    sorted ascending by resolution date (ties broken by task id) so the
    rebuilt file reads chronologically like an append-only log would have
    grown.

    Lets ``OSError`` (Lithos unreachable) and ``LithosClientError`` (error
    envelope) propagate so the CLI renders the same connection-/Lithos-
    specific messages as ``project import``.
    """
    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        resolved: list[Task] = []
        for status in _TERMINAL_STATUSES:
            resolved.extend(await client.task_list(status=status))

    seen: set[str] = set()
    selected: list[Task] = []
    for task in resolved:
        if task.metadata.get("project") != slug:
            continue
        if task.id in seen:
            continue
        seen.add(task.id)
        selected.append(task)

    selected.sort(key=lambda t: (_resolved_date(t), t.id))
    return [
        render_resolved_line(task, task.status, _resolved_date(task))
        for task in selected
    ]


def build_done_content(lines: list[str]) -> str:
    """Render the full done-file body from ``lines``.

    Bare Tasks-plugin lines, no header / frontmatter, trailing newline
    when non-empty so the file ends cleanly. Empty input yields an empty
    string (the caller decides whether to write it)."""
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def render_dry_run(slug: str, done_path: Path, lines: list[str]) -> str:
    """Format the ``--dry-run`` preview, framed with NO-CHANGES banners."""
    out: list[str] = [_DRY_RUN_BANNER, ""]
    out.append(f"Would regenerate {done_path} for project '{slug}'")
    out.append(f"  resolved tasks found: {len(lines)}")
    if lines:
        out.append("")
        out.extend(f"  {line}" for line in lines)
    out.extend(["", _DRY_RUN_BANNER])
    return "\n".join(out)
