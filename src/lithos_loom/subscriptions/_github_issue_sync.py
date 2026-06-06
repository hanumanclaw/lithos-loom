"""``github-issue-sync`` subscription handler — Slice 7.1.

Consumes ``github.issue.seen`` events from the github_issue_watcher
source and reconciles each issue against Lithos:

- **New issue (no linkage marker)**: create a Lithos task, then write
  ``<!-- lithos:<task_id> -->`` into the GitHub issue body so the next
  poll recognises the linkage.
- **Existing marker → open task**: no-op (the watcher will re-emit on
  every poll until the cursor catches up).
- **Existing marker → closed-completed on GH**: ``task_complete``.
- **Existing marker → closed-not_planned on GH**: ``task_cancel``.
- **Marker deleted by operator but a Lithos task carries
  ``metadata.github_issue_url`` for this URL**: re-write the marker on
  GitHub. Don't create a duplicate task.
- **Marker points at a deleted Lithos task** (operator removed it):
  treat as new and create a fresh task + marker.

State on the Lithos task:

    title       = issue.title
    description = issue.body
    tags        = issue.labels + ["github-issue"]
    metadata    = {
      project: <slug>,
      github_issue_url: <url>,
      github_issue_number: N,
      github_labels: [<labels>],            # snapshotted for drift sync
      github_state_snapshot: <issue.state>, # snapshotted for reopen dedup
    }

The exclude-filter knobs (``github_issue_exclude_labels`` /
``..._authors``) are sourced from the project-context doc's
``github_exclude_labels`` / ``github_exclude_authors`` metadata and
shipped on every event payload by the watcher. The handler applies
them only at import time — already-linked tasks survive an after-the-
fact filter add (PRD: "exclude is only at import time").
"""

from __future__ import annotations

import logging
from typing import Any

from lithos_loom.bus import Event
from lithos_loom.errors import LithosClientError
from lithos_loom.github_client import (
    GitHubClient,
    GitHubError,
    apply_marker,
    strip_marker,
)
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions import Handler, SubscriptionContext

__all__ = ["EVENT_TYPE", "make_handler"]

logger = logging.getLogger(__name__)

EVENT_TYPE = "github.issue.seen"
GITHUB_ISSUE_TAG = "github-issue"
"""Tag added to every Loom-created task derived from a GitHub issue.
Lets the operator filter tasks by origin without inspecting metadata."""


def make_handler(github: GitHubClient) -> Handler:
    """Build a stateful handler bound to the shared GitHub client.

    The handler closes over ``github`` so it doesn't need a per-call
    factory. Production wires this once in the github-watcher child
    next to the watcher source.
    """

    async def handle(event: Event, ctx: SubscriptionContext) -> None:
        if event.type != EVENT_TYPE:
            ctx.logger.debug(
                "github-issue-sync: ignoring unexpected event type %s", event.type
            )
            return

        payload = event.payload
        issue = _ParsedIssue.from_payload(payload)
        if issue is None:
            ctx.logger.warning(
                "github-issue-sync: malformed payload for %s: %r",
                event.type,
                dict(payload),
            )
            return

        await _reconcile(issue, ctx, github)

    return handle


# ── Parsed event shape ────────────────────────────────────────────────


class _ParsedIssue:
    """Strongly-typed view of the bus payload.

    Lives next to the handler because nothing else needs it. Constructed
    via :meth:`from_payload` to centralise the malformed-payload guard.
    """

    __slots__ = (
        "author",
        "body",
        "exclude_authors",
        "exclude_labels",
        "html_url",
        "labels",
        "number",
        "repo",
        "slug",
        "state",
        "state_reason",
        "title",
    )

    def __init__(
        self,
        *,
        slug: str,
        repo: str,
        number: int,
        title: str,
        body: str,
        state: str,
        state_reason: str | None,
        labels: list[str],
        author: str,
        html_url: str,
        exclude_labels: list[str] | None = None,
        exclude_authors: list[str] | None = None,
    ) -> None:
        self.slug = slug
        self.repo = repo
        self.number = number
        self.title = title
        self.body = body
        self.state = state
        self.state_reason = state_reason
        self.labels = labels
        self.author = author
        self.html_url = html_url
        self.exclude_labels = exclude_labels or []
        self.exclude_authors = exclude_authors or []

    @classmethod
    def from_payload(cls, payload: Any) -> _ParsedIssue | None:
        try:
            return cls(
                slug=str(payload["slug"]),
                repo=str(payload["repo"]),
                number=int(payload["number"]),
                title=str(payload["title"]),
                body=str(payload.get("body") or ""),
                state=str(payload["state"]),
                state_reason=_optional_str(payload.get("state_reason")),
                labels=list(payload.get("labels") or ()),
                author=str(payload.get("author") or ""),
                html_url=str(payload["html_url"]),
                exclude_labels=list(payload.get("exclude_labels") or ()),
                exclude_authors=list(payload.get("exclude_authors") or ()),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


# ── Reconciliation ─────────────────────────────────────────────────────


async def _reconcile(
    issue: _ParsedIssue, ctx: SubscriptionContext, github: GitHubClient
) -> None:
    from lithos_loom.github_client import parse_marker

    marker_task_id = parse_marker(issue.body)
    if marker_task_id is not None:
        existing = await _fetch_task(ctx, marker_task_id)
        if existing is not None:
            await _reconcile_existing(issue, existing, ctx)
            return
        # Marker points at a missing task — operator likely deleted it
        # in Lithos. Fall through and create a fresh task; the marker
        # writer below will overwrite the stale marker.
        ctx.logger.info(
            "github-issue-sync: marker on issue %s/#%d points at missing "
            "task %s; recreating",
            issue.repo,
            issue.number,
            marker_task_id,
        )

    # No marker — try to find a Lithos task that already tracks this URL.
    matching = await _find_task_by_url(ctx, issue.html_url)
    if matching is not None:
        # Operator deleted the marker but the task still exists. Re-
        # write the marker rather than creating a duplicate task.
        ctx.logger.info(
            "github-issue-sync: re-writing missing marker on %s/#%d → task %s",
            issue.repo,
            issue.number,
            matching.id,
        )
        await _apply_marker_safe(github, issue, matching.id, ctx)
        # Also reconcile in case the issue was closed during the marker-less window.
        await _reconcile_existing(issue, matching, ctx)
        return

    # No marker, no matching task. Skip closed issues — they were closed
    # without ever having existed in Lithos and we don't backfill historic
    # closures.
    if issue.state == "closed":
        ctx.logger.debug(
            "github-issue-sync: skipping already-closed %s/#%d (no Lithos task)",
            issue.repo,
            issue.number,
        )
        return

    # Apply per-project exclude filters at import time only. Already-linked
    # tasks (marker present, or matching URL above) bypass the filter — the
    # PRD locks "exclude is only at import time".
    excluded_reason = _matched_exclude_filter(issue)
    if excluded_reason is not None:
        ctx.logger.info(
            "github-issue-sync: skipping %s/#%d on import (%s)",
            issue.repo,
            issue.number,
            excluded_reason,
        )
        return

    await _create_task_and_mark(issue, ctx, github)


def _matched_exclude_filter(issue: _ParsedIssue) -> str | None:
    """Return a human-readable reason if ``issue`` matches a project filter.

    Returns ``None`` when no filter matches (the create path proceeds).
    Author check beats label check so the log line names the more
    specific signal when both fire (e.g. dependabot AND label 'automated').
    """
    if issue.author and issue.author in issue.exclude_authors:
        return f"excluded author {issue.author!r}"
    matched_labels = [lbl for lbl in issue.labels if lbl in issue.exclude_labels]
    if matched_labels:
        return f"excluded label(s) {matched_labels!r}"
    return None


async def _reconcile_existing(
    issue: _ParsedIssue, task: Task, ctx: SubscriptionContext
) -> None:
    """Apply GH state to a known Lithos task. Idempotent.

    Slice 7.2 layers three branches on top of the original close mirror:

    1. **Drift sync** (always runs): title / body / labels / state-snapshot.
       Builds a single merged ``task_update`` payload so a steady-state poll
       costs zero round-trips and a poll that observes multiple drifts costs
       exactly one.
    2. **Reopen finding**: terminal Lithos task + GH-open + snapshot bump
       fires ``[ReopenRequested]`` once. The snapshot transition (handled
       in step 1) is what de-dupes subsequent polls.
    3. **Close mirror** (Slice 7.1, preserved): GH-closed + Lithos-open
       triggers ``task_complete`` / ``task_cancel`` based on ``state_reason``.

    Reopen detection must compare the *current* snapshot value, so it
    inspects ``task.metadata`` BEFORE drift sync rewrites it.
    """
    # Reopen detection reads the snapshot before drift sync mutates it.
    # PR-review finding 1 (round 3, 2026-05-30): a failure in this
    # branch (finding_post or downstream drift sync) propagates and
    # freezes the watcher's cursor; the next poll re-enters the same
    # reopen condition because the snapshot hasn't advanced yet. A
    # rare interleaving — finding_post succeeds, drift sync's
    # task_update raises — can produce a single duplicate finding on
    # retry. Accepted: duplicate findings are visible noise but not
    # data loss, and the alternative (a separate snapshot-only
    # task_update between the two) costs every reopen an extra MCP
    # round-trip.
    prior_snapshot = task.metadata.get("github_state_snapshot")
    if (
        task.status in ("completed", "cancelled")
        and issue.state == "open"
        and prior_snapshot != "open"
    ):
        ctx.logger.info(
            "github-issue-sync: reopen detected on %s/#%d (task %s)",
            issue.repo,
            issue.number,
            task.id,
        )
        await ctx.lithos.finding_post(
            task_id=task.id,
            summary=(
                f"[ReopenRequested] GH issue {issue.repo}#{issue.number} reopened"
            ),
            agent=ctx.agent_id,
        )

    await _sync_drift(issue, task, ctx)

    if issue.state != "closed":
        return

    if task.status != "open":
        # Already terminal in Lithos. Idempotent skip — re-emitting an
        # event for a closed-on-GH issue that's already closed in Lithos
        # is the common steady-state case.
        ctx.logger.debug(
            "github-issue-sync: %s/#%d closed and task %s already %s — no-op",
            issue.repo,
            issue.number,
            task.id,
            task.status,
        )
        return

    if issue.state_reason == "completed":
        ctx.logger.info(
            "github-issue-sync: completing task %s (closed via %s/#%d)",
            task.id,
            issue.repo,
            issue.number,
        )
        await _safe_call(
            ctx,
            ctx.lithos.task_complete(task_id=task.id, agent=ctx.agent_id),
            describe=f"complete task {task.id}",
        )
    elif issue.state_reason == "not_planned":
        ctx.logger.info(
            "github-issue-sync: cancelling task %s (closed as not_planned via %s/#%d)",
            task.id,
            issue.repo,
            issue.number,
        )
        await _safe_call(
            ctx,
            ctx.lithos.task_cancel(
                task_id=task.id,
                agent=ctx.agent_id,
                reason=f"GH closed as not_planned: {issue.html_url}",
            ),
            describe=f"cancel task {task.id}",
        )
    else:
        ctx.logger.info(
            "github-issue-sync: %s/#%d closed without state_reason; "
            "leaving task %s open",
            issue.repo,
            issue.number,
            task.id,
        )


# ── Slice 7.2: drift sync helpers ─────────────────────────────────────


async def _sync_drift(
    issue: _ParsedIssue,
    task: Task,
    ctx: SubscriptionContext,
) -> None:
    """Mirror GH-side drift (title / body / labels) into Lithos.

    Build a single merged ``task_update`` payload to keep steady-state
    polls cheap. The state-snapshot field rides on the same write so the
    reopen-finding de-dupe stays consistent without an extra round-trip.

    Skipped entirely for terminal tasks (status ``completed`` /
    ``cancelled``). Pinned via soak 2026-05-30: Lithos ``task_update``
    returns ``task_not_found`` for terminal tasks (upstream
    `agent-lore/lithos#303`), so the per-poll boundary replay of a
    just-closed issue would otherwise re-attempt and re-log
    ``[Friction]`` every cycle. Until #303 lands, the metadata
    snapshot (and thus reopen-finding dedup on terminal tasks) is
    frozen at the value it had when the task went terminal — a known
    limit documented in the lithos-schema-status memory note.
    """
    if task.status != "open":
        ctx.logger.debug(
            "github-issue-sync: drift sync skipped for terminal task %s "
            "(Lithos #303: task_update rejects terminal tasks)",
            task.id,
        )
        return

    updates: dict[str, Any] = {}
    metadata_updates: dict[str, Any] = {}

    if issue.title != task.title:
        updates["title"] = issue.title

    body_sans_marker = strip_marker(issue.body)
    current_desc = (task.description or "").strip()
    if body_sans_marker != current_desc:
        updates["description"] = body_sans_marker

    raw_snapshot = task.metadata.get("github_labels") or ()
    old_snapshot: list[str] = [str(label) for label in raw_snapshot]
    new_labels = list(issue.labels)
    if set(old_snapshot) != set(new_labels):
        new_tags = _merge_tags_preserving_operator_adds(
            list(task.tags), old_snapshot, new_labels
        )
        if set(new_tags) != set(task.tags):
            updates["tags"] = new_tags
        metadata_updates["github_labels"] = new_labels

    if task.metadata.get("github_state_snapshot") != issue.state:
        metadata_updates["github_state_snapshot"] = issue.state

    if metadata_updates:
        updates["metadata"] = metadata_updates

    if not updates:
        return

    await _safe_call(
        ctx,
        ctx.lithos.task_update(
            task_id=task.id,
            agent=ctx.agent_id,
            **updates,
        ),
        describe=f"drift-sync task {task.id}",
    )


def _merge_tags_preserving_operator_adds(
    current: list[str],
    old_snapshot: list[str],
    new_labels: list[str],
) -> list[str]:
    """Reconcile Lithos task tags against a GH label diff.

    - Remove tags that were in the *prior* GH snapshot but are no longer
      in GH's current label list (GH-side removals propagate).
    - Add tags that are in GH's current label list but not yet on the task
      (GH-side additions propagate).
    - Preserve everything else — operator-added Lithos tags survive
      because they were never in any GH snapshot.

    Order-stable: existing tags keep their relative position; new GH
    labels append at the end.
    """
    removed = set(old_snapshot) - set(new_labels)
    seen: set[str] = set()
    result: list[str] = []
    for tag in current:
        if tag in removed or tag in seen:
            continue
        result.append(tag)
        seen.add(tag)
    for tag in new_labels:
        if tag in seen:
            continue
        result.append(tag)
        seen.add(tag)
    return result


async def _create_task_and_mark(
    issue: _ParsedIssue, ctx: SubscriptionContext, github: GitHubClient
) -> None:
    """Two-step: create the Lithos task, then write the marker on GitHub.

    If the marker write fails after task creation we end up with a Lithos
    task referencing the URL but no linkage marker on the issue. The
    next poll's no-marker / matching-URL branch picks this up and
    re-tries the marker write — eventually consistent.
    """
    metadata: dict[str, Any] = {
        "project": issue.slug,
        "github_issue_url": issue.html_url,
        "github_issue_number": issue.number,
        "github_labels": list(issue.labels),
        # Slice 7.2: bootstrap the snapshot so the reopen-finding de-dupe
        # has a baseline. Without it, a legacy migration path treats a
        # missing snapshot as "unknown" and could fire one spurious
        # finding on the first poll after close→reopen.
        "github_state_snapshot": issue.state,
    }
    tags = list(issue.labels) + [GITHUB_ISSUE_TAG]
    # PR-review finding 1 (round 3, 2026-05-30): a failed task_create
    # used to be swallowed as [Friction] and the handler returned
    # normally — the watcher then advanced the cursor past the issue
    # and that issue was permanently stranded. Propagate so the
    # dispatcher freezes the cursor; the next poll retries from the
    # same boundary.
    try:
        task_id = await ctx.lithos.task_create(
            title=issue.title,
            description=issue.body,
            agent=ctx.agent_id,
            tags=tags,
            metadata=metadata,
        )
    except (LithosClientError, OSError) as exc:
        ctx.logger.warning(
            "[Friction] github-issue-sync: task_create failed for %s/#%d: %s",
            issue.repo,
            issue.number,
            exc,
        )
        raise

    ctx.logger.info(
        "github-issue-sync: created task %s for %s/#%d",
        task_id,
        issue.repo,
        issue.number,
    )
    await _apply_marker_safe(github, issue, task_id, ctx)


async def _apply_marker_safe(
    github: GitHubClient,
    issue: _ParsedIssue,
    task_id: str,
    ctx: SubscriptionContext,
) -> None:
    """Write the canonical marker to the issue body; propagates GH errors.

    Re-fetches the issue body via ``github.get_issue`` immediately before
    the PATCH so an operator edit during the poll-to-PATCH window
    survives. GitHub's ``PATCH /issues/{n}`` is full-body replacement
    with no optimistic locking — the race window can't be closed, but
    fetching just before writing shrinks it from "one poll interval +
    handler latency" to "single round-trip latency".

    If the re-fetch fails (404, transport) we fall back to the body
    carried in the event payload — losing an operator-edit window is
    better than not writing the marker at all (which would cause the
    next poll to walk the orphan-marker recovery path and produce a
    duplicate write attempt).

    A marker-write failure now propagates (PR-review finding 1, round 3,
    2026-05-30). The watcher's inline dispatcher freezes the cursor at
    the prior issue's ``updated_at`` and the next poll re-fetches this
    issue — its URL-match recovery branch finds the existing task and
    re-writes the marker without creating a duplicate. The earlier
    swallow advanced the cursor past the unmarked issue and stranded
    the link.
    """
    body_source = issue.body
    try:
        fresh = await github.get_issue(issue.repo, issue.number)
    except GitHubError as exc:
        ctx.logger.debug(
            "github-issue-sync: get_issue for marker write on %s/#%d "
            "failed (%s); using poll-event body",
            issue.repo,
            issue.number,
            exc,
        )
    else:
        if fresh is not None:
            body_source = fresh.body

    new_body = apply_marker(body_source, task_id)
    # PR-review finding 1 (round 3, 2026-05-30): marker-write failures
    # used to be swallowed as [Friction], which let the watcher's
    # cursor advance past the unmarked issue. The next poll wouldn't
    # see it again (its ``updated_at`` is below the new cursor) so the
    # missing marker stayed orphaned indefinitely. Propagating freezes
    # the cursor; the next poll's URL-match recovery branch re-writes
    # the marker. We log the [Friction] line first so operators
    # grepping for the prefix still see the event.
    try:
        await github.update_issue_body(issue.repo, issue.number, new_body)
    except GitHubError as exc:
        ctx.logger.warning(
            "[Friction] github-issue-sync: marker write failed for %s/#%d "
            "(task %s): %s",
            issue.repo,
            issue.number,
            task_id,
            exc,
        )
        raise


# ── Lithos lookup helpers ─────────────────────────────────────────────


async def _fetch_task(ctx: SubscriptionContext, task_id: str) -> Task | None:
    """Return the task for ``task_id`` or ``None`` if it is *confirmed* absent.

    PR-review finding 2 (round 3, 2026-05-30): only
    ``LithosClientError(code="task_not_found")`` counts as confirmed
    absence. Transient transport / server errors must propagate so the
    watcher's inline dispatcher freezes the cursor — swallowing them
    here lets reconciliation fall through to the create branch and
    duplicate the task.
    """
    try:
        return await ctx.lithos.task_get(task_id=task_id)
    except LithosClientError as exc:
        if exc.code == "task_not_found":
            return None
        raise


async def _find_task_by_url(ctx: SubscriptionContext, url: str) -> Task | None:
    """Scan open + closed tasks for one whose metadata carries ``url``.

    PR-review finding 2 (round 3, 2026-05-30): a failed ``task_list``
    no longer falls through to ``None``. Propagation freezes the
    watcher's cursor so the next poll re-runs the marker-recovery
    lookup — a swallowed error here would let the create branch fire
    and produce a duplicate task.

    PR-review finding 1 (round 7, 2026-05-30): the scan is **unbounded**
    on every status because the no-duplicate contract is locked
    (SPEC §2.2: "Re-write the canonical marker on GitHub. No duplicate
    task."). A round-6 self-review pass added a 30-day cutoff to bound
    the cancelled/completed scan, but that turned a deleted-marker on
    an old reopened issue into a fresh duplicate task — breaking the
    contract for a speculative scaling concern. If MCP response size
    becomes a real production problem we'll add server-side metadata
    filtering (lithos-side) rather than client-side truncation here.
    """
    for status in ("open", "completed", "cancelled"):
        tasks = await ctx.lithos.task_list(status=status)
        for task in tasks:
            if task.metadata.get("github_issue_url") == url:
                return task
    return None


async def _safe_call(ctx: SubscriptionContext, coro: Any, *, describe: str) -> None:
    """Await ``coro`` logging a [Friction] line then re-raising on failure.

    Naming is now a slight misnomer — earlier rounds swallowed errors
    here so the runner didn't retry. PR-review finding 1 (round 3,
    2026-05-30) flipped that contract: load-bearing Lithos writes
    (``task_update`` for drift, ``task_complete``, ``task_cancel``)
    must surface to the watcher's inline dispatcher so the cursor
    freezes and the next poll retries. The [Friction] log line stays
    for the operator-grep convention.

    Soak observation (2026-05-30): Lithos's ``task_update`` returns
    ``task_not_found`` for tasks whose status is terminal
    (``completed`` / ``cancelled``) — even though ``task_get`` happily
    returns them. A poll that fetches a closed GH issue paired with a
    just-completed Lithos task (the operator ticked it in Obsidian,
    which closed both sides) then loops forever: drift-sync raises,
    cursor freezes, stuck retry re-hits the same wall. Treat
    ``task_not_found`` as **non-fatal**: log [Friction] but don't
    raise, so the dispatcher advances the cursor and the stuck entry
    drains. The spec's "drift sync always runs" contract holds at the
    handler-call level; whether Lithos accepts the write is a server
    concern we surface but don't loop on.
    """
    try:
        await coro
    except LithosClientError as exc:
        ctx.logger.warning("[Friction] github-issue-sync: %s failed: %s", describe, exc)
        if exc.code == "task_not_found":
            return
        raise
    except OSError as exc:
        ctx.logger.warning("[Friction] github-issue-sync: %s failed: %s", describe, exc)
        raise
