"""``develop()`` core — T2: coder turn + one reviewer pass (verdict only).

    worktree -> coder container -> commit -> reviewer container -> verdict.

Still a single round: the reviewer's findings are parsed and a verdict is
computed/printed, but there is no fix-and-re-review loop yet (that is T3). The
side-effecting bits (container start/exec/stop) live in :mod:`containers` /
:mod:`turns` so this orchestration is unit-testable by monkeypatching them.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ...runner import git, worktree
from . import containers, handoff
from .config import (
    CLAUDE_AUTH_FILES,
    HANDOFF_DIRNAME,
    DevelopConfig,
    is_valid_reviewer_name,
)
from .handoff import Finding, HandoffError, ReviewHandoff
from .turns import TurnResult, run_turn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewOutcome:
    """The result of the (single, T2) reviewer pass."""

    reviewer: str
    status: str  # "LGTM" | "FINDINGS" | "invalid"
    passed: bool  # by the configured block_threshold
    max_severity: str | None
    findings: list[Finding] = field(default_factory=list)
    cost_usd: float = 0.0

    @property
    def findings_count(self) -> int:
        return len(self.findings)


@dataclass(frozen=True)
class DevelopResult:
    """Outcome of a ``develop()`` run."""

    status: str  # "succeeded" | "failed"  (coder-based in T2; T3 gates on review)
    run_id: str
    worktree: Path
    branch: str
    base_sha: str
    commits: list[str]
    handoff_present: bool
    coder_cost_usd: float
    message: str
    review: ReviewOutcome | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def total_cost_usd(self) -> float:
        return self.coder_cost_usd + (self.review.cost_usd if self.review else 0.0)


def _render(template: str, **values: str) -> str:
    """Placeholder substitution that is safe against braces in the values."""
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def _build_run_cmd(
    config: DevelopConfig, *, agent: str, config_dir: Path, wt: Path, read_only: bool
) -> tuple[str, list[str]]:
    """Build (container_name, docker-run-argv) for an agent container."""
    name = containers.container_name(config.run_id, agent)
    cmd = containers.build_run_command(
        name=name,
        image=config.image,
        worktree=wt,
        config_dir=config_dir,
        handoff_dir=config.handoff_dir,
        auth_source_dir=config.claude_config_dir,
        auth_files=containers.resolve_auth_files(config, CLAUDE_AUTH_FILES),
        skills_dir=config.operator_skills_dir,
        read_only_worktree=read_only,
    )
    return name, cmd


def _read_review(path: Path) -> tuple[ReviewHandoff | None, str | None]:
    """Read + parse a reviewer handoff. Returns (handoff, error_message)."""
    if not path.is_file():
        return None, "no handoff file was written at the expected path"
    try:
        return handoff.parse_review_handoff(path.read_text(encoding="utf-8")), None
    except HandoffError as exc:
        return None, str(exc)


def _coder_summary(config: DevelopConfig) -> str:
    """Best-effort read of the coder's handoff summary, to seed the reviewer."""
    path = config.handoff_dir / handoff.coder_handoff_name(1)
    try:
        return handoff.parse_review_handoff(
            path.read_text(encoding="utf-8")
        ).summary or ("(the coder wrote no summary)")
    except (HandoffError, OSError):
        return "(coder summary unavailable)"


def _run_review(config: DevelopConfig, wt: Path, *, timeout: int) -> ReviewOutcome:
    """Run one reviewer pass: review the commit, parse the verdict, re-prompt once
    if the handoff is malformed."""
    reviewer = config.reviewer
    cfg_dir = config.reviewer_config_dir(reviewer)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    name, run_cmd = _build_run_cmd(
        config, agent=f"review-{reviewer}", config_dir=cfg_dir, wt=wt, read_only=True
    )
    review_file = handoff.reviewer_handoff_name(1, reviewer)
    review_path = config.handoff_dir / review_file
    session_id = str(uuid.uuid4())
    cost = 0.0
    parsed: ReviewHandoff | None = None
    err: str | None = "reviewer did not run"

    try:
        containers.start_container(run_cmd)
        logger.info(
            "story-develop %s: reviewer container %s started", config.run_id, name
        )
        prompt = _render(
            handoff.load_prompt("reviewer_round.md"),
            reviewer=reviewer,
            acceptance_criteria=config.effective_acceptance_criteria,
            coder_summary=_coder_summary(config),
            review_file=review_file,
        )
        turn = run_turn(
            container=name, prompt=prompt, session_id=session_id, timeout=timeout
        )
        cost += turn.cost_usd
        # A handoff is only authoritative if the turn that produced it SUCCEEDED
        # (clean exit + structured result). A failed turn that happens to leave a
        # parseable file must not be accepted — it defeats the exit-code contract.
        if not turn.succeeded:
            err = f"reviewer turn failed (exit {turn.exit_code})"
        else:
            parsed, err = _read_review(review_path)
            if parsed is None:
                # Malformed handoff: re-prompt the same reviewer (resume session).
                correction = (
                    f"Your review at .handoff/{review_file} was not valid: {err}. "
                    f"Please rewrite only that file per /workspace/.handoff/FORMAT.md."
                )
                retry = run_turn(
                    container=name,
                    prompt=correction,
                    session_id=session_id,
                    resume=True,
                    timeout=timeout,
                )
                cost += retry.cost_usd
                if retry.succeeded:
                    parsed, err = _read_review(review_path)
                else:
                    err = f"reviewer retry turn failed (exit {retry.exit_code})"
    finally:
        containers.stop_container(name)

    if parsed is None:
        logger.warning(
            "story-develop %s: reviewer handoff invalid: %s", config.run_id, err
        )
        return ReviewOutcome(
            reviewer=reviewer,
            status="invalid",
            passed=False,
            max_severity=None,
            cost_usd=cost,
        )
    return ReviewOutcome(
        reviewer=reviewer,
        status=parsed.status,
        passed=parsed.passes(config.block_threshold),
        max_severity=parsed.max_open_severity,
        findings=parsed.findings,
        cost_usd=cost,
    )


def develop(
    config: DevelopConfig, *, coder_timeout: int = 3600, reviewer_timeout: int = 3600
) -> DevelopResult:
    """Run the T2 cycle (coder + one reviewer pass) and return a result.

    The worktree and per-run state are preserved on exit (success or failure)
    for inspection; only the containers are torn down.
    """
    if config.coder != "claude" or config.reviewer_tool != "claude":
        raise ValueError(
            "unsupported tool for T2: only 'claude' (codex arrives with T5/T6)"
        )
    if not is_valid_reviewer_name(config.reviewer):
        raise ValueError(
            f"invalid reviewer name {config.reviewer!r}: must be lowercase "
            "alphanumerics + hyphens (e.g. 'code-quality')"
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

    # --- coder phase -------------------------------------------------------
    name, run_cmd = _build_run_cmd(
        config,
        agent="coder",
        config_dir=config.coder_config_dir,
        wt=wt,
        read_only=False,
    )
    turn: TurnResult | None = None
    try:
        containers.start_container(run_cmd)
        logger.info("story-develop %s: coder container %s started", config.run_id, name)
        prompt = _render(
            handoff.load_prompt("coder_init.md"),
            description=config.description,
            handoff_file=handoff.coder_handoff_name(1),
        )
        turn = run_turn(
            container=name,
            prompt=prompt,
            session_id=str(uuid.uuid4()),
            timeout=coder_timeout,
        )
    finally:
        containers.stop_container(name)

    handoff_present = (config.handoff_dir / handoff.coder_handoff_name(1)).is_file()
    commits: list[str] = []
    if (turn and turn.succeeded) and handoff_present:
        git.commit_all(
            wt, f"story-develop: {config.description}", exclude=[HANDOFF_DIRNAME]
        )
        commits = git.commits_since(wt, base)

    coder_ok = bool(turn and turn.succeeded) and handoff_present and bool(commits)

    # --- review phase (only when there is a commit to review) --------------
    review: ReviewOutcome | None = None
    if coder_ok:
        review = _run_review(config, wt, timeout=reviewer_timeout)

    # --- result ------------------------------------------------------------
    if coder_ok:
        assert review is not None  # always set when coder_ok
        sev = f" (max {review.max_severity})" if review.max_severity else ""
        gate = "passes" if review.passed else "blocks"
        total = (turn.cost_usd if turn else 0.0) + review.cost_usd
        message = (
            f"coder produced {len(commits)} commit(s) on {branch}; "
            f"review[{review.reviewer}]={review.status} ({gate}){sev}; "
            f"cost ${total:.4f}"
        )
    else:
        reasons = []
        if not (turn and turn.succeeded):
            reasons.append(
                f"coder turn failed (exit {turn.exit_code if turn else 'n/a'})"
            )
        if not handoff_present:
            reasons.append("no coder handoff file")
        if not commits:
            reasons.append("no commit produced")
        message = "; ".join(reasons)

    return DevelopResult(
        status="succeeded" if coder_ok else "failed",
        run_id=config.run_id,
        worktree=wt,
        branch=branch,
        base_sha=base,
        commits=commits,
        handoff_present=handoff_present,
        coder_cost_usd=turn.cost_usd if turn else 0.0,
        message=message,
        review=review,
    )
