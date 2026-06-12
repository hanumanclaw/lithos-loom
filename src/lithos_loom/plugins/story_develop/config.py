"""Resolved configuration + paths for a single ``story-develop`` run.

T1 carries only what the walking skeleton needs (one coder, no reviewers). Later
slices extend :class:`DevelopConfig` with reviewers, thresholds, fallback chains,
etc. — see ``docs/prd/story-develop.md``.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

# A reviewer name becomes a Docker container name, a host dir, and a handoff
# filename, so it must be a safe slug (lowercase alphanumerics + hyphens,
# starting alphanumeric). This rejects spaces ("code quality") and path
# separators ("security/appsec") before they create invalid names / nested dirs.
_REVIEWER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def is_valid_reviewer_name(name: str) -> bool:
    """True if *name* is a safe slug for container / path / filename use."""
    return bool(_REVIEWER_NAME_RE.fullmatch(name))


# Image + container constants (ralph-sandbox; see ADR 0002 / feasibility gate).
DEFAULT_CODER_TOOL = "claude"
DEFAULT_REVIEWER_TOOL = "claude"
DEFAULT_REVIEWER_NAME = "code-quality"
DEFAULT_BLOCK_THRESHOLD = "major"  # findings below this don't block (see handoff.py)
DEFAULT_MAX_ROUNDS = 5  # T3 loop bound; stall/dispute/cost guards arrive with T7
DEFAULT_TEST_TIMEOUT = 900  # seconds for one test-gate container run (T4)
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
    # T2: a single reviewer. Multi-reviewer config arrives with T6.
    reviewer: str = DEFAULT_REVIEWER_NAME
    reviewer_tool: str = DEFAULT_REVIEWER_TOOL
    block_threshold: str = DEFAULT_BLOCK_THRESHOLD
    # T3: how many implement→review→fix rounds before we stop unapproved.
    max_rounds: int = DEFAULT_MAX_ROUNDS
    # T4: objective test gate per round commit (throwaway container).
    test_gate: bool = True  # auto-skips when no test command is detected
    test_command: str | None = None  # explicit override beats detection
    block_on_red: bool = False  # red gate prevents approval + feeds the coder
    test_timeout: int = DEFAULT_TEST_TIMEOUT
    acceptance_criteria: str | None = None
    run_id: str = field(default_factory=_short_run_id)
    # Host path to the operator's claude config dir (source of the auth file).
    claude_config_dir: Path = field(default_factory=lambda: Path.home() / ".claude")

    @property
    def effective_acceptance_criteria(self) -> str:
        """The "definition of done" shown to the reviewer.

        T2 falls back to the task description; an explicit ``--acceptance-criteria``
        surface is wired in T8/T12.
        """
        return self.acceptance_criteria or self.description

    @property
    def run_dir(self) -> Path:
        """Per-run state root: ``<work_dir>/<run_id>``."""
        return self.work_dir / self.run_id

    @property
    def coder_config_dir(self) -> Path:
        """Per-run coder config dir (CLAUDE_CONFIG_DIR target; holds transcript)."""
        return self.run_dir / "agents" / "coder" / "claude_config"

    def reviewer_config_dir(self, name: str) -> Path:
        """Per-run, per-reviewer config dir (its own CLAUDE_CONFIG_DIR / transcript)."""
        return self.run_dir / "agents" / f"review-{name}" / "claude_config"

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
    def gate_dir(self) -> Path:
        """Per-run root for test-gate state (exported trees, output, cache)."""
        return self.run_dir / "test_gate"

    @property
    def operator_skills_dir(self) -> Path | None:
        """Operator's ``~/.claude/skills`` if present (mounted read-only).

        Restores the feasibility-gate G2 behaviour: operator-installed skills
        are available to the agent inside the per-run ``CLAUDE_CONFIG_DIR``.
        """
        skills = self.claude_config_dir / "skills"
        return skills if skills.is_dir() else None
