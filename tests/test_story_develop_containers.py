"""Unit tests for the pure docker command builders."""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import containers


def _run_cmd(**over) -> list[str]:
    kwargs: dict = dict(
        name="loom-develop-ab12cd34-coder",
        image="ralph-sandbox:latest",
        worktree=Path("/work/run/worktree/branch"),
        config_dir=Path("/work/run/agents/coder/claude_config"),
        handoff_dir=Path("/work/run/handoff"),
        auth_source_dir=Path("/home/u/.claude"),
        auth_files=[".credentials.json"],
    )
    kwargs.update(over)
    return containers.build_run_command(**kwargs)


def test_run_command_hardened_profile_and_mounts() -> None:
    cmd = _run_cmd()
    assert cmd[:3] == ["docker", "run", "-d"]
    assert "--rm" in cmd and "--init" in cmd
    # hardened
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges:true" in cmd
    # worktree RW, handoff dir OUTSIDE the worktree, config dir, single auth file
    assert "/work/run/worktree/branch:/workspace" in cmd
    assert "/work/run/handoff:/workspace/.handoff" in cmd
    assert "/work/run/agents/coder/claude_config:/claude_config" in cmd
    assert "/home/u/.claude/.credentials.json:/claude_config/.credentials.json" in cmd
    assert "CLAUDE_CONFIG_DIR=/claude_config" in cmd
    # idle entrypoint with the image and arg trailing
    assert cmd[-4:] == ["--entrypoint", "sleep", "ralph-sandbox:latest", "infinity"]


def test_run_command_mounts_skills_read_only_when_present() -> None:
    cmd = _run_cmd(skills_dir=Path("/home/u/.claude/skills"))
    assert "/home/u/.claude/skills:/claude_config/skills:ro" in cmd


def test_run_command_omits_skills_when_absent() -> None:
    cmd = _run_cmd(skills_dir=None)
    assert not any(a.endswith(":/claude_config/skills:ro") for a in cmd)


def test_run_command_readonly_worktree() -> None:
    cmd = _run_cmd(read_only_worktree=True)
    assert "/work/run/worktree/branch:/workspace:ro" in cmd


def test_run_command_multiple_auth_files() -> None:
    cmd = _run_cmd(auth_files=[".credentials.json", ".claude.json"])
    assert "/home/u/.claude/.claude.json:/claude_config/.claude.json" in cmd


def test_run_command_no_auth_files() -> None:
    cmd = _run_cmd(auth_files=[])
    assert not any(":/claude_config/." in a for a in cmd)


def test_exec_command_first_turn_uses_session_id() -> None:
    cmd = containers.build_exec_command(
        name="c", tool="claude", prompt="do it", session_id="sid-1"
    )
    assert cmd[:5] == ["docker", "exec", "-w", "/workspace", "c"]
    assert "claude" in cmd
    assert cmd[cmd.index("--session-id") + 1] == "sid-1"
    assert "-p" in cmd and "--dangerously-skip-permissions" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[-1] == "do it"  # prompt passed as a single argv element


def test_exec_command_resume_uses_resume_flag() -> None:
    cmd = containers.build_exec_command(
        name="c", tool="claude", prompt="p", session_id="sid-1", resume=True
    )
    assert "--resume" in cmd and "--session-id" not in cmd
    assert cmd[cmd.index("--resume") + 1] == "sid-1"


def test_exec_command_rejects_non_claude_tool() -> None:
    with pytest.raises(ValueError):
        containers.build_exec_command(
            name="c", tool="codex", prompt="p", session_id="s"
        )


def test_container_name() -> None:
    assert (
        containers.container_name("ab12cd34", "coder") == "loom-develop-ab12cd34-coder"
    )
