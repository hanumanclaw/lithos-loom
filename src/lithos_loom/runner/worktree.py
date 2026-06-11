"""Per-task git worktree helper (US-11).

Lifted from Ralph++ ``ralph_pp/steps/worktree.py`` and trimmed: no rich console,
no ``python-slugify`` dependency (a small inline slug is enough), and an
arbitrary base branch (Ralph++ always branched off ``main``; Loom branches off
the caller-supplied base).

Branch naming follows ``{slug(name)}-{8charrandom}`` to guarantee uniqueness.
The worktree directory is created under *parent* (default: the repo's parent
directory) so concurrent runs never collide.
"""

from __future__ import annotations

import re
import secrets
import subprocess
from pathlib import Path

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: str, *, max_length: int = 50) -> str:
    """Lowercase, collapse non-alphanumerics to ``-``, trim to *max_length*."""
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")[:max_length].strip("-")
    return slug or "task"


def create(
    repo: Path,
    base_branch: str,
    name: str,
    *,
    parent: Path | None = None,
) -> Path:
    """Create a per-task worktree off *base_branch* and return its path.

    A fresh branch ``{slug(name)}-{8hex}`` is created at *base_branch*. The
    worktree directory is placed under *parent* (default ``repo.parent``).
    """
    base_dir = parent if parent is not None else repo.parent
    base_dir.mkdir(parents=True, exist_ok=True)

    for _attempt in range(5):
        branch = f"{_slug(name)}-{secrets.token_hex(4)}"
        path = base_dir / branch
        if not path.exists():
            break
    else:  # pragma: no cover - astronomically unlikely
        raise RuntimeError("could not find a free worktree path after 5 attempts")

    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(path), base_branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git worktree add failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return path


def remove(path: Path, *, force: bool = False) -> None:
    """Remove a worktree. Refuses a dirty tree unless *force* is True.

    Runs from the main repository (derived from the worktree's common git dir)
    so git does not refuse to remove "the current working tree".
    """
    common = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if common.returncode != 0:
        raise RuntimeError(f"not a git worktree: {path} ({common.stderr.strip()})")
    # git-common-dir is "<repo>/.git"; its parent is the main working tree.
    common_dir = Path(common.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (path / common_dir).resolve()
    main_repo = common_dir.parent

    args = ["git", "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    result = subprocess.run(args, cwd=main_repo, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"git worktree remove failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
