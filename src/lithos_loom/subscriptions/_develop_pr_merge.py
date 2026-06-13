"""``develop-pr-merge`` reconcile — auto-close tasks on PR merge (#87).

When a PR-producing plugin (``story-develop``) delivers a PR and exits, the
route uses ``completes_task = false``, so the Lithos task stays **open** with
``metadata.develop_pr_url`` recording the PR. A GitHub-**issue**-linked task
closes on merge via the issue close-mirror (``_github_issue_push``); a task
created directly in Lithos (no ``github_issue_url``) has no such path.

This module polls the delivered PR's merge state and acts on the task. It is
called per-task by the github-watcher child's periodic reconcile sweep
(``children/github_watcher.py``), which already enumerates open tasks and holds
a ``GitHubClient``. It keys off ``develop_pr_url`` only — **plugin-agnostic**:
any plugin that records ``develop_pr_url`` gets merge→complete for free.

De-dup lives in a single ``metadata.develop_pr_merge_state`` marker (mirrors
``github_state_snapshot``): once it reaches a terminal value the sweep skips the
task, so neither completion nor a finding fires twice.
"""

from __future__ import annotations

from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.github_client import GitHubClient, GitHubError
from lithos_loom.subscriptions import SubscriptionContext

__all__ = [
    "MERGE_STATE_KEY",
    "MERGE_STATE_TERMINAL",
    "MERGE_STATE_URL_KEY",
    "reconcile_develop_pr",
]

# Stable, machine-parseable finding prefix (see AGENTS.md): a delivered PR
# reached a closed-without-merge end state (closed unmerged, or deleted), so the
# task is left open for a human rather than completed.
DELIVERED_PR_CLOSED = "[DeliveredPRClosed]"

# Task-metadata keys carrying the de-dup marker. The marker is SCOPED to the
# develop_pr_url it resolved (MERGE_STATE_URL_KEY): the sweep skips a task only
# when its resolved state is terminal AND the recorded url still matches the
# task's current develop_pr_url. So when a rejected PR is abandoned and the task
# is re-developed into a REPLACEMENT PR, develop_pr_url changes, the recorded
# url no longer matches, and the sweep re-evaluates the new PR — without that
# scoping a stale marker would suppress the new PR forever.
MERGE_STATE_KEY = "develop_pr_merge_state"
MERGE_STATE_URL_KEY = "develop_pr_merge_url"

# Marker values that mean "this develop_pr_url is resolved". A still-open PR
# leaves the marker UNSET so the sweep re-polls next cycle.
MERGE_STATE_TERMINAL: frozenset[str] = frozenset(
    {"merged", "closed_unmerged", "gone", "unparseable"}
)

_GH_PREFIX = "https://github.com/"


def _parse_pr_url(url: object) -> tuple[str | None, int | None]:
    """``https://github.com/<owner>/<repo>/pull/<n>`` → ``("owner/repo", n)``.

    Returns ``(None, None)`` on anything unparseable. Mirrors
    ``_github_issue_push._resolve_repo_number`` but the path segment is
    ``pull`` (issues use ``issues``).
    """
    if not isinstance(url, str) or not url.startswith(_GH_PREFIX):
        return None, None
    parts = url[len(_GH_PREFIX) :].split("/")
    if len(parts) < 4 or parts[2] != "pull":
        return None, None
    try:
        return f"{parts[0]}/{parts[1]}", int(parts[3])
    except ValueError:
        return None, None


async def reconcile_develop_pr(
    task: Any, github: GitHubClient, ctx: SubscriptionContext
) -> str | None:
    """Reconcile one open task's delivered-PR merge state.

    Returns a short outcome label for the sweep's counters
    (``merged`` / ``closed_unmerged`` / ``still_open`` / ``gone`` /
    ``unparseable`` / ``error``), or ``None`` when the task is not a
    develop-PR task (no ``develop_pr_url``, issue-linked, or already resolved
    for *this same* ``develop_pr_url``). Never raises — GitHub and Lithos
    failures are caught, logged as ``[Friction]``, and (for transient ones)
    retried next sweep by leaving the marker unset.
    """
    metadata = task.metadata
    pr_url = metadata.get("develop_pr_url")
    if not isinstance(pr_url, str) or not pr_url:
        return None
    if metadata.get("github_issue_url"):
        # Issue-linked: the issue close-mirror already handles merge→complete.
        return None
    if (
        metadata.get(MERGE_STATE_KEY) in MERGE_STATE_TERMINAL
        and metadata.get(MERGE_STATE_URL_KEY) == pr_url
    ):
        # Already resolved THIS pr_url. A replacement PR (changed develop_pr_url)
        # has a stale recorded url, so it falls through and gets re-evaluated.
        return None

    repo, number = _parse_pr_url(pr_url)
    if repo is None or number is None:
        await _friction_and_mark(
            task,
            ctx,
            "unparseable",
            pr_url,
            f"[Friction] develop-pr-merge: task {task.id} has an unparseable "
            f"develop_pr_url ({pr_url!r}); cannot watch it for merge",
        )
        return "unparseable"

    try:
        pr = await github.get_pull_request(repo, number)
    except GitHubError as exc:
        # Auth / rate-limit / 5xx / network — transient from the sweep's POV.
        # Leave the marker unset so the next cycle retries (the sweep is a
        # natural retry loop; no in-cycle backoff to trigger).
        ctx.logger.warning(
            "[Friction] develop-pr-merge: fetching %s#%d for task %s failed "
            "(%s: %s); will retry next sweep",
            repo,
            number,
            task.id,
            type(exc).__name__,
            exc,
        )
        return "error"

    if pr is None:  # 404 — PR or repo gone (permanent, cf. #69)
        await _friction_and_mark(
            task,
            ctx,
            "gone",
            pr_url,
            f"{DELIVERED_PR_CLOSED} develop-pr-merge: delivered PR {pr_url} no "
            f"longer exists (404) for task {task.id}; left open for a human",
        )
        return "gone"

    if pr.merged:
        await _complete_merged(task, pr_url, pr, ctx)
        return "merged"

    if pr.state == "closed":  # closed without merging
        await _friction_and_mark(
            task,
            ctx,
            "closed_unmerged",
            pr_url,
            f"{DELIVERED_PR_CLOSED} develop-pr-merge: delivered PR {pr_url} was "
            f"closed without merging; task {task.id} left open for a human",
        )
        return "closed_unmerged"

    # state == "open" — still in flight; re-poll next sweep (no marker).
    return "still_open"


# ── Lithos side-effects (idempotent; swallow task_not_found) ───────────


async def _complete_merged(
    task: Any, pr_url: str, pr: Any, ctx: SubscriptionContext
) -> None:
    """Complete the task on PR merge, then write the ``merged`` marker.

    ``task_complete`` is independently idempotent (Lithos returns
    ``task_not_found`` for an already-terminal task — lithos#303 — which we
    swallow), so a crash between the complete and the marker write just
    re-completes (a no-op) and re-marks next sweep. Order: complete first; only
    mark once the close has been accepted (or the task was already terminal).
    """
    try:
        await ctx.lithos.task_complete(task_id=task.id)
    except LithosClientError as exc:
        if exc.code == "task_not_found":
            ctx.logger.info(
                "develop-pr-merge: task %s already terminal; marking merged",
                task.id,
            )
        else:
            ctx.logger.warning(
                "[Friction] develop-pr-merge: completing task %s on PR merge "
                "failed (%s); will retry next sweep",
                task.id,
                exc,
            )
            return  # leave the marker unset → retry next sweep
    ctx.logger.info(
        "develop-pr-merge: completed task %s on PR merge %s (%s)",
        task.id,
        pr_url,
        pr.merge_commit_sha or "no sha",
    )
    await _mark(task, ctx, "merged", pr_url)


async def _friction_and_mark(
    task: Any, ctx: SubscriptionContext, marker: str, pr_url: str, summary: str
) -> None:
    """Post a one-shot finding, then write the (state, url) marker.

    Post-then-mark ordering: a crash between the two costs at most one
    duplicate finding on the next sweep — the accepted tradeoff (cf.
    ``_github_issue_sync`` snapshot writes). The marker is what makes the
    finding one-shot for this ``pr_url``.
    """
    try:
        await ctx.lithos.finding_post(task_id=task.id, summary=summary)
    except LithosClientError as exc:
        if exc.code != "task_not_found":
            ctx.logger.warning(
                "[Friction] develop-pr-merge: posting finding for task %s "
                "failed (%s); will retry next sweep",
                task.id,
                exc,
            )
            return  # leave the marker unset → retry next sweep
    await _mark(task, ctx, marker, pr_url)


async def _mark(task: Any, ctx: SubscriptionContext, state: str, pr_url: str) -> None:
    """Write the de-dup marker (state + the url it resolved). Swallows
    ``task_not_found`` (the task may already be terminal)."""
    try:
        await ctx.lithos.task_update(
            task_id=task.id,
            metadata={MERGE_STATE_KEY: state, MERGE_STATE_URL_KEY: pr_url},
        )
    except LithosClientError as exc:
        if exc.code != "task_not_found":
            ctx.logger.warning(
                "[Friction] develop-pr-merge: marking task %s %s failed (%s)",
                task.id,
                state,
                exc,
            )
