"""Tests for the multi-check check-set abstraction (#131).

Pure types + the execution-outcome adapter, plus the ``build_default_check_set``
constructor. No Docker — the container run is exercised via the existing
``test_gate`` seam in the core orchestration tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import develop as develop_mod
from lithos_loom.plugins.story_develop.check_set import (
    Check,
    CheckResult,
    CheckSetResult,
    classify_execution,
)
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.develop import build_default_check_set
from lithos_loom.plugins.story_develop.test_gate import GateResult


def _green() -> GateResult:
    return GateResult(command="pytest", exit_code=0, passed=True, output_tail="ok")


def _red() -> GateResult:
    return GateResult(command="pytest", exit_code=1, passed=False, output_tail="boom")


def _timeout() -> GateResult:
    return GateResult(command="pytest", exit_code=124, passed=False, output_tail="")


# --- classify_execution: the (exit_code, output) -> execution_outcome axis ----


def test_classify_execution_ran_for_green_and_red() -> None:
    # A RED run still RAN — execution success is a separate axis from blocking.
    assert classify_execution(_green()) == "ran"
    assert classify_execution(_red()) == "ran"


def test_classify_execution_timed_out_and_errored() -> None:
    assert classify_execution(_timeout()) == "timed_out"
    assert classify_execution(None) == "errored"  # infra error -> never executed


# --- CheckResult.passed: the blocking semantics ------------------------------


def test_required_check_blocks_on_red() -> None:
    r = CheckResult(Check("test", "pytest", "required"), "ran", _red())
    assert r.passed is False


def test_required_check_passes_on_green() -> None:
    r = CheckResult(Check("test", "pytest", "required"), "ran", _green())
    assert r.passed is True


def test_required_check_blocks_on_timeout() -> None:
    r = CheckResult(Check("test", "pytest", "required"), "timed_out", _timeout())
    assert r.passed is False


def test_informational_check_never_blocks_even_red() -> None:
    r = CheckResult(Check("lint", "ruff", "informational"), "ran", _red())
    assert r.passed is True


def test_required_check_errored_does_not_block() -> None:
    # The foundation-slice rule (matches today's "infra error skips the gate"):
    # a required check that errored at the infra level never BLOCKS.
    r = CheckResult(Check("test", "pytest", "required"), "errored", None)
    assert r.passed is True


# --- CheckSetResult aggregate views ------------------------------------------


def test_single_test_check_views_reduce_to_the_gate() -> None:
    g = _green()
    cs = CheckSetResult((CheckResult(Check("test", "pytest", "required"), "ran", g),))
    assert cs.test_gate is g
    assert cs.blocking_passed is True
    assert cs.aggregate_verdict == "GREEN"


def test_single_red_required_check_blocks() -> None:
    cs = CheckSetResult(
        (CheckResult(Check("test", "pytest", "required"), "ran", _red()),)
    )
    assert cs.blocking_passed is False
    assert cs.aggregate_verdict == "RED"


def test_two_check_set_separates_blocking_from_verdict() -> None:
    # Proves the structure is real (ordered, multi-check, separated axes) without
    # shipping a second check in the default set: a green REQUIRED test plus a RED
    # INFORMATIONAL check -> nothing blocks, but the rolled-up verdict is RED.
    cs = CheckSetResult(
        (
            CheckResult(Check("test", "pytest", "required"), "ran", _green()),
            CheckResult(Check("lint", "ruff", "informational"), "ran", _red()),
        )
    )
    assert cs.blocking_passed is True
    assert cs.aggregate_verdict == "RED"


def test_timeout_dominates_aggregate_verdict() -> None:
    cs = CheckSetResult(
        (
            CheckResult(Check("test", "pytest", "required"), "ran", _green()),
            CheckResult(
                Check("lint", "ruff", "informational"), "timed_out", _timeout()
            ),
        )
    )
    assert cs.aggregate_verdict == "TIMEOUT"


def test_test_gate_view_is_none_without_a_test_check() -> None:
    cs = CheckSetResult(
        (CheckResult(Check("lint", "ruff", "informational"), "ran", _green()),)
    )
    assert cs.test_gate is None


def test_errored_test_check_clears_the_gate_view() -> None:
    # The stale-RED-clearing path: a test check that errored -> test_gate is None
    # (so DevelopResult.test_gate is None) AND blocking_passed is True.
    cs = CheckSetResult(
        (CheckResult(Check("test", "pytest", "required"), "errored", None),)
    )
    assert cs.test_gate is None
    assert cs.blocking_passed is True
    assert cs.aggregate_verdict is None  # no check produced a verdict


# --- build_default_check_set: the {test} default + the §10 re-scope ----------


def _config(tmp_path: Path, **kw: object) -> DevelopConfig:
    return DevelopConfig(
        repo=tmp_path,
        description="x",
        work_dir=tmp_path,
        **kw,  # type: ignore[arg-type]
    )


def test_default_set_is_one_informational_test_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        develop_mod, "_resolve_test_command", lambda config, wt: "pytest"
    )
    checks = build_default_check_set(_config(tmp_path, block_on_red=False), tmp_path)
    assert len(checks) == 1
    assert checks[0] == Check(name="test", command="pytest", state="informational")


def test_block_on_red_makes_the_test_check_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        develop_mod, "_resolve_test_command", lambda config, wt: "pytest"
    )
    checks = build_default_check_set(_config(tmp_path, block_on_red=True), tmp_path)
    assert checks[0].state == "required"


def test_test_gate_false_excludes_the_test_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR §10: develop_test_gate=false drops only the `test` check; with a
    # one-element default set that is an empty set (observably "no gate").
    def _boom(config: object, wt: object) -> str:
        raise AssertionError("must not resolve a command when the gate is off")

    monkeypatch.setattr(develop_mod, "_resolve_test_command", _boom)
    assert build_default_check_set(_config(tmp_path, test_gate=False), tmp_path) == ()


def test_no_detected_command_yields_empty_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(develop_mod, "_resolve_test_command", lambda config, wt: None)
    assert build_default_check_set(_config(tmp_path, test_gate=True), tmp_path) == ()
