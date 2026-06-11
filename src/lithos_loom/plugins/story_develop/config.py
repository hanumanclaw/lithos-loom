"""Resolved configuration + paths for a single ``story-develop`` run.

T1 carries only what the walking skeleton needs (one coder, no reviewers). Later
slices extend :class:`DevelopConfig` with reviewers, thresholds, fallback chains,
etc. — see ``docs/prd/story-develop.md``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path

# Image + container constants (ralph-sandbox; see ADR 0002 / feasibility gate).
DEFAULT_CODER_TOOL = "claude"
DEFAULT_IMAGE = "ralph-sandbox:latest"
WORKSPACE_MOUNT = "/workspace"
CLAUDE_CONFIG_MOUNT = "/claude_config"
# The single auth file bind-mounted from the operator's real config (RW, so the
# OAuth token refresh propagates) — never the whole ~/.claude, and NOT
# ``.claude.json`` (that is mutable user state, not auth; mounting the real one
# RW would let the container pollute the operator's live config). See the PRD
# "Run-state & session durability" section.
CLAUDE_AUTH_FILES = (".credentials.json",)
HANDOFF_DIRNAME = ".handoff"


def _short_run_id() -> str:
    """8 hex chars; unique enough to namespace a run's tmux/containers/state."""
    return secrets.token_hex(4)


@dataclass(frozen=True)
class DevelopConfig:
    """Everything ``develop()`` needs for one run.

    Paths under ``work_dir`` are derived lazily so the dataclass stays a plain
    value object: ``run_dir``/``coder_config_dir``/``worktree_parent``.
    """

    repo: Path
    description: str
    work_dir: Path
    coder: str = DEFAULT_CODER_TOOL
    image: str = DEFAULT_IMAGE
    base_branch: str = "main"
    run_id: str = field(default_factory=_short_run_id)
    # Host path to the operator's claude config dir (source of the auth file).
    claude_config_dir: Path = field(default_factory=lambda: Path.home() / ".claude")

    @property
    def run_dir(self) -> Path:
        """Per-run state root: ``<work_dir>/<run_id>``."""
        return self.work_dir / self.run_id

    @property
    def coder_config_dir(self) -> Path:
        """Per-run coder config dir (CLAUDE_CONFIG_DIR target; holds transcript)."""
        return self.run_dir / "agents" / "coder" / "claude_config"

    @property
    def worktree_parent(self) -> Path:
        """Where the run's worktree directory is created."""
        return self.run_dir / "worktree"

    @property
    def handoff_dir(self) -> Path:
        """Per-run handoff dir, mounted into the container at ``/workspace/.handoff``.

        Lives *outside* the git worktree so the worktree stays clean (the
        handoff is a separate artifact, not part of the deliverable branch).
        """
        return self.run_dir / "handoff"

    @property
    def operator_skills_dir(self) -> Path | None:
        """Operator's ``~/.claude/skills`` if present (mounted read-only).

        Restores the feasibility-gate G2 behaviour: operator-installed skills
        are available to the agent inside the per-run ``CLAUDE_CONFIG_DIR``.
        """
        skills = self.claude_config_dir / "skills"
        return skills if skills.is_dir() else None
