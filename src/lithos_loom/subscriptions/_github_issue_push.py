"""``github-issue-push`` subscription — Slice 7.2.

Consumes ``lithos.task.*`` events emitted by
:class:`~lithos_loom.sources.lithos_event_stream.LithosEventStream` and
pushes the relevant change back to the linked GitHub issue:

- ``lithos.task.completed`` — close the issue as ``completed``.
- ``lithos.task.cancelled`` — close the issue as ``not_planned``.
- ``lithos.task.updated`` — if the Lithos title changed, mirror it via
  PATCH on the GH issue.

This is the Lithos→GH half of the bidirectional sync; the GH→Lithos
half lives in :mod:`._github_issue_sync`. Both directions co-exist in
the ``github-watcher`` child on the same in-process :class:`EventBus`.

Idempotency lives at the GitHub end: every terminal event re-fetches
the issue and skips the PATCH when it is already in the target state.
Cheap in steady state (the operator typically closes once), and the
re-fetch dodges the case where the GH→Lithos path already closed the
issue moments before the corresponding ``lithos.task.completed`` event
landed on the bus.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from lithos_loom.bus import Event
from lithos_loom.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubRepoNotFoundError,
)
from lithos_loom.subscriptions import Handler, SubscriptionContext

__all__ = ["EVENT_TYPES", "make_handler"]

logger = logging.getLogger(__name__)

EVENT_TYPES: tuple[str, ...] = (
    "lithos.task.created",
    "lithos.task.completed",
    "lithos.task.cancelled",
    "lithos.task.updated",
)
"""Bus event types this handler consumes.

PR-review finding 4 (round 3, 2026-05-30) added ``lithos.task.created``:
when the daemon restarts, ``LithosEventStream`` replays the open-task
snapshot as ``task.created`` events. A title that was renamed on the
Lithos side while the watcher was down only surfaces there — without
this entry the rename would be overwritten by the next GH→Lithos poll.
The handler treats ``created`` exactly like ``updated`` for the title
branch (compare, PATCH GH on drift)."""


def make_handler(github: GitHubClient) -> Handler:
    """Build a stateful handler closing over the shared GitHub client."""

    async def handle(event: Event, ctx: SubscriptionContext) -> None:
        if event.type not in EVENT_TYPES:
            ctx.logger.debug(
                "github-issue-push: ignoring unexpected event type %s", event.type
            )
            return

        payload = event.payload
        if not isinstance(payload, Mapping):
            ctx.logger.warning(
                "github-issue-push: malformed (non-mapping) payload for %s",
                event.type,
            )
            return

        metadata = _coerce_dict(payload.get("metadata"))
        repo, number = _resolve_repo_number(metadata)
        if repo is None or number is None:
            # Not a GH-linked task — by far the common case. Stay quiet
            # to avoid spamming the log when every Lithos task event
            # passes through this handler.
            ctx.logger.debug(
                "github-issue-push: %s for task %s has no github_issue_url",
                event.type,
                payload.get("id"),
            )
            return

        if event.type in ("lithos.task.completed", "lithos.task.cancelled"):
            await _mirror_close(event, payload, repo, number, github, ctx)
        elif event.type in ("lithos.task.updated", "lithos.task.created"):
            # ``created`` is bootstrap replay — same title-compare logic.
            await _mirror_title(payload, repo, number, github, ctx)

    return handle


# ── Branches ──────────────────────────────────────────────────────────


async def _mirror_close(
    event: Event,
    payload: Mapping[str, Any],
    repo: str,
    number: int,
    github: GitHubClient,
    ctx: SubscriptionContext,
) -> None:
    """Close the linked GH issue with the matching state_reason. Idempotent."""
    state_reason = (
        "completed" if event.type == "lithos.task.completed" else "not_planned"
    )

    current = await _fetch_or_skip(github, repo, number, "close mirror", ctx)
    if current is _SKIP:
        return
    if current is None:
        ctx.logger.info(
            "github-issue-push: %s/#%d no longer exists; skipping close mirror",
            repo,
            number,
        )
        return

    if current.state == "closed" and current.state_reason == state_reason:
        # The GH→Lithos path already closed it (or a prior push call did).
        ctx.logger.debug(
            "github-issue-push: %s/#%d already closed as %s — no-op",
            repo,
            number,
            state_reason,
        )
        return

    ctx.logger.info(
        "github-issue-push: closing %s/#%d as %s (task %s)",
        repo,
        number,
        state_reason,
        payload.get("id"),
    )
    await _patch_or_skip(
        github,
        repo,
        number,
        "close",
        ctx,
        state="closed",
        state_reason=state_reason,
    )


async def _mirror_title(
    payload: Mapping[str, Any],
    repo: str,
    number: int,
    github: GitHubClient,
    ctx: SubscriptionContext,
) -> None:
    """If the Lithos title differs from the GH title, PATCH the GH title."""
    lithos_title = payload.get("title")
    if not isinstance(lithos_title, str) or not lithos_title:
        return

    current = await _fetch_or_skip(github, repo, number, "title sync", ctx)
    if current is _SKIP:
        return
    if current is None:
        ctx.logger.debug(
            "github-issue-push: %s/#%d gone; skipping title sync", repo, number
        )
        return

    if current.title == lithos_title:
        # Already in sync — common case (most task.updated events touch
        # fields other than title, e.g. tags or metadata).
        return

    ctx.logger.info(
        "github-issue-push: renaming %s/#%d to %r (task %s)",
        repo,
        number,
        lithos_title,
        payload.get("id"),
    )
    await _patch_or_skip(github, repo, number, "title", ctx, title=lithos_title)


# ── Error classification ──────────────────────────────────────────────


# Sentinel returned by helper functions when a *permanent* error (auth,
# 404) means the handler should skip rather than retry. Distinct from
# ``None`` (issue deleted, treated as a legitimate no-op).
_SKIP: Any = object()


async def _fetch_or_skip(
    github: GitHubClient,
    repo: str,
    number: int,
    operation: str,
    ctx: SubscriptionContext,
) -> Any:
    """get_issue with permanent vs transient classification.

    PR-review finding 3 (round 3, 2026-05-30): the previous code
    swallowed every ``GitHubError`` as [Friction] and returned, which
    discarded events even when the failure was transient (5xx, network
    blip). Now permanent failures (auth / not found) log + return the
    ``_SKIP`` sentinel; transient failures propagate so the consumer
    loop retries with backoff.
    """
    try:
        return await github.get_issue(repo, number)
    except (GitHubAuthError, GitHubRepoNotFoundError) as exc:
        ctx.logger.warning(
            "[Friction] github-issue-push: %s on %s/#%d hit permanent error "
            "(%s: %s); dropping event",
            operation,
            repo,
            number,
            type(exc).__name__,
            exc,
        )
        return _SKIP


async def _patch_or_skip(
    github: GitHubClient,
    repo: str,
    number: int,
    operation: str,
    ctx: SubscriptionContext,
    *,
    title: str | None = None,
    state: str | None = None,
    state_reason: str | None = None,
) -> None:
    """update_issue_fields with permanent vs transient classification.

    Forwards only the non-None kwargs so the call signature matches what
    the handler's branches send (title-only for renames, state+reason
    for closes) — keeps the assertions in tests precise.
    """
    kwargs: dict[str, str] = {}
    if title is not None:
        kwargs["title"] = title
    if state is not None:
        kwargs["state"] = state
    if state_reason is not None:
        kwargs["state_reason"] = state_reason
    try:
        await github.update_issue_fields(repo, number, **kwargs)
    except (GitHubAuthError, GitHubRepoNotFoundError) as exc:
        ctx.logger.warning(
            "[Friction] github-issue-push: %s PATCH for %s/#%d hit permanent "
            "error (%s: %s); dropping event",
            operation,
            repo,
            number,
            type(exc).__name__,
            exc,
        )


# ── Helpers ───────────────────────────────────────────────────────────


def _coerce_dict(value: Any) -> dict[str, Any]:
    """LithosEventStream emits MappingProxyType; normalise to plain dict."""
    if isinstance(value, dict):
        return value
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _resolve_repo_number(metadata: dict[str, Any]) -> tuple[str | None, int | None]:
    """Extract (repo, number) from task.metadata.

    Prefers ``github_issue_url`` because it is the canonical Slice 7.1
    field; falls back to ``github_issue_number`` paired with a repo
    inferred from the URL when the explicit number field is missing.

    Returns ``(None, None)`` when nothing parseable is present.
    """
    url = metadata.get("github_issue_url")
    if not isinstance(url, str) or not url:
        return None, None
    # Format: https://github.com/<owner>/<repo>/issues/<n>
    # Just enough parsing to extract the three relevant segments.
    prefix = "https://github.com/"
    if not url.startswith(prefix):
        return None, None
    rest = url[len(prefix) :]
    parts = rest.split("/")
    if len(parts) < 4 or parts[2] != "issues":
        return None, None
    repo = f"{parts[0]}/{parts[1]}"
    try:
        number = int(parts[3])
    except ValueError:
        # Fall back to the explicit number field if URL is malformed.
        explicit = metadata.get("github_issue_number")
        if isinstance(explicit, int):
            return repo, explicit
        return None, None
    return repo, number
