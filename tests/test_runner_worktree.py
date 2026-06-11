"""Unit tests for ``lithos_loom.runner.worktree``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.runner import worktree


def _branch_of(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_create_makes_worktree_on_new_branch(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    parent = tmp_path / "wts"
    wt = worktree.create(tmp_git_repo, "main", "Add a CLI flag!", parent=parent)
    assert wt.is_dir()
    assert wt.parent == parent
    # branch name is the dir name, slugged + random suffix
    assert wt.name.startswith("add-a-cli-flag-")
    assert _branch_of(wt) == wt.name
    # worktree HEAD matches the base branch tip
    repo_head = subprocess.run(
        ["git", "rev-parse", "main"], cwd=tmp_git_repo, capture_output=True, text=True
    ).stdout.strip()
    wt_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True
    ).stdout.strip()
    assert wt_head == repo_head


def test_create_is_unique(tmp_git_repo: Path, tmp_path: Path) -> None:
    a = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    b = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    assert a != b


def test_remove_deletes_clean_worktree(tmp_git_repo: Path, tmp_path: Path) -> None:
    wt = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    worktree.remove(wt)
    assert not wt.exists()


def test_remove_refuses_dirty_without_force(tmp_git_repo: Path, tmp_path: Path) -> None:
    wt = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    (wt / "untracked.txt").write_text("dirty")
    with pytest.raises(RuntimeError):
        worktree.remove(wt, force=False)
    worktree.remove(wt, force=True)
    assert not wt.exists()


def test_remove_rejects_non_worktree(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        worktree.remove(tmp_path / "nope")
