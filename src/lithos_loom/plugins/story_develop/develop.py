"""``develop()`` core — the full implement → review → fix → approve loop.

    worktree
      -> start coder (RW) + reviewer (RO) containers, both long-lived
      -> round 1: coder implements, commit, test gate, reviewer reviews
      -> round N: coder fixes (resume), commit, gate, reviewer re-reviews (resume)
      -> stop when the reviewer passes (approved) or max_rounds is hit
      -> tear both containers down; leave the branch + a conversation log.

The test gate (T4) runs each round commit's tree in a fresh throwaway container
— an agent-free check on the coder's self-reported test results. By default it
is recorded but non-blocking; with ``block_on_red`` a red gate prevents
approval and its output is fed to the coder next round.

The two agents keep their sessions **across rounds** (ADR 0002): each round is a
fresh ``docker exec`` that resumes the on-disk session, so the coder remembers
what it tried and the reviewer remembers what it objected to — the whole point
of the conversational model over Ralph++'s fire-and-forget loop.

The side-effecting bits (container start/exec/stop) live in :mod:`containers` /
:mod:`turns` so this orchestration is unit-testable by monkeypatching them.
Stall / dispute / cost guards are deliberately *not* here — they arrive with T7;
T3's only loop bound is ``max_rounds``.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ...runner import detection, git, worktree
from . import containers, handoff, test_gate
from .config import (
    CLAUDE_AUTH_FILES,
    HANDOFF_DIRNAME,
    DevelopConfig,
    is_valid_reviewer_name,
)
from .handoff import Finding, HandoffError, ReviewHandoff
from .test_gate import GateResult
from .turns import run_turn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewOutcome:
    """The result of a single reviewer pass in one round."""

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

    status: str  # "approved" | "max_rounds" | "failed"
    run_id: str
    worktree: Path
    branch: str
    base_sha: str
    commits: list[str]
    rounds: int
    handoff_present: bool
    coder_cost_usd: float
    review_cost_usd: float
    message: str
    review: ReviewOutcome | None = None  # the final round's review
    test_gate: GateResult | None = None  # the latest round's gate (T4)
    conversation_log: Path | None = None

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    @property
    def succeeded(self) -> bool:
        """True only when the reviewer approved (drives the CLI exit code)."""
        return self.status == "approved"

    @property
    def total_cost_usd(self) -> float:
        return self.coder_cost_usd + self.review_cost_usd


# --- prompt / rendering helpers --------------------------------------------


def _render(template: str, **values: str) -> str:
    """Placeholder substitution that is safe against braces in the values."""
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def _render_findings(findings: list[Finding]) -> str:
    """Render a reviewer's findings as a compact block for the coder's prompt."""
    if not findings:
        return "(no structured findings were listed)"
    lines: list[str] = []
    for f in findings:
        files = ", ".join(f.files) if f.files else "(unspecified)"
        lines.append(f"- [{f.finding_id}] severity={f.severity} status={f.status}")
        lines.append(f"  files: {files}")
        if f.rationale:
            lines.append(f"  rationale: {f.rationale}")
    return "\n".join(lines)


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


def _coder_summary(config: DevelopConfig, round_no: int) -> str:
    """Best-effort read of the coder's round-*round_no* summary (seeds review)."""
    path = config.handoff_dir / handoff.coder_handoff_name(round_no)
    try:
        return handoff.parse_review_handoff(
            path.read_text(encoding="utf-8")
        ).summary or ("(the coder wrote no summary)")
    except (HandoffError, OSError):
        return "(coder summary unavailable)"


# --- test gate (T4) ---------------------------------------------------------


def _resolve_gate_command(config: DevelopConfig, wt: Path) -> str | None:
    """Pick the test command the gate will run, or ``None`` to skip the gate.

    An explicit ``test_command`` is trusted as-is; otherwise candidates are
    auto-detected from the worktree and the first one whose tool exists in the
    container image wins (the image may lack e.g. ``make`` — see
    :mod:`...runner.detection`).
    """
    if not config.test_gate:
        return None
    if config.test_command:
        return config.test_command
    candidates = detection.detect_test_commands(wt)
    if not candidates:
        logger.info(
            "story-develop %s: test gate skipped (no test command detected)",
            config.run_id,
        )
        return None
    tools = list(dict.fromkeys(c.split()[0] for c in candidates))
    chosen = test_gate.select_command(
        candidates, test_gate.probe_tools(config.image, tools)
    )
    if chosen is None:
        logger.warning(
            "story-develop %s: test gate skipped — none of %s runnable in %s; "
            "set --test-command explicitly",
            config.run_id,
            candidates,
            config.image,
        )
    return chosen


def _run_gate(
    config: DevelopConfig, wt: Path, sha: str, round_no: int, command: str
) -> GateResult | None:
    """Run the gate for one round commit. Infra errors skip the gate (with a
    warning) rather than failing the run — the gate is an independent check,
    not a dependency."""
    round_dir = config.gate_dir / f"round_{round_no:02d}"
    name = containers.container_name(config.run_id, f"gate-r{round_no}")
    try:
        test_gate.export_tree(wt, sha, round_dir / "tree")
        cache = config.gate_dir / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        gate_cmd = test_gate.build_gate_command(
            name=name,
            image=config.image,
            tree=round_dir / "tree",
            cache_dir=cache,
            command=command,
        )
        result = test_gate.run_gate_container(
            gate_cmd, name=name, command=command, timeout=config.test_timeout
        )
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "story-develop %s: round %d test gate errored (skipping): %s",
            config.run_id,
            round_no,
            exc,
        )
        return None
    (round_dir / "output.txt").write_text(
        f"$ {result.command}\nexit: {result.exit_code} ({result.verdict})\n\n"
        f"{result.output_tail}\n",
        encoding="utf-8",
    )
    logger.info(
        "story-develop %s: round %d test gate %s (`%s`, exit %d)",
        config.run_id,
        round_no,
        result.verdict,
        result.command,
        result.exit_code,
    )
    return result


def _gate_note(gate: GateResult | None) -> str:
    """A prompt section describing a red gate (empty when green/absent)."""
    if gate is None or gate.passed:
        return ""
    how = "timed out" if gate.timed_out else f"exit {gate.exit_code}"
    return (
        "\n## Independent test gate (FAILED)\n\n"
        f"The orchestrator independently ran `{gate.command}` against your last "
        f"commit in a clean container and it failed ({how}). This result is "
        "authoritative — fix the failures regardless of how the tests behaved "
        "in your own environment. Output tail:\n\n"
        "```\n" + gate.output_tail + "\n```\n"
    )


# --- per-turn drivers -------------------------------------------------------


def _review_turn(
    config: DevelopConfig,
    *,
    container: str,
    session_id: str,
    round_no: int,
    resume: bool,
    prompt: str,
    timeout: int,
) -> ReviewOutcome:
    """Run one reviewer turn against an already-running reviewer container.

    Re-prompts the *same* session once if the handoff is malformed. The handoff
    is only authoritative if the turn that produced it SUCCEEDED (clean exit +
    structured result) — a failed turn that happens to leave a parseable file is
    rejected, preserving the exit-code contract (ADR 0002).
    """
    reviewer = config.reviewer
    review_file = handoff.reviewer_handoff_name(round_no, reviewer)
    review_path = config.handoff_dir / review_file
    cost = 0.0
    parsed: ReviewHandoff | None = None
    err: str | None = "reviewer did not run"

    turn = run_turn(
        container=container,
        prompt=prompt,
        session_id=session_id,
        resume=resume,
        timeout=timeout,
    )
    cost += turn.cost_usd
    if not turn.succeeded:
        err = f"reviewer turn failed (exit {turn.exit_code})"
    else:
        parsed, err = _read_review(review_path)
        if parsed is None:
            correction = (
                f"Your review at .handoff/{review_file} was not valid: {err}. "
                f"Please rewrite only that file per /workspace/.handoff/FORMAT.md."
            )
            retry = run_turn(
                container=container,
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

    if parsed is None:
        logger.warning(
            "story-develop %s: round %d reviewer handoff invalid: %s",
            config.run_id,
            round_no,
            err,
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


# --- orchestration ----------------------------------------------------------


def develop(
    config: DevelopConfig,
    *,
    coder_timeout: int = 3600,
    reviewer_timeout: int = 3600,
) -> DevelopResult:
    """Run the T3 develop loop and return a result.

    The worktree, per-run state, and conversation log are preserved on exit
    (approved, max_rounds, or failed) for inspection; only the containers are
    torn down.
    """
    if config.coder != "claude" or config.reviewer_tool != "claude":
        raise ValueError(
            "unsupported tool for T3: only 'claude' (codex arrives with T5/T6)"
        )
    if not is_valid_reviewer_name(config.reviewer):
        raise ValueError(
            f"invalid reviewer name {config.reviewer!r}: must be lowercase "
            "alphanumerics + hyphens (e.g. 'code-quality')"
        )
    if config.max_rounds < 1:
        raise ValueError(f"max_rounds must be >= 1 (got {config.max_rounds})")

    config.coder_config_dir.mkdir(parents=True, exist_ok=True)
    config.reviewer_config_dir(config.reviewer).mkdir(parents=True, exist_ok=True)
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

    coder_name, coder_cmd = _build_run_cmd(
        config,
        agent="coder",
        config_dir=config.coder_config_dir,
        wt=wt,
        read_only=False,
    )
    reviewer_name, reviewer_cmd = _build_run_cmd(
        config,
        agent=f"review-{config.reviewer}",
        config_dir=config.reviewer_config_dir(config.reviewer),
        wt=wt,
        read_only=True,
    )
    coder_session = str(uuid.uuid4())
    reviewer_session = str(uuid.uuid4())

    status = "failed"
    failure_reason = "no rounds ran"
    final_review: ReviewOutcome | None = None
    gate: GateResult | None = None
    gate_command = _resolve_gate_command(config, wt)
    rounds_completed = 0
    coder_cost = 0.0
    review_cost = 0.0

    try:
        containers.start_container(coder_cmd)
        containers.start_container(reviewer_cmd)
        logger.info(
            "story-develop %s: coder %s + reviewer %s started",
            config.run_id,
            coder_name,
            reviewer_name,
        )

        for round_no in range(1, config.max_rounds + 1):
            rounds_completed = round_no
            # --- coder turn ------------------------------------------------
            if round_no == 1:
                coder_prompt = _render(
                    handoff.load_prompt("coder_init.md"),
                    description=config.description,
                    handoff_file=handoff.coder_handoff_name(1),
                )
                coder_resume = False
            else:
                assert final_review is not None  # set by the prior round's review
                coder_prompt = _render(
                    handoff.load_prompt("coder_fix.md"),
                    round_no=str(round_no),
                    reviewer=config.reviewer,
                    acceptance_criteria=config.effective_acceptance_criteria,
                    findings=_render_findings(final_review.findings),
                    test_gate_note=_gate_note(gate),
                    review_file=handoff.reviewer_handoff_name(
                        round_no - 1, config.reviewer
                    ),
                    handoff_file=handoff.coder_handoff_name(round_no),
                )
                coder_resume = True

            coder_turn = run_turn(
                container=coder_name,
                prompt=coder_prompt,
                session_id=coder_session,
                resume=coder_resume,
                timeout=coder_timeout,
            )
            coder_cost += coder_turn.cost_usd
            done_present = (
                config.handoff_dir / handoff.coder_handoff_name(round_no)
            ).is_file()
            if not (coder_turn.succeeded and done_present):
                reasons = []
                if not coder_turn.succeeded:
                    reasons.append(f"coder turn failed (exit {coder_turn.exit_code})")
                if not done_present:
                    reasons.append("no coder handoff file")
                failure_reason = f"round {round_no}: " + "; ".join(reasons)
                status = "failed"
                break

            new_commit = git.commit_all(
                wt,
                f"story-develop r{round_no}: {config.description}",
                exclude=[HANDOFF_DIRNAME],
            )
            if round_no == 1 and new_commit is None:
                failure_reason = "round 1: coder produced no commit"
                status = "failed"
                break

            # --- test gate (only when there is a new commit to gate) --------
            if gate_command is not None and new_commit is not None:
                # Overwrite unconditionally: on a gate infra error this clears
                # to None rather than letting a PRIOR commit's result (e.g. a
                # stale RED under block_on_red) stand in for this commit. A
                # round with no new commit keeps the prior result — the tree is
                # unchanged, so it still describes HEAD.
                gate = _run_gate(config, wt, new_commit, round_no, gate_command)

            # --- reviewer turn --------------------------------------------
            if round_no == 1:
                review_prompt = _render(
                    handoff.load_prompt("reviewer_round.md"),
                    reviewer=config.reviewer,
                    acceptance_criteria=config.effective_acceptance_criteria,
                    coder_summary=_coder_summary(config, 1),
                    review_file=handoff.reviewer_handoff_name(1, config.reviewer),
                )
                review_resume = False
            else:
                review_prompt = _render(
                    handoff.load_prompt("reviewer_rereview.md"),
                    reviewer=config.reviewer,
                    round_no=str(round_no),
                    acceptance_criteria=config.effective_acceptance_criteria,
                    base_sha=base[:12],
                    coder_handoff_file=handoff.coder_handoff_name(round_no),
                    review_file=handoff.reviewer_handoff_name(
                        round_no, config.reviewer
                    ),
                )
                review_resume = True

            review = _review_turn(
                config,
                container=reviewer_name,
                session_id=reviewer_session,
                round_no=round_no,
                resume=review_resume,
                prompt=review_prompt,
                timeout=reviewer_timeout,
            )
            review_cost += review.cost_usd
            final_review = review

            if review.status == "invalid":
                failure_reason = f"round {round_no}: reviewer handoff invalid"
                status = "failed"
                break
            if review.passed:
                if gate is not None and not gate.passed and config.block_on_red:
                    logger.info(
                        "story-develop %s: round %d review passed but test gate "
                        "is RED and --block-on-red is set; continuing",
                        config.run_id,
                        round_no,
                    )
                else:
                    status = "approved"
                    break
            # otherwise: loop to the next round (if any remain)
        else:
            # loop exhausted without an approval / failure break
            status = "max_rounds"
    finally:
        containers.stop_container(coder_name)
        containers.stop_container(reviewer_name)

    commits = git.commits_since(wt, base)
    handoff_present = (config.handoff_dir / handoff.coder_handoff_name(1)).is_file()

    log_path = config.run_dir / "conversation.md"
    log_path.write_text(
        handoff.conversation_log(config.handoff_dir, rounds_completed, config.reviewer),
        encoding="utf-8",
    )

    total = coder_cost + review_cost
    gate_part = f"; test gate {gate.verdict} (`{gate.command}`)" if gate else ""
    if status == "approved":
        assert final_review is not None
        sev = f" (max {final_review.max_severity})" if final_review.max_severity else ""
        message = (
            f"approved by [{final_review.reviewer}] in {rounds_completed} "
            f"round(s){sev}{gate_part}; {len(commits)} commit(s) on {branch}; "
            f"cost ${total:.4f}"
        )
    elif status == "max_rounds":
        assert final_review is not None
        sev = f" (max {final_review.max_severity})" if final_review.max_severity else ""
        message = (
            f"NOT approved after {rounds_completed} round(s) (max_rounds); "
            f"last review[{final_review.reviewer}]={final_review.status}{sev}"
            f"{gate_part}; {len(commits)} commit(s) on {branch}; cost ${total:.4f}"
        )
    else:  # failed
        message = f"{failure_reason}{gate_part}; {len(commits)} commit(s) on {branch}"

    return DevelopResult(
        status=status,
        run_id=config.run_id,
        worktree=wt,
        branch=branch,
        base_sha=base,
        commits=commits,
        rounds=rounds_completed,
        handoff_present=handoff_present,
        coder_cost_usd=coder_cost,
        review_cost_usd=review_cost,
        message=message,
        review=final_review,
        test_gate=gate,
        conversation_log=log_path,
    )
