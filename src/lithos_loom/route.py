"""Route matching and dependency resolution.

Stub — implements ``docs/prd/mvp.md`` US-5 (tag-based matching, first-match-wins)
and US-6 / US-9 (``metadata.depends_on`` resolution, cycle detection,
parallelizable siblings).

This is a deep module: pure function over Lithos task list + route table,
trivial to test exhaustively.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MatchResult:
    """One runnable (task, route) pair."""

    task_id: str
    route_name: str


def select_runnable_tasks(*args: object, **kwargs: object) -> list[MatchResult]:
    """Stub — implement per docs/prd/mvp.md US-5 / US-6 / US-9."""
    raise NotImplementedError(
        "route.select_runnable_tasks — implement tag matching + depends_on resolution"
    )
