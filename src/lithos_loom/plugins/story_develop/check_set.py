"""The multi-check check-set abstraction for story-develop's deterministic gate (#131).

ADR 0003 §4 reframed the gate from a single test command into an ordered set of
named **checks**, each with a *state* (required / optional / informational /
not_applicable) and an *execution outcome* (did the tool run) that is kept
separate from whether its result *blocks* approval.

This module is the pure-data layer: the :class:`Check` spec, the per-check
:class:`CheckResult`, the aggregate :class:`CheckSetResult`, and the
``(exit_code, output) -> execution_outcome`` adapter :func:`classify_execution`.
The container mechanics live in :mod:`test_gate`; the orchestration (building the
default set, running it per round) lives in :mod:`develop`.

#131 ships exactly one check — ``test`` — so the default set is degenerate and
behaviour is identical to the old single-command gate. The structure is what the
follow-on slices extend: #132 turns ``CheckResult.gate`` into a finding ledger,
#133 adds per-ecosystem applicability, #136 renders the aggregate into prompts,
#139 lets a Review Profile select the set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .test_gate import GateResult

# A check's role in the floor (ADR §4). #131 only emits "required"/"informational"
# (mapped from the legacy block_on_red flag, ADR §10); "optional"/"not_applicable"
# are reserved for #133/#139.
CheckState = Literal["required", "optional", "informational", "not_applicable"]

# "Did the tool run", kept separate from "did its result block" (ADR §4). A RED
# run is still ``ran``; ``errored`` is an infra failure (the tool never executed).
ExecutionOutcome = Literal["ran", "absent", "errored", "timed_out", "n_a"]

_NON_BLOCKING_STATES: frozenset[str] = frozenset(
    {"informational", "optional", "not_applicable"}
)
# Outcomes where the check produced no verdict — an infra skip, which (in the
# foundation slice) never blocks, matching the old gate's "infra error skips it".
_NON_VERDICT_OUTCOMES: frozenset[str] = frozenset({"absent", "errored", "n_a"})


@dataclass(frozen=True)
class Check:
    """The spec for one deterministic check — what to run and how it counts.

    Carries no result state. A Review Profile (#139) is a list of these; #131
    ships exactly one (the ``test`` check). ``state`` is the §4 axis that #133
    (applicability) and #139 (profiles) extend.
    """

    name: str
    command: str
    state: CheckState


@dataclass(frozen=True)
class CheckResult:
    """The outcome of running one :class:`Check` against a round commit.

    ``gate`` is the raw container outcome (the input #132's severity adapter will
    consume); it is ``None`` when the check never executed.
    """

    check: Check
    execution_outcome: ExecutionOutcome
    gate: GateResult | None

    @property
    def passed(self) -> bool:
        """Whether this check is satisfied *for approval* (i.e. does not block).

        Non-blocking states (informational / optional / not_applicable) always
        pass. An infra skip (errored / absent / n_a) never blocks in the
        foundation slice — matching the old gate, where an infra error skipped the
        check rather than failing the run; #132/#133 tighten
        "required-but-absent -> blocks" once absent checks become possible.
        Otherwise a check passes iff it ran green.
        """
        if self.check.state in _NON_BLOCKING_STATES:
            return True
        if self.execution_outcome in _NON_VERDICT_OUTCOMES:
            return True
        return self.gate is not None and self.gate.passed


@dataclass(frozen=True)
class CheckSetResult:
    """The aggregate outcome of running an ordered check-set for one round."""

    results: tuple[CheckResult, ...]

    @property
    def test_result(self) -> CheckResult | None:
        """The ``test`` check's result, if the set contained one."""
        return next((r for r in self.results if r.check.name == "test"), None)

    @property
    def test_gate(self) -> GateResult | None:
        """The ``test`` check's raw :class:`GateResult` — the back-compat view that
        ``DevelopResult.test_gate`` / ``pr_delivery`` / ``_gate_note`` still read.
        ``None`` when the test check didn't run (or there was none)."""
        tr = self.test_result
        return tr.gate if tr is not None else None

    @property
    def blocking_passed(self) -> bool:
        """True when no check blocks approval (every result ``passed``)."""
        return all(r.passed for r in self.results)

    @property
    def aggregate_verdict(self) -> str | None:
        """The worst verdict across checks that produced one, or ``None`` when none
        ran. Feeds the run summary / PR body. For the ``{test}`` set this is exactly
        the test check's verdict."""
        gates = [r.gate for r in self.results if r.gate is not None]
        if not gates:
            return None
        if any(g.timed_out for g in gates):
            return "TIMEOUT"
        return "RED" if any(not g.passed for g in gates) else "GREEN"


def classify_execution(gate: GateResult | None) -> ExecutionOutcome:
    """Map a raw container outcome onto the ``execution_outcome`` axis.

    ``None`` (the infra-error path) -> ``errored``; a timed-out run -> ``timed_out``;
    everything else (GREEN *or* RED) -> ``ran`` (a RED run still executed).
    """
    if gate is None:
        return "errored"
    if gate.timed_out:
        return "timed_out"
    return "ran"
