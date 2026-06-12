"""Orchestration tests for ``develop()`` (T3: implement → review → fix loop).

Real git/worktree against a temp repo; the containers + turns are monkeypatched
so no Docker or agent is needed. A single fake ``run_turn`` plays both roles and
all rounds, branching on the container name and parsing the round number out of
the prompt, and writing the appropriate handoff files. Reviewer behaviour per
round is scripted via the ``reviews`` list.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import containers, handoff
from lithos_loom.plugins.story_develop import develop as develop_mod
from lithos_loom.plugins.story_develop import test_gate as test_gate_mod
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.test_gate import GateResult
from lithos_loom.plugins.story_develop.turns import TurnResult

_LGTM = "## Status: LGTM\n## Summary\nLooks correct and complete.\n"
_FINDINGS_MAJOR = (
    "## Status: FINDINGS\n## Summary\nOne issue.\n## Findings\n"
    "- finding_id: f-001\n  severity: major\n  status: open\n"
    '  files: ["greeting.txt:1"]\n  rationale: needs work\n  coder_response:\n'
)
_FINDINGS_MINOR = _FINDINGS_MAJOR.replace("severity: major", "severity: minor")
_GARBAGE = "this is not a valid handoff at all\n"


def _worktree_from_run_cmd(run_cmd) -> Path:
    # the worktree mount is "<src>:/workspace" (coder) or "<src>:/workspace:ro"
    # (reviewer); the handoff mount "<src>:/workspace/.handoff" must not match.
    for i, arg in enumerate(run_cmd):
        if arg == "-v":
            parts = run_cmd[i + 1].split(":")
            if len(parts) >= 2 and parts[1] == "/workspace":
                return Path(parts[0])
    raise AssertionError("no /workspace mount in run cmd")


def _round_from(prompt: str, kind: str) -> int:
    m = re.search(rf"round_(\d+)_{kind}", prompt)
    assert m is not None, f"no {kind} round marker in prompt:\n{prompt}"
    return int(m.group(1))


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    config: DevelopConfig,
    *,
    coder_ok: bool = True,
    write_source: bool = True,
    write_coder_handoff: bool = True,
    reviews: list[dict] | None = None,
    source_rounds: set[int] | None = None,
    gates: list[bool | str] | None = None,
) -> dict:
    """Install fake container + turn + gate machinery.

    ``reviews`` scripts the reviewer per round (0-based; the last entry repeats
    for any further rounds). Each entry: ``{text, ok, retry_text, retry_ok}``.
    ``text`` is what the first review turn writes (None = write nothing);
    ``retry_text`` is what the malformed-handoff re-prompt writes.
    ``source_rounds`` limits which rounds the coder writes source in (None =
    every round). ``gates`` scripts the gate result per gate run (last repeats;
    ``True``/``False`` = green/red, ``"error"`` = simulated infra failure); the
    gate only actually runs when the config carries a ``test_command``.
    """
    reviews = reviews if reviews is not None else [{"text": _LGTM}]
    state: dict = {
        "stopped": [],
        "coder_calls": [],
        "coder_prompts": [],
        "review_calls": [],
        "gate_calls": [],
    }

    def fake_start(run_cmd) -> str:
        state["worktree"] = _worktree_from_run_cmd(run_cmd)
        return "cid"

    def _entry(rnd: int) -> dict:
        return reviews[min(rnd - 1, len(reviews) - 1)]

    def fake_run_turn(*, container, prompt, session_id, resume=False, timeout):
        wt = state["worktree"]
        if "-coder" in container:
            rnd = _round_from(prompt, "coder_done")
            state["coder_calls"].append((rnd, resume))
            state["coder_prompts"].append(prompt)
            if write_source and (source_rounds is None or rnd in source_rounds):
                (wt / "greeting.txt").write_text(f"hello round {rnd}\n")
            if write_coder_handoff:
                (config.handoff_dir / handoff.coder_handoff_name(rnd)).write_text(
                    f"## Status: LGTM\n## Summary\nRound {rnd}: did the work.\n"
                )
            return TurnResult(
                exit_code=0 if coder_ok else 1,
                succeeded=coder_ok,
                session_id=session_id,
                result_text="",
                cost_usd=0.01,
                raw={"is_error": not coder_ok},
                stderr="",
            )
        # reviewer turn
        rnd = _round_from(prompt, "review")
        is_correction = "was not valid" in prompt
        state["review_calls"].append((rnd, resume, is_correction))
        entry = _entry(rnd)
        review_path = config.handoff_dir / handoff.reviewer_handoff_name(
            rnd, config.reviewer
        )
        if is_correction:
            text, ok = entry.get("retry_text"), entry.get("retry_ok", True)
        else:
            text, ok = entry.get("text"), entry.get("ok", True)
        if text is not None:
            review_path.write_text(text)
        return TurnResult(
            exit_code=0 if ok else 1,
            succeeded=ok,
            session_id=session_id,
            result_text="",
            cost_usd=0.02,
            raw={"is_error": not ok},
            stderr="",
        )

    def fake_gate_container(gate_cmd, *, name, command, timeout):
        seq = gates if gates is not None else [True]
        val = seq[min(len(state["gate_calls"]), len(seq) - 1)]
        state["gate_calls"].append(name)
        if isinstance(val, str):  # "error" -> simulated infra failure
            raise RuntimeError("simulated gate infra failure")
        ok = val
        return GateResult(
            command=command,
            exit_code=0 if ok else 1,
            passed=ok,
            output_tail="2 failed, 10 passed" if not ok else "12 passed",
        )

    monkeypatch.setattr(containers, "start_container", fake_start)
    monkeypatch.setattr(
        containers, "stop_container", lambda name: state["stopped"].append(name)
    )
    monkeypatch.setattr(develop_mod, "run_turn", fake_run_turn)
    monkeypatch.setattr(test_gate_mod, "run_gate_container", fake_gate_container)
    return state


@pytest.fixture
def config(tmp_git_repo: Path, tmp_path: Path) -> DevelopConfig:
    cfg_dir = tmp_path / "fake-claude"
    cfg_dir.mkdir()
    return DevelopConfig(
        repo=tmp_git_repo,
        description="Add a greeting file",
        work_dir=tmp_path / "work",
        claude_config_dir=cfg_dir,
    )


def _commit_count_since_base(result) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", f"{result.base_sha}..HEAD"],
        cwd=result.worktree,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return int(out or 0)


# --- happy paths ------------------------------------------------------------


def test_approved_in_round_one_on_lgtm(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install_fakes(monkeypatch, config, reviews=[{"text": _LGTM}])
    result = develop_mod.develop(config)

    assert result.status == "approved"
    assert result.approved is True and result.succeeded is True
    assert result.rounds == 1
    assert len(result.commits) == 1
    assert result.review is not None and result.review.status == "LGTM"
    # both containers torn down
    assert any("-coder" in n for n in state["stopped"])
    assert any("-review-" in n for n in state["stopped"])
    # only one round of each agent; both started fresh (no resume)
    assert state["coder_calls"] == [(1, False)]
    assert state["review_calls"] == [(1, False, False)]
    # committed file present
    assert (
        subprocess.run(
            ["git", "show", "HEAD:greeting.txt"],
            cwd=result.worktree,
            capture_output=True,
            text=True,
        ).stdout
        == "hello round 1\n"
    )


def test_below_threshold_findings_pass_immediately(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(monkeypatch, config, reviews=[{"text": _FINDINGS_MINOR}])
    result = develop_mod.develop(config)
    assert result.status == "approved"  # minor < major threshold
    assert result.rounds == 1
    assert result.review is not None and result.review.max_severity == "minor"
    assert result.review.passed is True


def test_findings_then_fix_then_approved(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install_fakes(
        monkeypatch,
        config,
        reviews=[{"text": _FINDINGS_MAJOR}, {"text": _LGTM}],
    )
    result = develop_mod.develop(config)

    assert result.status == "approved"
    assert result.rounds == 2
    assert len(result.commits) == 2  # a commit per round (distinct content)
    # round 2 resumed BOTH sessions — the headline session-persistence proof
    assert state["coder_calls"] == [(1, False), (2, True)]
    assert state["review_calls"] == [(1, False, False), (2, True, False)]
    assert result.review is not None and result.review.status == "LGTM"


# --- bounded termination ----------------------------------------------------


def test_max_rounds_stops_unapproved(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    cfg = DevelopConfig(
        repo=config.repo,
        description=config.description,
        work_dir=config.work_dir,
        claude_config_dir=config.claude_config_dir,
        max_rounds=2,
    )
    _install_fakes(monkeypatch, cfg, reviews=[{"text": _FINDINGS_MAJOR}])
    result = develop_mod.develop(cfg)

    assert result.status == "max_rounds"
    assert result.succeeded is False
    assert result.rounds == 2
    assert len(result.commits) == 2
    assert result.review is not None and result.review.status == "FINDINGS"
    assert result.review.passed is False
    assert "max_rounds" in result.message


# --- malformed / failed review handling -------------------------------------


def test_malformed_review_is_reprompted_and_recovers(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install_fakes(
        monkeypatch, config, reviews=[{"text": _GARBAGE, "retry_text": _LGTM}]
    )
    result = develop_mod.develop(config)
    assert result.status == "approved"  # the re-prompt fixed it
    # the correction was a resumed turn on the same reviewer session
    assert state["review_calls"] == [(1, False, False), (1, True, True)]


def test_review_invalid_when_never_well_formed(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(
        monkeypatch, config, reviews=[{"text": _GARBAGE, "retry_text": _GARBAGE}]
    )
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert result.review is not None and result.review.status == "invalid"
    assert result.review.passed is False


def test_review_invalid_when_turn_fails_even_with_parseable_file(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # A failed reviewer turn that left a *valid* handoff must NOT be accepted.
    _install_fakes(monkeypatch, config, reviews=[{"text": _LGTM, "ok": False}])
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert result.review is not None and result.review.status == "invalid"


def test_review_invalid_when_retry_turn_fails(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(
        monkeypatch,
        config,
        reviews=[{"text": _GARBAGE, "retry_text": _LGTM, "retry_ok": False}],
    )
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert result.review is not None and result.review.status == "invalid"


# --- coder failure modes (no review, no commit) -----------------------------


def test_failed_when_coder_turn_fails(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install_fakes(monkeypatch, config, coder_ok=False)
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert result.review is None  # never got to review
    assert state["review_calls"] == []
    assert result.commits == []
    assert _commit_count_since_base(result) == 0


def test_failed_when_coder_makes_no_commit(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(monkeypatch, config, write_source=False)
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert result.review is None
    assert _commit_count_since_base(result) == 0


# --- artifacts --------------------------------------------------------------


def test_conversation_log_written_per_round(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(
        monkeypatch,
        config,
        reviews=[{"text": _FINDINGS_MAJOR}, {"text": _LGTM}],
    )
    result = develop_mod.develop(config)
    assert result.conversation_log is not None
    log = result.conversation_log.read_text()
    assert "## Round 1" in log and "## Round 2" in log
    # both the coder's and the reviewer's handoffs are inlined
    assert "Coder" in log and f"Reviewer [{config.reviewer}]" in log
    # handoff bodies are blockquoted so their own "## Status" headings don't
    # become siblings of the log's "## Round N" structure
    assert "> ## Status:" in log
    assert "\n## Status:" not in log


# --- test gate (T4) ---------------------------------------------------------


def _gated_config(config: DevelopConfig, **overrides) -> DevelopConfig:
    from dataclasses import replace

    return replace(config, test_command="fake-tests", **overrides)


def test_gate_skipped_when_no_command_detected(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # fixture repo has no Makefile/pytest markers -> detection finds nothing
    state = _install_fakes(monkeypatch, config)
    result = develop_mod.develop(config)
    assert result.status == "approved"
    assert result.test_gate is None
    assert state["gate_calls"] == []


def test_gate_green_recorded_on_approval(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    cfg = _gated_config(config)
    state = _install_fakes(monkeypatch, cfg, gates=[True])
    result = develop_mod.develop(cfg)
    assert result.status == "approved"
    assert result.test_gate is not None and result.test_gate.passed
    assert result.test_gate.command == "fake-tests"
    assert "test gate GREEN" in result.message
    assert len(state["gate_calls"]) == 1
    # the gate output artifact is preserved per round
    assert (cfg.gate_dir / "round_01" / "output.txt").is_file()


def test_gate_red_nonblocking_records_but_approves(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    cfg = _gated_config(config)  # block_on_red defaults False
    _install_fakes(monkeypatch, cfg, gates=[False])
    result = develop_mod.develop(cfg)
    assert result.status == "approved"  # recorded, not gating
    assert result.test_gate is not None and not result.test_gate.passed
    assert "test gate RED" in result.message


def test_gate_red_blocking_loops_and_feeds_coder(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    cfg = _gated_config(config, block_on_red=True)
    state = _install_fakes(monkeypatch, cfg, gates=[False, True])
    result = develop_mod.develop(cfg)

    # round 1: review LGTM but gate RED -> blocked; round 2: gate GREEN -> approved
    assert result.status == "approved"
    assert result.rounds == 2
    assert len(state["gate_calls"]) == 2
    # the round-2 coder prompt carried the gate failure + its output tail
    r2_prompt = state["coder_prompts"][1]
    assert "Independent test gate (FAILED)" in r2_prompt
    assert "2 failed, 10 passed" in r2_prompt
    assert result.test_gate is not None and result.test_gate.passed


def test_gate_red_blocking_exhausts_rounds(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    cfg = _gated_config(config, block_on_red=True, max_rounds=2)
    _install_fakes(monkeypatch, cfg, gates=[False])
    result = develop_mod.develop(cfg)
    assert result.status == "max_rounds"
    assert result.succeeded is False
    assert "test gate RED" in result.message


def test_gate_not_rerun_without_new_commit(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # round 1 commits (gate runs); round 2 the coder only disputes (no commit,
    # no new tree) -> the gate must not re-run.
    cfg = _gated_config(config)
    state = _install_fakes(
        monkeypatch,
        cfg,
        reviews=[{"text": _FINDINGS_MAJOR}, {"text": _LGTM}],
        source_rounds={1},
        gates=[True],
    )
    result = develop_mod.develop(cfg)
    assert result.status == "approved"
    assert result.rounds == 2
    assert len(state["gate_calls"]) == 1  # only the round-1 commit was gated


def test_gate_infra_error_clears_stale_red_under_block_on_red(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # Round 1: gate RED + block_on_red -> blocked despite LGTM review.
    # Round 2: NEW commit but the gate errors (infra) -> the stale round-1 RED
    # must NOT stand in for this commit; with no gate result the review's pass
    # approves the run (the gate is an independent check, not a dependency).
    cfg = _gated_config(config, block_on_red=True)
    state = _install_fakes(monkeypatch, cfg, gates=[False, "error"])
    result = develop_mod.develop(cfg)

    assert result.status == "approved"
    assert result.rounds == 2
    assert len(state["gate_calls"]) == 2  # round 2 did attempt its own gate
    assert result.test_gate is None  # no result for the approved commit
    assert "test gate" not in result.message  # no stale verdict reported


def test_gate_disabled_by_config(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from dataclasses import replace

    cfg = replace(config, test_command="fake-tests", test_gate=False)
    state = _install_fakes(monkeypatch, cfg, gates=[True])
    result = develop_mod.develop(cfg)
    assert result.test_gate is None
    assert state["gate_calls"] == []


# --- validation -------------------------------------------------------------


def test_develop_rejects_invalid_reviewer_name(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    cfg = DevelopConfig(
        repo=tmp_git_repo,
        description="x",
        work_dir=tmp_path / "work",
        reviewer="code quality",  # space -> invalid container/path/filename
        claude_config_dir=tmp_path / "fake-claude",
    )
    with pytest.raises(ValueError, match="invalid reviewer name"):
        develop_mod.develop(cfg)


def test_develop_rejects_unsupported_coder(tmp_git_repo: Path, tmp_path: Path) -> None:
    cfg = DevelopConfig(
        repo=tmp_git_repo,
        description="x",
        work_dir=tmp_path / "work",
        coder="codex",
        claude_config_dir=tmp_path / "fake-claude",
    )
    with pytest.raises(ValueError, match="unsupported tool"):
        develop_mod.develop(cfg)


def test_develop_rejects_bad_max_rounds(tmp_git_repo: Path, tmp_path: Path) -> None:
    cfg = DevelopConfig(
        repo=tmp_git_repo,
        description="x",
        work_dir=tmp_path / "work",
        max_rounds=0,
        claude_config_dir=tmp_path / "fake-claude",
    )
    with pytest.raises(ValueError, match="max_rounds"):
        develop_mod.develop(cfg)
