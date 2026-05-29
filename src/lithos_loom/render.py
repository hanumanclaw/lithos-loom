"""Projected-line renderer shared between the projection subscription
and the capture-macro CLI.

Renders a single Tasks-plugin line for a Lithos task. Extracted from
:mod:`lithos_loom.subscriptions._obsidian_projection` so the
``lithos-loom task create`` CLI can produce a line identical to what
the projection would write for the same task — a macro-inserted line
and a projection-rewritten line must be byte-equal so the fs-watcher's
self-write suppression treats them as the same content.

The renderer is pure: given a :class:`~lithos_loom.lithos_client.Task`
plus the route config and the local-tz "today", it returns a single-
line markdown string. No I/O. Malformed metadata is warn-logged once
per call (mirrors the projection's silent-degradation contract) and
the offending marker is omitted.

Public surface:

* :func:`render_line` — open-task line (``- [ ] ...``).
* :func:`render_resolved_line` — terminal-state line (``- [x]`` /
  ``- [-]``) with the resolution date marker.
* :data:`PRIORITY_EMOJI` — priority enum → Tasks-plugin emoji table.
  Values: ``highest``/``high``/``medium``/``low``/``lowest``.

The helpers (:func:`priority_marker`, :func:`dep_markers`,
:func:`due_date_str`, :func:`parse_scheduled_for`,
:func:`validated_priority`) are also exported so tests can exercise
the per-marker logic in isolation and the fs-watcher's anti-drift
test for :data:`PRIORITY_EMOJI` can pin its inverse.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from lithos_loom.config import RouteConfig
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions._human_actionable import human_blocking_route_name

__all__ = [
    "PRIORITY_EMOJI",
    "TASK_ID_RE",
    "dep_markers",
    "due_date_str",
    "extract_task_ids",
    "parse_scheduled_for",
    "priority_marker",
    "render_line",
    "render_resolved_line",
    "validated_priority",
]

logger = logging.getLogger(__name__)


TASK_ID_RE = re.compile(r"🆔 lithos:(?P<task_id>[A-Za-z0-9_-]+)")
"""Matches the ``🆔 lithos:<id>`` stable-identifier marker that both
:func:`render_line` and :func:`render_resolved_line` emit immediately
after the title. Consumers parse the id back out of an on-disk line to
recover which Lithos task a projected/archived line belongs to — the
projection seeds its ``surfaced`` set from ``tasks.md`` on restart, and
the task-archive subscription dedups against ids already on disk in a
``<slug>-done.md`` file. The character class matches the same id shape
``render_line`` writes (Lithos ids are ``[A-Za-z0-9_-]``)."""


def extract_task_ids(text: str) -> set[str]:
    """Return the set of Lithos task ids referenced by ``🆔 lithos:<id>``
    markers anywhere in ``text``.

    Used to recover task identity from already-written vault content:
    the projection's restart seed (parse ``tasks.md``) and the
    task-archive dedup-cache load (parse ``<slug>-done.md``). Lines
    without the marker contribute nothing, so prose / headers / blank
    lines are inert."""
    return {m.group("task_id") for m in TASK_ID_RE.finditer(text)}


PRIORITY_EMOJI: dict[str, str] = {
    "highest": "🔺",
    "high": "⏫",
    "medium": "🔼",
    "low": "🔽",
    "lowest": "⏬",
}
"""Priority enum → Tasks-plugin emoji.

Values: ``highest``/``high``/``medium``/``low``/``lowest``.

Strict case-sensitive match: the Lithos surface owns this enum, and
deviation would mean a task with a non-canonical priority value
silently drops the marker. The fs-watcher's
``test_priority_emoji_table_matches_projection_table`` test pins
this table against the inverse map in
:mod:`lithos_loom.sources.obsidian_fs_watcher`."""


def render_line(
    task: Task,
    routes: Sequence[RouteConfig],
    today: date,
) -> str:
    """Render one Tasks-plugin task line for an open task.

    Field order (omit optional markers when they don't apply):

        - [ ] <title> 🆔 lithos:<id> [#project/<slug>] \
            [#lithos/<route>] [⛔ lithos:<dep>]... [<prio>] [📅 <date>]

    Layout follows the canonical Tasks-plugin emoji format the
    user empirically confirmed by re-running a task through the
    plugin's own rewrite dialogue:

      title → 🆔 (stable identifier, immediately after title) →
      tags → Tasks-plugin emoji metadata (⛔ deps, priority, dates)

    The plugin's public emoji-format docs
    (`Reference/Task Formats/Tasks Emoji Format`) don't formally
    pin field order, but mid-line emoji metadata is silently
    ignored for sort/filter — only trailing-position metadata is
    parsed. Tags must come BEFORE trailing emoji metadata; 🆔 is
    the lone exception (immediately after title) because it acts
    as a stable identifier other tasks reference via ⛔, not as
    sort/filter metadata.

    Within trailing metadata, the order is: ⛔ deps → priority →
    📅 date. Matches the plugin's rewrite-dialogue output.

    Titles with embedded newlines (rare in Lithos but possible) are
    collapsed to spaces so the markdown line stays single-line. The
    ``🆔 lithos:<id>`` marker is what lets the projection's content-hash
    dedup identify the same task across rewrites.

    Completed/cancelled lines are kept around for ``resolved_ttl_days``
    (see :func:`render_resolved_line`).
    """
    title = " ".join(task.title.split())  # collapse \n, \r, runs of spaces
    parts: list[str] = [f"- [ ] {title}", f"🆔 lithos:{task.id}"]

    # Tags BEFORE Tasks-plugin emoji metadata so the trailing
    # metadata sorts/filters correctly.
    project = task.metadata.get("project")
    if isinstance(project, str) and project:
        parts.append(f"#project/{project}")

    route_name = human_blocking_route_name(task, routes)
    if route_name:
        parts.append(f"#lithos/{route_name}")

    # Trailing Tasks-plugin emoji metadata: deps → priority → due date.
    parts.extend(dep_markers(task))

    priority = priority_marker(task)
    if priority is not None:
        parts.append(priority)

    due = due_date_str(task, routes, today)
    if due is not None:
        parts.append(f"📅 {due}")

    return " ".join(parts)


def render_resolved_line(task: Task, status: str, resolved_at: date) -> str:
    """Render the historical-line shape for completed/cancelled tasks.

    Field order:

        - [x] <title> 🆔 lithos:<id> [#project/<slug>] ✅ <date>
        - [-] <title> 🆔 lithos:<id> [#project/<slug>] ❌ <date>

    Layout follows the same Tasks-plugin convention as
    :func:`render_line`: title → 🆔 → tag → trailing emoji metadata
    (here the ✅/❌ date is the only emoji metadata for resolved
    tasks). The ✅ / ❌ date must be at the END so the Tasks plugin's
    `sort by done date` / `done after Y-M-D` filters parse it
    correctly.

    Resolved tasks drop priority / dep / due-date / route-name
    markers — they are historical record, not actionable work. The
    ``#project/<slug>`` tag is kept so the operator's
    ``done-this-week-for-project-X`` queries still cluster correctly.
    """
    checkbox = "[x]" if status == "completed" else "[-]"
    marker_emoji = "✅" if status == "completed" else "❌"
    title = " ".join(task.title.split())
    parts: list[str] = [f"- {checkbox} {title}", f"🆔 lithos:{task.id}"]
    project = task.metadata.get("project")
    if isinstance(project, str) and project:
        parts.append(f"#project/{project}")
    # Done/cancelled date at the END for Tasks-plugin filter recognition.
    parts.append(f"{marker_emoji} {resolved_at.isoformat()}")
    return " ".join(parts)


def priority_marker(task: Task) -> str | None:
    """Map ``task.metadata.priority`` to its Tasks-plugin emoji.

    Returns ``None`` for absent / non-string / unknown-enum values so
    the renderer simply omits the marker. Unknown values are warn-
    logged once per event — same shape as :func:`parse_scheduled_for`,
    because malformed metadata must never crash the projection.
    """
    value = task.metadata.get("priority")
    if value is None:
        return None
    if not isinstance(value, str):
        logger.warning(
            "render: ignoring non-string metadata.priority=%r",
            value,
        )
        return None
    emoji = PRIORITY_EMOJI.get(value)
    if emoji is None:
        logger.warning(
            "render: ignoring unknown metadata.priority=%r (expected one of: %s)",
            value,
            ", ".join(PRIORITY_EMOJI),
        )
    return emoji


def validated_priority(task: Task) -> str | None:
    """Return ``task.metadata.priority`` only when it's a known enum
    value, else ``None``.

    Parallel to :func:`priority_marker` but returns the enum string
    rather than the emoji — the projection's ``_StateEntry`` carries
    the enum so ``_flush`` can pass per-task priority into
    :meth:`ProjectionSyncState.record_projection_write` without
    re-parsing the rendered line. Deliberately silent on malformed
    values: :func:`priority_marker` already warns on the same code
    path (called by :func:`render_line`), so we'd duplicate the
    warning if this also logged.
    """
    value = task.metadata.get("priority")
    if isinstance(value, str) and value in PRIORITY_EMOJI:
        return value
    return None


def dep_markers(task: Task) -> list[str]:
    """Render one ``⛔ lithos:<dep_id>`` marker per entry in
    ``task.metadata.depends_on``.

    Preserves list order; dedups duplicate IDs (first occurrence
    wins) since the marker is a visual signal not a count. Returns
    ``[]`` for absent / ``None`` / non-list / all-invalid inputs.
    Non-string and empty-string entries are skipped with a single
    warn per event — same shape as :func:`priority_marker` and
    :func:`parse_scheduled_for`, so malformed metadata can never
    crash the subscription loop.
    """
    raw = task.metadata.get("depends_on")
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.warning(
            "render: ignoring non-list metadata.depends_on=%r",
            raw,
        )
        return []

    seen: set[str] = set()
    markers: list[str] = []
    bad: list[Any] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            bad.append(entry)
            continue
        if entry in seen:
            continue
        seen.add(entry)
        markers.append(f"⛔ lithos:{entry}")
    if bad:
        logger.warning(
            "render: skipping invalid entries in metadata.depends_on=%r",
            bad,
        )
    return markers


def due_date_str(
    task: Task,
    routes: Sequence[RouteConfig],
    today: date,
) -> str | None:
    """Hybrid due-date policy for the ``📅`` marker.

    - ``task.metadata.scheduled_for`` (if present and parseable) is an
      explicit override and wins for all cases.
    - Else, tasks claimed by a ``human_blocking = true`` route render
      ``today`` so they surface in the operator's daily query.
    - Else (orphan / backlog), no ``📅`` marker is emitted; the user's
      Inbox query (open tasks without due/scheduled/start date) picks
      them up naturally.
    """
    override = parse_scheduled_for(task.metadata.get("scheduled_for"))
    if override is not None:
        return override.isoformat()
    if human_blocking_route_name(task, routes) is not None:
        return today.isoformat()
    return None


def parse_scheduled_for(value: Any) -> date | None:
    """Best-effort parse of ``metadata.scheduled_for``.

    Accepts ``YYYY-MM-DD`` and full ISO 8601 datetime strings; returns
    ``None`` for anything we can't read. Malformed metadata must never
    crash the projection — a warn-and-fall-through is the right shape.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        # Datetime form ('2026-06-15T09:00:00Z' etc.). fromisoformat
        # in 3.11+ accepts the trailing 'Z'.
        if "T" in value:
            return datetime.fromisoformat(value).date()
        return date.fromisoformat(value)
    except ValueError:
        logger.warning(
            "render: ignoring malformed metadata.scheduled_for=%r",
            value,
        )
        return None
