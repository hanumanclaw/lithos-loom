"""Git helpers — base SHA, commits-since, dirty detection (US-13).

Lifted from Ralph++ ``ralph_pp/steps/_git.py`` and trimmed to the three
primitives Loom needs. All functions shell out to ``git`` with an explicit
``cwd`` and raise :class:`RuntimeError` on non-zero exit so callers fail loudly
rather than acting on a half-read repo.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path


def _git(worktree: Path, *args: str) -> str:
    """Run ``git *args`` in *worktree*; return stripped stdout or raise."""
    result = subprocess.run(
        ["git", *args],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def base_sha(worktree: Path) -> str:
    """Return the current ``HEAD`` SHA of *worktree*.

    Callers record this immediately after worktree creation (before any agent
    commit) so :func:`commits_since` can later enumerate the round commits.
    """
    return _git(worktree, "rev-parse", "HEAD")


def commits_since(worktree: Path, base_sha: str) -> list[str]:
    """Return full 40-char SHAs added since *base_sha*, in chronological order."""
    out = _git(worktree, "rev-list", "--reverse", f"{base_sha}..HEAD")
    return out.splitlines() if out else []


def has_uncommitted_changes(worktree: Path) -> bool:
    """Return True if *worktree* has staged or unstaged changes."""
    return bool(_git(worktree, "status", "--porcelain"))


def commit_all(
    worktree: Path, message: str, *, exclude: Sequence[str] = ()
) -> str | None:
    """Stage all changes (minus *exclude* pathspecs) and commit if any remain.

    *exclude* entries are git pathspecs (e.g. ``".handoff"``) kept out of the
    commit — used to keep orchestration scaffolding out of the deliverable
    branch. Returns the new commit SHA, or ``None`` when nothing was staged.
    """
    pathspec = [".", *(f":(exclude){p}" for p in exclude)]
    _git(worktree, "add", "-A", "--", *pathspec)
    # Defensively unstage excluded paths too, in case something was already
    # staged before this call (the agent is told not to, but must not be able
    # to leak .handoff/ into the deliverable commit).
    for p in exclude:
        _git(worktree, "reset", "-q", "--", p)
    if not _git(worktree, "diff", "--cached", "--name-only"):
        return None
    _git(worktree, "commit", "-m", message)
    return base_sha(worktree)
