"""``develop()`` core — T1 walking skeleton.

One coder, one turn, no review loop:

    worktree -> idle coder container -> one coder turn -> commit -> teardown.

The side-effecting bits (container start/exec/stop) live in :mod:`containers` /
:mod:`turns` so this orchestration is unit-testable by monkeypatching them.
Later slices (T2/T3) grow the round loop around the same skeleton.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from ...runner import git, worktree
from . import containers, handoff
from .config import CLAUDE_AUTH_FILES, HANDOFF_DIRNAME, DevelopConfig
from .turns import CoderTurnResult, run_coder_turn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DevelopResult:
    """Outcome of a ``develop()`` run."""

    status: str  # "succeeded" | "failed"
    run_id: str
    worktree: Path
    branch: str
    base_sha: str
    commits: list[str]
    handoff_present: bool
    coder_cost_usd: float
    message: str

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


def _render(template: str, **values: str) -> str:
    """Placeholder substitution that is safe against braces in the values."""
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def develop(config: DevelopConfig, *, coder_timeout: int = 3600) -> DevelopResult:
    """Run the T1 cycle and return a :class:`DevelopResult`.

    The worktree and per-run state are preserved on exit (success or failure)
    for inspection; only the container is torn down.
    """
    if config.coder != "claude":  # codex/other coders arrive with T5/T6
        raise ValueError(
            f"unsupported coder tool for T1: {config.coder!r} (only 'claude')"
        )
    config.coder_config_dir.mkdir(parents=True, exist_ok=True)
    config.worktree_parent.mkdir(parents=True, exist_ok=True)
    handoff.seed_handoff_dir(config.handoff_dir)

    wt = worktree.create(
        config.repo,
        config.base_branch,
        config.description,
        parent=config.worktree_parent,
    )
    branch = wt.name
    base = git.base_sha(wt)
    logger.info("story-develop %s: worktree %s (branch %s)", config.run_id, wt, branch)

    name = containers.container_name(config.run_id, "coder")
    run_cmd = containers.build_run_command(
        name=name,
        image=config.image,
        worktree=wt,
        config_dir=config.coder_config_dir,
        handoff_dir=config.handoff_dir,
        auth_source_dir=config.claude_config_dir,
        auth_files=containers.resolve_auth_files(config, CLAUDE_AUTH_FILES),
        skills_dir=config.operator_skills_dir,
    )

    turn: CoderTurnResult | None = None
    try:
        containers.start_container(run_cmd)
        logger.info("story-develop %s: coder container %s started", config.run_id, name)
        handoff_file = handoff.coder_handoff_name(1)
        prompt = _render(
            handoff.load_prompt("coder_init.md"),
            description=config.description,
            handoff_file=handoff_file,
        )
        turn = run_coder_turn(
            container=name,
            prompt=prompt,
            session_id=str(uuid.uuid4()),
            timeout=coder_timeout,
        )
    finally:
        containers.stop_container(name)

    handoff_path = config.handoff_dir / handoff.coder_handoff_name(1)
    handoff_present = handoff_path.is_file()

    # Only promote work to the branch when the turn succeeded AND signed off.
    # On failure the worktree changes are left uncommitted (inspectable, not
    # promoted). The .handoff/ exclude is belt-and-suspenders — the handoff dir
    # already lives outside the worktree (PRD #9).
    commits: list[str] = []
    if (turn and turn.succeeded) and handoff_present:
        git.commit_all(
            wt, f"story-develop: {config.description}", exclude=[HANDOFF_DIRNAME]
        )
        commits = git.commits_since(wt, base)

    ok = bool(turn and turn.succeeded) and handoff_present and bool(commits)
    if ok:
        message = (
            f"coder produced {len(commits)} commit(s) on {branch}; "
            f"handoff written; cost ${turn.cost_usd:.4f}"
        )
    else:
        reasons = []
        if not (turn and turn.succeeded):
            code = turn.exit_code if turn else "n/a"
            reasons.append(f"coder turn failed (exit {code})")
        if not handoff_present:
            reasons.append("no coder handoff file")
        if not commits:
            reasons.append("no commit produced")
        message = "; ".join(reasons)

    return DevelopResult(
        status="succeeded" if ok else "failed",
        run_id=config.run_id,
        worktree=wt,
        branch=branch,
        base_sha=base,
        commits=commits,
        handoff_present=handoff_present,
        coder_cost_usd=turn.cost_usd if turn else 0.0,
        message=message,
    )
