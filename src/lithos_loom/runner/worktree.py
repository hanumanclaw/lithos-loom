"""Per-task git worktree helper (US-11).

Stub — lifted from Ralph++ ``ralph_pp/`` and adapted to take an arbitrary base
branch (Ralph++ always used ``main``; Loom branches off the per-PRD integration
branch ``loom/<prd-slug>``).

Branch naming follows ``{name}-{8charrandom}`` to guarantee uniqueness.
"""

from __future__ import annotations

from pathlib import Path


def create(repo: Path, base_branch: str, name: str) -> Path:
    """Create a per-task worktree off ``base_branch``. Stub — implement per US-11."""
    raise NotImplementedError("runner.worktree.create — implement per US-11")


def remove(path: Path, *, force: bool = False) -> None:
    """Remove a worktree. Refuses dirty trees unless ``force=True``."""
    raise NotImplementedError("runner.worktree.remove — implement per US-11")
