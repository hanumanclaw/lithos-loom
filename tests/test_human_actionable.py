"""Unit tests for ``is_human_actionable``.

The function is pure: ``(Task, routes, ObsidianSyncConfig) -> bool``.
All tests construct minimal fixtures and assert the decision directly.

Projection rule: an open task is human-actionable iff (a) no route is
claimable against it, OR (b) a ``human_blocking = true`` route currently
holds the claim. A route is claimable when every tag in its ``match.tags``
is present on the task (same all-tags semantic the bus enforces).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lithos_loom.config import ObsidianSyncConfig, RouteConfig, RouteMatch
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions._human_actionable import (
    human_blocking_route_name,
    is_human_actionable,
    would_be_actionable,
)


def _task(
    *,
    status: str = "open",
    tags: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
    claims: tuple[Mapping[str, Any], ...] = (),
) -> Task:
    return Task(
        id="t1",
        title="t",
        status=status,
        tags=tags,
        metadata=metadata or {},
        claims=claims,
    )


def _route(name: str, *, tags: tuple[str, ...], human_blocking: bool) -> RouteConfig:
    return RouteConfig(
        name=name,
        command="echo hi",
        match=RouteMatch(tags=tags),
        human_blocking=human_blocking,
    )


def _claim(aspect: str, agent: str = "lithos-orchestrator-test") -> Mapping[str, Any]:
    return {"agent": agent, "aspect": aspect}


def _cfg(
    *,
    include_blocked: bool = True,
    exclude_tags: tuple[str, ...] = (),
) -> ObsidianSyncConfig:
    return ObsidianSyncConfig(
        vault_path=Path("/vault"),
        include_blocked=include_blocked,
        exclude_tags=exclude_tags,
    )


# ── D6 first disjunct: open AND not claimable by any route ─────────────


def test_open_orphan_with_no_routes_is_actionable() -> None:
    """No routes configured at all → every open task is the operator's."""
    assert is_human_actionable(_task(tags=("anything",)), routes=[], cfg=_cfg()) is True


def test_open_task_with_no_matching_route_is_actionable() -> None:
    """A task whose tags don't fully cover any route's match.tags is an
    orphan — no automation will pick it up."""
    routes = [_route("r1", tags=("trigger:other",), human_blocking=False)]
    assert is_human_actionable(_task(tags=("needs-review",)), routes, _cfg()) is True


def test_multi_tag_route_partial_overlap_treats_task_as_orphan() -> None:
    """Regression: a multi-tag route ``["trigger:foo", "needs:bar"]`` must
    NOT be considered claimable against a task tagged only ``trigger:foo``.

    The bus matcher requires all listed tags to be present, so the
    route-runner would never claim the task. Before this guard, the
    helper used any-overlap semantics and reported the task as claimable,
    hiding it from Obsidian permanently — the worst kind of silent loss.
    """
    routes = [_route("multi", tags=("trigger:foo", "needs:bar"), human_blocking=False)]
    task = _task(tags=("trigger:foo",))  # missing "needs:bar"
    assert is_human_actionable(task, routes, _cfg()) is True


# ── D6 second disjunct: claimed by a human_blocking route ──────────────


def test_claimed_by_human_blocking_route_is_actionable() -> None:
    """The defining case: a route-runner has claimed this task on behalf
    of the human, e.g. story-review-human waiting for a PR merge."""
    routes = [_route("review-human", tags=("trigger:review",), human_blocking=True)]
    task = _task(tags=("trigger:review",), claims=(_claim("review-human"),))
    assert is_human_actionable(task, routes, _cfg()) is True


def test_claimable_by_human_blocking_route_but_not_yet_claimed_hidden() -> None:
    """Tag-matches a human_blocking route but no claim yet — wait for
    the route-runner to actually claim before projecting. Otherwise we'd
    surface work that's still in the autonomous-pickup queue."""
    routes = [_route("review-human", tags=("trigger:review",), human_blocking=True)]
    task = _task(tags=("trigger:review",), claims=())
    assert is_human_actionable(task, routes, _cfg()) is False


def test_claimed_by_autonomous_route_hidden() -> None:
    """A claim by an autonomous route → automation is handling it,
    nothing for the human to do."""
    routes = [_route("auto", tags=("trigger:auto",), human_blocking=False)]
    task = _task(tags=("trigger:auto",), claims=(_claim("auto"),))
    assert is_human_actionable(task, routes, _cfg()) is False


def test_claimable_by_autonomous_route_not_yet_claimed_hidden() -> None:
    """Open + claimable by autonomous route + no claim yet — wait for
    automation to pick it up. Not the operator's problem either way."""
    routes = [_route("auto", tags=("trigger:auto",), human_blocking=False)]
    task = _task(tags=("trigger:auto",), claims=())
    assert is_human_actionable(task, routes, _cfg()) is False


def test_human_blocking_claim_overrides_autonomous_claimability() -> None:
    """Two routes both match the task's tags — one autonomous, one
    human_blocking — and the human_blocking route already claimed.
    Project."""
    routes = [
        _route("auto", tags=("trigger:shared",), human_blocking=False),
        _route("review", tags=("trigger:shared",), human_blocking=True),
    ]
    task = _task(tags=("trigger:shared",), claims=(_claim("review"),))
    assert is_human_actionable(task, routes, _cfg()) is True


def test_claim_aspect_for_unknown_route_does_not_actionable() -> None:
    """A claim whose aspect doesn't match any configured human_blocking
    route name shouldn't promote the task to actionable. Defensive
    against operator-deleted routes leaving orphaned claims behind.

    Configures an autonomous route matching the task tag so the
    "orphan" first disjunct doesn't accidentally fire — that way the
    only path to True would be a human_blocking claim, which there
    isn't (the claim's aspect names a deleted route)."""
    task = _task(tags=("trigger:other",), claims=(_claim("deleted-route"),))
    routes = [
        _route("review", tags=("trigger:review",), human_blocking=True),
        _route("auto", tags=("trigger:other",), human_blocking=False),
    ]
    assert is_human_actionable(task, routes, _cfg()) is False


# ── Operator opt-outs ──────────────────────────────────────────────────


def test_include_blocked_false_with_deps_returns_false() -> None:
    """Operator opted out of blocked work — even an orphan blocked task is hidden."""
    task = _task(tags=(), metadata={"depends_on": ["other-task-id"]})
    assert (
        is_human_actionable(task, routes=[], cfg=_cfg(include_blocked=False)) is False
    )


def test_include_blocked_true_with_deps_returns_true() -> None:
    """D6 default: blocked tasks still project."""
    task = _task(tags=(), metadata={"depends_on": ["other-task-id"]})
    assert is_human_actionable(task, routes=[], cfg=_cfg(include_blocked=True)) is True


def test_excluded_tag_returns_false_even_for_orphan_task() -> None:
    """Operator denylist wins over the default-true orphan path."""
    task = _task(tags=("debug:trace", "needs-review"))
    cfg = _cfg(exclude_tags=("debug:trace",))
    assert is_human_actionable(task, routes=[], cfg=cfg) is False


def test_depends_on_missing_or_empty_does_not_block() -> None:
    """metadata.depends_on absent OR [] is not 'blocked' — both must project."""
    no_meta = _task(tags=(), metadata={})
    empty_deps = _task(tags=(), metadata={"depends_on": []})
    cfg = _cfg(include_blocked=False)  # the strictest setting
    assert is_human_actionable(no_meta, routes=[], cfg=cfg) is True
    assert is_human_actionable(empty_deps, routes=[], cfg=cfg) is True


# ── Status semantics ────────────────────────────────────────────────────


def test_completed_orphan_task_not_actionable() -> None:
    """First disjunct requires status==open. A completed orphan task is
    terminal — the removal-event branch handles it, not actionability."""
    task = _task(status="completed", tags=("orphan",))
    assert is_human_actionable(task, routes=[], cfg=_cfg()) is False


def test_cancelled_task_with_human_blocking_claim_not_actionable() -> None:
    """Even with a residual human_blocking claim, a cancelled task is
    terminal — D6's second disjunct requires the implicit "still open"
    context for a claim to imply actionable work."""
    routes = [_route("review", tags=("trigger:review",), human_blocking=True)]
    task = _task(
        status="cancelled", tags=("trigger:review",), claims=(_claim("review"),)
    )
    # Per the second disjunct as worded, a claim by a human_blocking
    # route is sufficient. But for a cancelled task this would be a
    # stale claim. We treat status as the gating signal — terminal
    # status overrides any claim. The handler also drops on
    # completed/cancelled events regardless of actionability, so this
    # is a belt-and-braces test of the helper alone.
    assert is_human_actionable(task, routes, _cfg()) is False


# ── human_blocking_route_name (US9 extraction) ─────────────────────────


def test_human_blocking_route_name_returns_route_for_matching_claim() -> None:
    routes = [_route("review-human", tags=("trigger:review",), human_blocking=True)]
    task = _task(claims=(_claim("review-human"),))
    assert human_blocking_route_name(task, routes) == "review-human"


def test_human_blocking_route_name_returns_none_when_no_claims() -> None:
    routes = [_route("review-human", tags=("trigger:review",), human_blocking=True)]
    task = _task(claims=())
    assert human_blocking_route_name(task, routes) is None


def test_human_blocking_route_name_returns_none_when_aspect_mismatch() -> None:
    """Claim aspect must equal a configured human_blocking route's name."""
    routes = [_route("review-human", tags=("trigger:review",), human_blocking=True)]
    task = _task(claims=(_claim("some-other-aspect"),))
    assert human_blocking_route_name(task, routes) is None


def test_human_blocking_route_name_returns_none_with_no_blocking_routes() -> None:
    """Claims by autonomous routes never qualify, even when aspect = name."""
    routes = [_route("auto", tags=("trigger:auto",), human_blocking=False)]
    task = _task(claims=(_claim("auto"),))
    assert human_blocking_route_name(task, routes) is None


def test_human_blocking_route_name_picks_first_matching_claim() -> None:
    """When two human_blocking routes have claimed the task, return the
    first claim's aspect (Lithos-canonical order) for stability."""
    routes = [
        _route("review-human", tags=("trigger:review",), human_blocking=True),
        _route("signoff", tags=("trigger:signoff",), human_blocking=True),
    ]
    task = _task(claims=(_claim("review-human"), _claim("signoff")))
    assert human_blocking_route_name(task, routes) == "review-human"


# ── would_be_actionable (PR #21 review extraction) ─────────────────────


def test_would_be_actionable_open_orphan_true() -> None:
    """The status-agnostic predicate matches is_human_actionable for
    the open-orphan case (D6 first disjunct)."""
    task = _task(status="open", tags=("anything",))
    assert would_be_actionable(task, routes=[], cfg=_cfg()) is True


def test_would_be_actionable_ignores_status_for_completed_orphan() -> None:
    """Distinguishing feature: a completed orphan returns True from
    would_be_actionable but False from is_human_actionable. Used by the
    obsidian-projection terminal-event branch to decide whether a
    just-resolved task should join the TTL lingering window."""
    task = _task(status="completed", tags=("anything",))
    assert would_be_actionable(task, routes=[], cfg=_cfg()) is True
    assert is_human_actionable(task, routes=[], cfg=_cfg()) is False


def test_would_be_actionable_cancelled_orphan_true() -> None:
    """Same as completed — terminal status doesn't disqualify."""
    task = _task(status="cancelled", tags=("anything",))
    assert would_be_actionable(task, routes=[], cfg=_cfg()) is True


def test_would_be_actionable_autonomous_route_match_false() -> None:
    """Autonomous-route work — never on the operator's view, whether
    open or completed. PR #21 review issue 2: this is the predicate the
    handler's terminal-event branch uses to skip ghost promotions."""
    routes = [_route("auto", tags=("trigger:auto",), human_blocking=False)]
    task = _task(status="completed", tags=("trigger:auto",))
    assert would_be_actionable(task, routes, _cfg()) is False


def test_would_be_actionable_human_blocking_claim_true() -> None:
    routes = [_route("review", tags=("trigger:review",), human_blocking=True)]
    task = _task(
        status="completed",
        tags=("trigger:review",),
        claims=(_claim("review"),),
    )
    assert would_be_actionable(task, routes, _cfg()) is True


def test_would_be_actionable_excluded_tag_false() -> None:
    """Operator opt-out wins over the default-true orphan path, same
    as is_human_actionable."""
    task = _task(status="completed", tags=("noisy",))
    assert (
        would_be_actionable(task, routes=[], cfg=_cfg(exclude_tags=("noisy",))) is False
    )


def test_would_be_actionable_blocked_with_include_blocked_false() -> None:
    """include_blocked=False + non-empty depends_on → hide, same as
    is_human_actionable."""
    task = _task(
        status="completed", tags=("anything",), metadata={"depends_on": ["dep-1"]}
    )
    cfg = _cfg(include_blocked=False)
    assert would_be_actionable(task, routes=[], cfg=cfg) is False
