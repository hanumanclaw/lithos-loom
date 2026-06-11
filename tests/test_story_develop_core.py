"""Orchestration tests for ``develop()`` (T1 walking skeleton).

Real git/worktree against a temp repo; the container + coder turn are
monkeypatched so no Docker or agent is needed. The fake coder writes a source
file + handoff into the worktree it is told about via the captured run command.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import containers
from lithos_loom.plugins.story_develop import develop as develop_mod
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.turns import CoderTurnResult


def _worktree_from_run_cmd(run_cmd) -> Path:
    for i, arg in enumerate(run_cmd):
        if arg == "-v" and run_cmd[i + 1].endswith(":/workspace"):
            return Path(run_cmd[i + 1].split(":", 1)[0])
    raise AssertionError("no /workspace mount in run cmd")


@pytest.fixture
def config(tmp_git_repo: Path, tmp_path: Path) -> DevelopConfig:
    # empty config dir -> no auth files mounted -> no dependency on real ~/.claude
    cfg_dir = tmp_path / "fake-claude"
    cfg_dir.mkdir()
    return DevelopConfig(
        repo=tmp_git_repo,
        description="Add a greeting file",
        work_dir=tmp_path / "work",
        claude_config_dir=cfg_dir,
    )


def _install_fake_coder(
    monkeypatch: pytest.MonkeyPatch,
    config: DevelopConfig,
    *,
    write_handoff: bool,
    write_source: bool,
    ok: bool,
) -> dict:
    state: dict = {}

    def fake_start(run_cmd) -> str:
        state["worktree"] = _worktree_from_run_cmd(run_cmd)
        return "container-id"

    def fake_run_turn(*, container, prompt, session_id, resume=False, timeout):
        wt = state["worktree"]
        if write_source:
            (wt / "greeting.txt").write_text("hello\n")
        if write_handoff:
            # the coder writes to the handoff dir (mounted outside the worktree)
            (config.handoff_dir / "round_01_coder_done.md").write_text(
                "## Status: LGTM\n## Summary\nWrote greeting.txt; tests pass.\n"
            )
        return CoderTurnResult(
            exit_code=0 if ok else 1,
            succeeded=ok,
            session_id=session_id,
            result_text="done" if ok else "",
            cost_usd=0.0123,
            raw={"is_error": not ok},
            stderr="" if ok else "boom",
        )

    monkeypatch.setattr(containers, "start_container", fake_start)
    monkeypatch.setattr(
        containers, "stop_container", lambda name: state.setdefault("stopped", name)
    )
    monkeypatch.setattr(develop_mod, "run_coder_turn", fake_run_turn)
    return state


def _commit_count_since_base(result) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", f"{result.base_sha}..HEAD"],
        cwd=result.worktree,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return int(out or 0)


def test_develop_success(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install_fake_coder(
        monkeypatch, config, write_handoff=True, write_source=True, ok=True
    )
    result = develop_mod.develop(config)

    assert result.status == "succeeded"
    assert result.succeeded
    assert result.handoff_present
    assert len(result.commits) == 1
    assert result.branch == result.worktree.name
    assert result.coder_cost_usd == 0.0123
    # container was torn down
    assert state["stopped"] == containers.container_name(config.run_id, "coder")
    # the coder's file is committed on the branch
    show = subprocess.run(
        ["git", "show", "HEAD:greeting.txt"],
        cwd=result.worktree,
        capture_output=True,
        text=True,
    )
    assert show.returncode == 0 and show.stdout == "hello\n"
    # the handoff is a separate artifact (outside the worktree) — not committed,
    # and the worktree is left clean (no untracked .handoff/).
    assert (config.handoff_dir / "round_01_coder_done.md").is_file()
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=result.worktree,
        capture_output=True,
        text=True,
    ).stdout
    assert porcelain == ""


def test_develop_fails_without_handoff_and_does_not_commit(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fake_coder(
        monkeypatch, config, write_handoff=False, write_source=True, ok=True
    )
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert not result.handoff_present
    assert "no coder handoff" in result.message
    # failure must NOT promote partial work to the branch...
    assert result.commits == []
    assert _commit_count_since_base(result) == 0
    # ...but the changes remain in the worktree for inspection
    assert (result.worktree / "greeting.txt").is_file()


def test_develop_fails_when_turn_errors_and_does_not_commit(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fake_coder(
        monkeypatch, config, write_handoff=True, write_source=True, ok=False
    )
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert "coder turn failed" in result.message
    assert result.commits == []
    assert _commit_count_since_base(result) == 0


def test_develop_fails_with_no_changes(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # handoff but no source change -> no commit -> failed
    _install_fake_coder(
        monkeypatch, config, write_handoff=True, write_source=False, ok=True
    )
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert "no commit" in result.message
    assert _commit_count_since_base(result) == 0


def test_develop_rejects_unsupported_coder(tmp_git_repo: Path, tmp_path: Path) -> None:
    cfg = DevelopConfig(
        repo=tmp_git_repo,
        description="x",
        work_dir=tmp_path / "work",
        coder="codex",
        claude_config_dir=tmp_path / "fake-claude",
    )
    with pytest.raises(ValueError, match="unsupported coder"):
        develop_mod.develop(cfg)
