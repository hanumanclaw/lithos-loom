"""Subprocess child that hosts the obsidian-sync runtime.

Spawned by the :class:`~lithos_loom.supervisor.Supervisor` whenever
the loaded config carries an ``[obsidian_sync]`` section. The supervisor
gate is the presence test; this child is responsible for everything
below that line.

The actions this child hosts (see :data:`_CHILD_ACTIONS`) are:

* ``"obsidian-projection"`` — renders projected task lines into
  ``_lithos/tasks.md``.
* ``"obsidian-status-transition"`` — consumes the fs watcher's
  ``obsidian.task.status_changed`` events and pushes the
  corresponding action to Lithos.
* ``"obsidian-priority-changed"`` — consumes the fs watcher's
  ``obsidian.task.priority_changed`` events; pushes the new
  priority enum to Lithos via ``task_update(metadata={"priority":
  <enum>})``.
* ``"obsidian-due-date-changed"`` — consumes the fs watcher's
  ``obsidian.task.due_date_changed`` events; pushes the new
  ``YYYY-MM-DD`` date to Lithos via
  ``task_update(metadata={"scheduled_for": <date>})``. Without this,
  date edits in the file would be silently overwritten on the next
  projection rewrite.

Subscription actions outside the allow-list (e.g. generic ``noop``)
are silently skipped here — they're routed to a different child.

Invocation contract (set by the supervisor):

    python -m lithos_loom.children.obsidian_sync --config <path>
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from lithos_loom.bus import EventBus
from lithos_loom.config import LogLevel, LoomConfig, SubscriptionConfig, load_config
from lithos_loom.lithos_client import LithosClient
from lithos_loom.sources.lithos_event_stream import LithosEventStream
from lithos_loom.sources.lithos_note_stream import LithosNoteStream
from lithos_loom.sources.obsidian_dir_watcher import ObsidianDirWatcher
from lithos_loom.sources.obsidian_fs_watcher import ObsidianFsWatcher
from lithos_loom.subscriptions import (
    Handler,
    SubscriptionContext,
    build_runners,
)
from lithos_loom.subscriptions._note_push import (
    make_handler as make_note_push_handler,
)
from lithos_loom.subscriptions._obsidian_due_date_changed import (
    handle as handle_obsidian_due_date_changed,
)
from lithos_loom.subscriptions._obsidian_priority_changed import (
    handle as handle_obsidian_priority_changed,
)
from lithos_loom.subscriptions._obsidian_projection import (
    make_handler as make_obsidian_projection_handler,
)
from lithos_loom.subscriptions._obsidian_status_transition import (
    handle as handle_obsidian_status_transition,
)
from lithos_loom.subscriptions._project_context_projection import (
    make_handler as make_project_context_projection_handler,
)
from lithos_loom.subscriptions._task_archive import (
    make_handler as make_task_archive_handler,
)
from lithos_loom.sync_state import ProjectionSyncState

# Actions this child is willing to host. Subscriptions whose ``action``
# is outside this set are silently skipped — some other child's job
# (route-runner for routes, a future generic subscription-runner for
# things like ``noop``).
_CHILD_ACTIONS: frozenset[str] = frozenset(
    {
        "obsidian-projection",
        "obsidian-status-transition",
        "obsidian-priority-changed",
        "obsidian-due-date-changed",
        "project-context-projection",
        "note-push",
        "task-archive",
    }
)

_LEVEL_MAP: dict[LogLevel, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

logger = logging.getLogger(__name__)


def _configure_logging(level: LogLevel) -> None:
    logging.basicConfig(
        level=_LEVEL_MAP[level],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The MCP SDK's SSE reader (``mcp.client.sse.sse_reader``) logs a
    # full ERROR-level traceback whenever its persistent session is
    # torn down — e.g. when Lithos restarts. Our LithosClient's outer
    # reconnect loop (and the subscription handlers' retry policy) is
    # what's actually responsible for recovery here; the SDK's
    # traceback is just noise that buries our own reconnect timeline.
    # Pin it to CRITICAL so we still see real failures (auth errors,
    # protocol bugs) but not the routine "peer closed connection"
    # exceptions that fire every time Lithos cycles. Conservative
    # version (WARNING) would still let the traceback through; the
    # SDK doesn't currently log anything informational below ERROR
    # we'd want to keep.
    logging.getLogger("mcp.client.sse").setLevel(logging.CRITICAL)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lithos_loom.children.obsidian_sync")
    parser.add_argument("--config", type=Path, default=None)
    return parser.parse_args(argv)


async def _amain(cfg: LoomConfig) -> int:
    if cfg.obsidian_sync is None:
        # Defensive: the supervisor's spawn gate is `cfg.obsidian_sync
        # is not None`, so reaching here means a config reload removed
        # the section underneath us, or someone invoked the module
        # directly with the wrong config. Exit non-zero so the
        # supervisor sees the discrepancy rather than us silently
        # parking.
        logger.error("obsidian-sync spawned without [obsidian_sync] config; exiting")
        return 1

    obs = cfg.obsidian_sync
    logger.info(
        "obsidian-sync child started; vault=%s tasks_file=%s "
        "projects_dir=%s resolved_ttl_days=%d include_blocked=%s "
        "exclude_tags=%s",
        obs.vault_path,
        obs.tasks_file,
        obs.projects_dir,
        obs.resolved_ttl_days,
        obs.include_blocked,
        list(obs.exclude_tags) or "[]",
    )

    # Filter cfg.subscriptions to the actions this child is willing to
    # host. Other actions are some other child's job (route-runner for
    # routes; a future subscription-runner child for generic actions
    # like `noop`).
    child_specs = tuple(s for s in cfg.subscriptions if s.action in _CHILD_ACTIONS)
    # Fail fast on duplicate specs of the same action. The projection
    # handler is stateful (per-handler state dict + per-handler
    # tasks_file path) so two specs would race on the same file. The
    # status-transition handler is stateless but two specs would still
    # mean duplicate Lithos calls per event, which is never what the
    # operator wanted.
    by_action: dict[str, list[SubscriptionConfig]] = {}
    for spec in child_specs:
        by_action.setdefault(spec.action, []).append(spec)
    for action, specs in by_action.items():
        if len(specs) > 1:
            names = ", ".join(s.name for s in specs)
            logger.error(
                "obsidian-sync: refusing to wire %d %s subscriptions (%s); "
                "only one instance per action is supported per child",
                len(specs),
                action,
                names,
            )
            return 1

    projection_specs = by_action.get("obsidian-projection", [])
    status_transition_specs = by_action.get("obsidian-status-transition", [])
    priority_changed_specs = by_action.get("obsidian-priority-changed", [])
    due_date_changed_specs = by_action.get("obsidian-due-date-changed", [])
    project_context_projection_specs = by_action.get("project-context-projection", [])
    note_push_specs = by_action.get("note-push", [])
    task_archive_specs = by_action.get("task-archive", [])
    projection_spec = projection_specs[0] if projection_specs else None
    status_transition_spec = (
        status_transition_specs[0] if status_transition_specs else None
    )
    priority_changed_spec = (
        priority_changed_specs[0] if priority_changed_specs else None
    )
    due_date_changed_spec = (
        due_date_changed_specs[0] if due_date_changed_specs else None
    )
    project_context_projection_spec = (
        project_context_projection_specs[0]
        if project_context_projection_specs
        else None
    )
    note_push_spec = note_push_specs[0] if note_push_specs else None
    task_archive_spec = task_archive_specs[0] if task_archive_specs else None

    # status-transition / priority-changed / due-date-changed all need
    # the projection's ``sync_state`` populated for the fs watcher to
    # emit any events at all (the watcher silently skips tasks with no
    # projection-known baseline). Configuring any of them without
    # projection is permitted but inert — call that out at startup
    # rather than letting the operator wonder why their edits don't
    # push.
    for downstream_spec in (
        status_transition_spec,
        priority_changed_spec,
        due_date_changed_spec,
    ):
        if downstream_spec is not None and projection_spec is None:
            logger.warning(
                "obsidian-sync: %r is configured but no obsidian-projection "
                "subscription is present. The handler will load but never "
                "fire, because the projection is what populates the marker "
                "baseline the fs watcher reads against.",
                downstream_spec.name,
            )

    # The task-archive handler depends on the projection: the projection
    # populates ``sync_state.surfaced``, which is the archiver's
    # "was this task operator-visible" gate. Without it, nothing ever
    # sets the flag, so the archiver would skip every task.
    if task_archive_spec is not None and projection_spec is None:
        logger.warning(
            "obsidian-sync: %r is configured but no obsidian-projection "
            "subscription is present. The archiver will load but never "
            "fire, because the projection is what populates the "
            "surfaced-task set the archiver gates on.",
            task_archive_spec.name,
        )

    # note-push has a softer dependency than the task-side handlers:
    # without project-context-projection, the dir-watcher still
    # emits events for pre-existing on-disk docs once they've been
    # seeded on first-sight (subsequent body edits trip the
    # observed-hash diff), but two important properties degrade:
    #
    # 1. **No baseline pull.** The daemon won't project Lithos-side
    #    updates to local files, so canonical changes that happen
    #    via MCP / other agents silently diverge from disk. The next
    #    operator edit pushes their stale local body OVER those
    #    upstream changes (no conflict detected because the local
    #    ``lithos_version`` in frontmatter is still what Lithos has
    #    on its side as of when this file was last projected).
    #
    # 2. **First-sight skip.** A doc that's never been on disk
    #    before — e.g. a project created upstream via MCP — won't
    #    materialise locally without project-context-projection, so
    #    the operator can't push to it from Obsidian either.
    #
    # Both degraded modes are real failure modes for the operator;
    # the previous warning text claimed the handler "will never fire"
    # which is materially wrong (reviewer finding on PR #46). Reword
    # to describe the actual risk.
    if note_push_spec is not None and project_context_projection_spec is None:
        logger.warning(
            "obsidian-sync: %r is configured without project-context-projection. "
            "The dir watcher will still detect edits to pre-existing projected "
            "files on disk and push them upstream, BUT the daemon will not "
            "pull canonical Lithos-side changes back to your vault. Operator "
            "edits can overwrite upstream changes without surfacing a "
            "conflict. Enable project-context-projection for the full "
            "bidirectional contract.",
            note_push_spec.name,
        )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
            installed.append(sig)

    try:
        # The fs watcher's lifecycle is gated on ``[obsidian_sync]``
        # alone, not on whether a projection subscription is present.
        # Without projection state to compare against, the watcher's
        # per-task transition check never fires (every parsed task has
        # ``prior is None``) and no events are published — runtime
        # behaviour is identical to the previous "idle and warn" path,
        # but the source itself is independently spawnable so the
        # spawn gate doesn't have to be re-plumbed as more actions land.
        sync_state = ProjectionSyncState()
        bus = EventBus()
        fs_watcher = ObsidianFsWatcher(
            bus=bus,
            tasks_path=obs.vault_path / obs.tasks_file,
            sync_state=sync_state,
        )
        # Spawn the dir-watcher alongside the file-watcher. SAME
        # sync_state instance — the projection populates the per-doc
        # body-hash baseline that the dir-watcher reads against, and the
        # note-push handler updates sync_state after a successful push so
        # the dir-watcher absorbs the post-push frontmatter rewrite as a
        # self-write. All three see one coordinator state.
        dir_watcher = ObsidianDirWatcher(
            bus=bus,
            projects_root=obs.vault_path / obs.projects_dir,
            sync_state=sync_state,
        )
        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(fs_watcher.run(), name="obsidian-fs-watcher"),
            asyncio.create_task(dir_watcher.run(), name="obsidian-dir-watcher"),
        ]

        if not child_specs:
            logger.warning(
                "obsidian-sync: no obsidian-projection, "
                "obsidian-status-transition, obsidian-priority-changed, "
                "obsidian-due-date-changed, "
                "project-context-projection, note-push, or task-archive "
                "subscription configured; both watchers run but emit "
                "nothing without projection state. Add a [[subscriptions]] "
                "block with action='obsidian-projection' to populate it."
            )
            try:
                await stop_event.wait()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            return 0

        my_handlers: dict[str, Handler] = {}
        if projection_spec is not None:
            logger.info("obsidian-sync: wiring subscription %r", projection_spec.name)
            # 50ms debounce coalesces bursts of bus events (especially
            # the source's bootstrap, which can fire dozens of created
            # events in quick succession) into a single flush at
            # quiescence. The disk-seeded content-hash check then turns
            # a quiet-KB restart into zero on-disk writes.
            my_handlers["obsidian-projection"] = make_obsidian_projection_handler(
                cfg, debounce_seconds=0.05, sync_state=sync_state
            )
        if status_transition_spec is not None:
            logger.info(
                "obsidian-sync: wiring subscription %r",
                status_transition_spec.name,
            )
            my_handlers["obsidian-status-transition"] = (
                handle_obsidian_status_transition
            )
        if priority_changed_spec is not None:
            logger.info(
                "obsidian-sync: wiring subscription %r",
                priority_changed_spec.name,
            )
            my_handlers["obsidian-priority-changed"] = handle_obsidian_priority_changed
        if due_date_changed_spec is not None:
            logger.info(
                "obsidian-sync: wiring subscription %r",
                due_date_changed_spec.name,
            )
            my_handlers["obsidian-due-date-changed"] = handle_obsidian_due_date_changed
        if project_context_projection_spec is not None:
            logger.info(
                "obsidian-sync: wiring subscription %r",
                project_context_projection_spec.name,
            )
            my_handlers["project-context-projection"] = (
                make_project_context_projection_handler(cfg, sync_state=sync_state)
            )
        if note_push_spec is not None:
            logger.info(
                "obsidian-sync: wiring subscription %r",
                note_push_spec.name,
            )
            my_handlers["note-push"] = make_note_push_handler(
                cfg, sync_state=sync_state
            )
        if task_archive_spec is not None:
            logger.info("obsidian-sync: wiring subscription %r", task_archive_spec.name)
            # SAME sync_state instance: the projection sets surfaced[id]
            # (the archiver's gate) and reads archived[id] (set here) for
            # its flush-time eviction. No debounce — the archiver does a
            # single synchronous O_APPEND per event and never flushes.
            my_handlers["task-archive"] = make_task_archive_handler(
                cfg, sync_state=sync_state
            )

        # LithosClient is needed for both: the projection wires through
        # to LithosEventStream for upstream events; the status-transition
        # handler calls task_complete / task_cancel / finding_post.
        # LithosEventStream only spawns when projection is configured —
        # status-transition consumes obsidian-side events only. The
        # LithosNoteStream only spawns when the project-context-projection
        # subscription is configured — otherwise nothing would consume
        # its events.
        need_event_stream = projection_spec is not None
        need_note_stream = project_context_projection_spec is not None
        async with LithosClient(
            cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
        ) as lithos:
            ctx = SubscriptionContext(
                lithos=lithos,
                logger=logging.getLogger("lithos_loom.subscriptions"),
                agent_id=cfg.orchestrator.agent_id,
            )
            runners = build_runners(
                bus=bus,
                specs=child_specs,
                handlers=my_handlers,
                ctx=ctx,
            )

            if need_event_stream:
                events_url = cfg.orchestrator.lithos_url.rstrip("/") + "/events"
                # Pull resolved-task history into the bootstrap so the
                # TTL-lingering window survives daemon restart (PR #21
                # review issue 1). The source fetches
                # completed + cancelled tasks at bootstrap via Lithos's
                # server-side resolved_since filter (lithos#286) before
                # publishing them as terminal events.
                source = LithosEventStream(
                    client=lithos,
                    bus=bus,
                    events_url=events_url,
                    bootstrap_resolved_window=timedelta(days=obs.resolved_ttl_days),
                )
                tasks.append(
                    asyncio.create_task(source.run(), name="lithos-event-stream")
                )
            if need_note_stream:
                events_url = cfg.orchestrator.lithos_url.rstrip("/") + "/events"
                # Second SSE source for note lifecycle events.
                # Bootstraps via lithos_list(path_prefix=, tags=) so
                # cold restart re-projects every existing project-context
                # doc — the projection subscription's per-doc hash dedup
                # short-circuits the writes when nothing changed.
                note_source = LithosNoteStream(
                    client=lithos,
                    bus=bus,
                    events_url=events_url,
                )
                tasks.append(
                    asyncio.create_task(note_source.run(), name="lithos-note-stream")
                )
            tasks.extend(
                asyncio.create_task(r.run(), name=f"sub-{r.spec.name}") for r in runners
            )
            try:
                await stop_event.wait()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # Mirror the supervisor's install/uninstall pair so the test
        # process's event loop isn't left with handlers attached after
        # _amain returns (Copilot review on #16).
        for sig in installed:
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(sig)
        logger.info("obsidian-sync child stopping")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = load_config(args.config)
    _configure_logging(cfg.orchestrator.log_level)
    try:
        return asyncio.run(_amain(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
