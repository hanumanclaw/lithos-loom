"""CLI-boundary validation tests for the story-develop entry point.

These exercise ``main()``'s fail-fast guards, which return before any Docker /
agent work happens.
"""

from __future__ import annotations

from pathlib import Path

from lithos_loom.plugins.story_develop.__main__ import main


def test_main_rejects_empty_description(tmp_git_repo: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_git_repo), "--description", "   "])
    assert rc == 2
    assert "description must not be empty" in capsys.readouterr().err


def test_main_rejects_non_git_repo(tmp_path: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_path), "--description", "do a thing"])
    assert rc == 2
    assert "not a git repository" in capsys.readouterr().err
