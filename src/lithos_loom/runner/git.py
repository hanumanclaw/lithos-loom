"""Git helpers — base SHA, commits-since, dirty detection (US-13).

Stub — lifted from Ralph++.
"""

from __future__ import annotations

from pathlib import Path


def base_sha(worktree: Path) -> str:
    """Return the SHA the worktree branched from. Stub — implement per US-13."""
    raise NotImplementedError("runner.git.base_sha — implement per US-13")


def commits_since(worktree: Path, base_sha: str) -> list[str]:
    """Return full 40-char SHAs added since ``base_sha`` in chronological order."""
    raise NotImplementedError("runner.git.commits_since — implement per US-13")


def has_uncommitted_changes(worktree: Path) -> bool:
    """Return True if the worktree has uncommitted changes."""
    raise NotImplementedError(
        "runner.git.has_uncommitted_changes — implement per US-13"
    )
