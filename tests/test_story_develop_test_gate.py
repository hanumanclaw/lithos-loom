"""Tests for the objective test gate: pure builders + real ``export_tree``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import test_gate as test_gate_mod
from lithos_loom.plugins.story_develop.test_gate import (
    GateResult,
    build_gate_command,
    build_probe_command,
    export_tree,
    probe_tools,
    run_gate_container,
    select_command,
)

# --- select_command ----------------------------------------------------------


def test_select_first_available() -> None:
    cands = ["make test", "uv run pytest"]
    assert select_command(cands, ["make", "uv"]) == "make test"


def test_select_falls_back_when_tool_missing() -> None:
    cands = ["make test", "uv run pytest"]
    assert select_command(cands, ["uv"]) == "uv run pytest"


def test_select_none_when_nothing_runnable() -> None:
    assert select_command(["make test"], []) is None


def test_select_empty_candidates() -> None:
    assert select_command([], ["make"]) is None


# --- command builders --------------------------------------------------------


def test_probe_command_shape() -> None:
    cmd = build_probe_command(image="img:latest", tools=["make", "uv"])
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "img:latest" in cmd
    script = cmd[-1]
    assert "command -v make" in script and "command -v uv" in script
    # hardened profile
    assert "ALL" in cmd and "no-new-privileges:true" in cmd


def test_gate_command_shape(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    cache = tmp_path / "cache"
    cmd = build_gate_command(
        name="loom-develop-x-gate-r1",
        image="img:latest",
        tree=tree,
        cache_dir=cache,
        command="uv run pytest",
    )
    assert cmd[:4] == ["docker", "run", "--rm", "--init"]
    assert "--name" in cmd and "loom-develop-x-gate-r1" in cmd
    assert f"{tree}:/workspace" in cmd
    assert f"{cache}:/gate_cache" in cmd
    assert "UV_CACHE_DIR=/gate_cache/uv" in cmd
    assert cmd[-1] == "uv run pytest"  # runs via sh -c
    assert cmd[-2] == "-c"
    # hardened profile + no agent config mounts (agent-free by construction)
    assert "ALL" in cmd and "no-new-privileges:true" in cmd
    assert not any("claude_config" in str(a) for a in cmd)


# --- GateResult --------------------------------------------------------------


def test_gate_result_verdicts() -> None:
    green = GateResult(command="x", exit_code=0, passed=True, output_tail="")
    red = GateResult(command="x", exit_code=1, passed=False, output_tail="boom")
    timeout = GateResult(command="x", exit_code=124, passed=False, output_tail="")
    assert green.verdict == "GREEN"
    assert red.verdict == "RED" and not red.timed_out
    assert timeout.verdict == "TIMEOUT" and timeout.timed_out


# --- infra-failure degradation ------------------------------------------------


def test_probe_tools_degrades_when_docker_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args, **kwargs):
        raise FileNotFoundError("docker not on PATH")

    monkeypatch.setattr(test_gate_mod.subprocess, "run", boom)
    assert probe_tools("img:latest", ["make", "uv"]) == []


def test_probe_tools_degrades_on_probe_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=120)

    monkeypatch.setattr(test_gate_mod.subprocess, "run", boom)
    assert probe_tools("img:latest", ["make"]) == []


def test_probe_tools_empty_tools_no_docker_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args, **kwargs):  # would fail the test if reached
        raise AssertionError("docker should not be invoked for an empty tool list")

    monkeypatch.setattr(test_gate_mod.subprocess, "run", boom)
    assert probe_tools("img:latest", []) == []


def test_timeout_cleanup_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    # First call (the gate run) times out; the docker rm -f cleanup then fails
    # too — callers must still get a TIMEOUT GateResult, not an exception.
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[0] if isinstance(cmd, list) else str(cmd))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd="docker", timeout=5)
        raise FileNotFoundError("docker vanished")

    monkeypatch.setattr(test_gate_mod.subprocess, "run", fake_run)
    result = run_gate_container(
        ["docker", "run", "..."], name="gate-x", command="pytest", timeout=5
    )
    assert result.timed_out and result.verdict == "TIMEOUT"
    assert len(calls) == 2  # gate run + attempted cleanup


# --- export_tree (real git) --------------------------------------------------


def test_export_tree_exports_commit_content_only(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    # committed file + an untracked file that must NOT be exported
    (tmp_git_repo / "src.py").write_text("print('hi')\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add src"], cwd=tmp_git_repo, check=True
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (tmp_git_repo / "untracked.txt").write_text("cruft\n")

    dest = tmp_path / "tree"
    export_tree(tmp_git_repo, sha, dest)

    assert (dest / "src.py").read_text() == "print('hi')\n"
    assert (dest / "README.md").is_file()
    assert not (dest / "untracked.txt").exists()  # only committed content
    assert not (dest / ".git").exists()  # no git metadata


def test_export_tree_bad_sha_raises(tmp_git_repo: Path, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="git archive"):
        export_tree(tmp_git_repo, "0" * 40, tmp_path / "tree")
