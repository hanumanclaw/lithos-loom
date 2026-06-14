"""RouteRunner — claim-bound bus subscriber that runs plugins.

A special subscriber type sitting on the in-process :class:`EventBus`.
It listens for ``lithos.task.created`` / ``lithos.task.updated`` /
``lithos.task.released`` events whose tags match the route's
``RouteMatch.tags``, claims the task via Lithos, runs the configured plugin
subprocess, and applies the resulting status:

* ``status="succeeded"`` → ``task_complete`` (releases all claims)
* ``status="failed"`` → ``task_release`` + ``[BlockerFailed]`` finding
* ``status="interrupted"`` → ``task_release`` (no finding — operator
  signal, not an error). When the result also carries a ``resume`` block
  (``resume_after`` timestamp — e.g. a story-develop run checkpointed on a
  provider usage limit), the runner schedules an in-process re-dispatch:
  at ``resume_after`` it re-checks the task is still open, drops it from
  the dedup set, and re-claims + re-runs. Bounded by
  ``MAX_RESUMES_PER_TASK``; the schedule is in-memory only (a daemon
  restart re-bootstraps open tasks anyway).

The runner is instantiated directly by the route-runner child entry
point (one runner per route) — it does **not** go through the
``lithos_loom.subscriptions`` entry-point registry, because routes have
distinct semantics (claim-bound, plugin-driven) from the generic fire-
and-forget subscriptions that registry serves. Routes and subscriptions
share an internal type but are distinct TOML stanzas.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import shutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.config import RouteConfig
from lithos_loom.errors import LithosClientError, PluginContractError
from lithos_loom.plugin_runner import run_plugin

__all__ = ["PluginRunFn", "RouteRunner"]

logger = logging.getLogger(__name__)


PluginRunFn = Callable[..., Awaitable[Mapping[str, Any]]]
"""Signature ``run_plugin`` exposes; injectable for tests."""


_HANDLED_EVENT_TYPES = (
    "lithos.task.created",
    "lithos.task.updated",
    "lithos.task.released",
)
# `updated` is treated as "re-evaluate match," not "always run" (issue #86):
# adding a route's trigger tag to an already-open task arrives as
# `lithos.task.updated` and should dispatch without a daemon restart. The
# `_handle` guards below make this safe against self-triggering — a plugin's
# own end-of-run `task_update` (e.g. story-develop writing `develop_*`
# metadata) fires `updated`, but the task is already in `_processed_tasks`
# (claimed this process) so it's skipped. Before lithos#283 Lithos emitted no
# `updated` event at all, which is why the original two-tuple was complete.

# Re-dispatch budget for `interrupted` results carrying a `resume` block.
# Each resume re-runs the full plugin (container spin-up + agent spend), so
# a run that keeps hitting its provider limit must not retry unbounded —
# after this many resumes the task is left open with a [Friction] finding
# for the operator. Distinct from the failure retry budget (issue #11):
# resume is "try again after the limit lifts", not "retry a failure".
MAX_RESUMES_PER_TASK = 3


def _task_to_payload(task: Any) -> dict[str, Any]:
    """Build an event-shaped payload from a fresh :class:`Task` snapshot.

    Mirrors the fields ``_handle`` reads and the runner writes into
    ``task.json`` (id / status / title / description / tags / metadata), so a
    resumed run develops against the task's CURRENT content, not the snapshot
    captured when it was interrupted.
    """
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "description": task.description or "",
        "tags": list(task.tags),
        "metadata": dict(task.metadata),
    }


@dataclass
class RouteRunner:
    """One claim-bound subscriber per route.

    Attributes
    ----------
    route:
        The route configuration this runner serves. ``route.match.tags``
        becomes the bus filter; ``route.command`` is the plugin template;
        ``route.max_runtime_seconds`` caps each plugin invocation.
    bus:
        The in-process bus this runner subscribes against.
    lithos:
        A live :class:`lithos_loom.lithos_client.LithosClient` (or any
        object that quacks like one for tests).
    agent_id:
        The Lithos agent identity used for ``task_claim`` / ``task_renew``
        / ``task_release`` / ``task_complete`` / ``finding_post``.
    work_dir_base:
        Per-task staging directories are created at
        ``work_dir_base / <task_id>``.
    renew_interval_seconds:
        How often the renewer task calls ``task_renew``. Should be less
        than the claim TTL; defaults to 60s, matching the claim default.
    retain_failed_workdirs:
        When ``True`` (default), the work dir is left behind on plugin
        failure for operator inspection.
    plugin_runner:
        Injectable subprocess-runner. Defaults to the real
        :func:`lithos_loom.plugin_runner.run_plugin`. Tests inject an
        ``AsyncMock`` to bypass real subprocess work.
    project_repos:
        Map of project slug → on-disk repo path, from the host's
        ``[projects.*]`` TOML. A route command may carry a ``{{repo}}``
        token; the runner resolves it per task from this map keyed by
        ``task.metadata.project``, so one generic route can serve every
        registered project instead of baking an absolute path into the
        command. Empty by default — routes that don't use ``{{repo}}``
        don't need it.
    """

    route: RouteConfig
    bus: EventBus
    lithos: Any
    agent_id: str
    work_dir_base: Path
    renew_interval_seconds: float = 60.0
    retain_failed_workdirs: bool = True
    plugin_runner: PluginRunFn = field(default=run_plugin)
    project_repos: Mapping[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._subscription: Subscription = self.bus.subscribe(
            event_types=_HANDLED_EVENT_TYPES,
            match={"tags": list(self.route.match.tags)},
            name=f"route-runner-{self.route.name}",
        )
        # Tasks this runner has successfully claimed. Future events for
        # the same id are skipped — without this, multiple stale events
        # queued for the same open task would each run the plugin,
        # relying on Lithos's claim_failed envelope for safety. Real
        # Lithos enforces it, but the runner should too. This is also what
        # makes subscribing to `lithos.task.updated` (issue #86) safe: a
        # plugin's own end-of-run `task_update` fires `updated` for a task
        # we've already claimed this process, and that event is dropped here
        # rather than re-running the plugin.
        #
        # Important: this set is also what suppresses re-attempts after a
        # plugin failure. When the plugin fails we release the claim and
        # post a [BlockerFailed] finding; Lithos then emits
        # lithos.task.released; that event hits this dedup check and is
        # silently skipped. The effect is "fail once per task per daemon
        # process" — deliberate, to avoid tight retry loops when a plugin
        # is deterministically broken. A proper retry budget lives in
        # follow-up issue #11. The lost-claim-race path below
        # (claim_failed) deliberately does NOT add to this set, so a
        # subsequent released event there does re-attempt the claim.
        self._processed_tasks: set[str] = set()
        # T10: pending usage-limit re-dispatches (task id → sleeper task)
        # and how many resumes each task has consumed. In-memory only:
        # a daemon restart loses the schedule, but the event-stream
        # bootstrap re-surfaces open tasks on startup anyway.
        self._resume_tasks: dict[str, asyncio.Task[None]] = {}
        self._resume_counts: dict[str, int] = {}

    @property
    def subscription(self) -> Subscription:
        return self._subscription

    async def run(self) -> None:
        """Drain the subscription forever. Cancellable."""
        while True:
            event = await self._subscription.queue.get()
            try:
                await self._handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "RouteRunner %s: unhandled error processing %s",
                    self.route.name,
                    event.type,
                )

    # ── handle ────────────────────────────────────────────────────────

    async def _handle(self, event: Event) -> None:
        payload = event.payload
        task_id = str(payload.get("id") or "")
        if not task_id:
            return
        if payload.get("status") != "open":
            return  # nothing to do for terminal-state observations
        if task_id in self._processed_tasks:
            logger.debug(
                "RouteRunner %s: skipping stale event for already-processed %s",
                self.route.name,
                task_id,
            )
            return

        metadata = payload.get("metadata") or {}
        if not self.route.completes_task and metadata.get("loom_delivered"):
            # `loom_delivered` is restart-safety for THIS kind of route: a
            # completes_task=false route already delivered this task (PR raised,
            # awaiting merge) and left it open, so don't re-develop it — most
            # importantly on a daemon restart, where the bootstrap re-emits every
            # open task as `created`. The guard is gated on `not completes_task`
            # so the marker stays route-specific: a normal (completes_task=true)
            # route re-tagged onto the still-open task is NOT suppressed by it.
            # Completion (and the marker becoming moot) happens when the PR merges.
            logger.debug(
                "RouteRunner %s: skipping %s — already delivered (awaiting merge)",
                self.route.name,
                task_id,
            )
            return

        depends_on = metadata.get("depends_on") or []
        if depends_on and not await self._deps_satisfied(depends_on):
            logger.info(
                "RouteRunner %s: deferring %s — dependencies not complete",
                self.route.name,
                task_id,
            )
            return

        try:
            await self.lithos.task_claim(
                task_id=task_id, aspect=self.route.name, agent=self.agent_id
            )
        except LithosClientError as exc:
            if exc.code == "claim_failed":
                # Another runner won the race. Don't add to processed —
                # if they release the claim, the lithos.task.released
                # event will land here again and we'll re-attempt. This
                # is the only path where released triggers a re-claim;
                # for the won-claim-then-plugin-fail path, see the
                # comment on _processed_tasks above (issue #11).
                logger.debug(
                    "RouteRunner %s: lost claim race for %s",
                    self.route.name,
                    task_id,
                )
                return
            raise

        # Claim succeeded; remember so duplicate queued events for the same
        # task ID are skipped rather than racing into a second plugin run.
        self._processed_tasks.add(task_id)
        logger.info("RouteRunner %s: claimed %s", self.route.name, task_id)
        await self._run_claimed_task(task_id, payload)

    async def _deps_satisfied(self, dep_ids: list[str]) -> bool:
        # Use task_get (post-lithos#294) — only ``.status`` is read here,
        # so skipping claim serialization with the dedicated single-task
        # endpoint is a small per-call efficiency win, plus we get an
        # explicit task_not_found envelope instead of an empty-list miss.
        for dep_id in dep_ids:
            status = await self.lithos.task_get(task_id=dep_id)
            if status is None or status.status != "completed":
                return False
        return True

    # ── claimed-task lifecycle ────────────────────────────────────────

    def _resolve_command(self, payload: Mapping[str, Any]) -> str:
        """Substitute the optional ``{{repo}}`` token from the projects map.

        Resolution is keyed off ``task.metadata.project`` against the host's
        ``[projects.*]`` table, so the repo a plugin acts on is derived from
        the task's own project rather than hard-coded per route. Raises
        :class:`PluginContractError` when the token is present but
        unresolvable — the caller releases the claim with a finding (a
        misconfigured route + unroutable task is a config error, not a plugin
        failure). Routes without the token are returned unchanged.
        """
        command = self.route.command
        if "{{repo}}" not in command:
            return command
        metadata = payload.get("metadata") or {}
        slug = metadata.get("project") if isinstance(metadata, Mapping) else None
        if not isinstance(slug, str) or not slug:
            raise PluginContractError(
                "route command uses the {{repo}} token but the task has no "
                "metadata.project to resolve it against"
            )
        repo = self.project_repos.get(slug)
        if repo is None:
            raise PluginContractError(
                f"route command uses the {{repo}} token but project {slug!r} "
                "is not registered in [projects.*] on this host"
            )
        # shlex.quote: the resolved command is tokenised with shlex.split in
        # plugin_runner._build_argv, so a repo path containing spaces (or
        # shell metacharacters) must be quoted or it would split into several
        # argv elements and truncate --repo.
        return command.replace("{{repo}}", shlex.quote(str(repo)))

    async def _run_claimed_task(self, task_id: str, payload: Mapping[str, Any]) -> None:
        try:
            command = self._resolve_command(payload)
        except PluginContractError as exc:
            # Token present but unresolvable: release with a finding before
            # any work-dir / plugin spend, same as a contract violation.
            await self._release_with_finding(task_id, f"route misconfigured: {exc}")
            return

        work_dir = self.work_dir_base / task_id
        work_dir.mkdir(parents=True, exist_ok=True)
        task_json_path = work_dir / "task.json"
        result_file = work_dir / "result.json"
        task_json_path.write_text(json.dumps({"task": dict(payload)}))

        renew_task = asyncio.create_task(
            self._renew_loop(task_id), name=f"renew-{task_id}"
        )
        succeeded = False
        try:
            try:
                result = await self.plugin_runner(
                    command=command,
                    task_json_path=task_json_path,
                    work_dir=work_dir,
                    result_file=result_file,
                    max_runtime_seconds=self.route.max_runtime_seconds,
                )
            except PluginContractError as exc:
                await self._release_with_finding(
                    task_id,
                    f"plugin contract violation: {exc}",
                )
            except TimeoutError as exc:
                await self._release_with_finding(
                    task_id,
                    f"plugin exceeded max runtime: {exc}",
                )
            else:
                succeeded = await self._apply_result(task_id, result)
        finally:
            renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renew_task
            # Cleanup runs on every exit path — success, plugin failure,
            # contract violation, timeout, even cancellation. The flag
            # decides whether failed dirs are retained for inspection.
            self._cleanup_work_dir(work_dir, success=succeeded)

    async def _renew_loop(self, task_id: str) -> None:
        while True:
            await asyncio.sleep(self.renew_interval_seconds)
            try:
                await self.lithos.task_renew(
                    task_id=task_id, aspect=self.route.name, agent=self.agent_id
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "RouteRunner %s: renew failed for %s",
                    self.route.name,
                    task_id,
                )
                # Don't crash; next renewal attempt may succeed. If the
                # claim has fully expired, the next operation will fail
                # cleanly and the [BlockerFailed] finding will surface.

    async def _apply_result(
        self,
        task_id: str,
        result: Mapping[str, Any],
    ) -> bool:
        """Apply the plugin's result. Returns ``True`` iff the task succeeded."""
        status = result.get("status")
        if status == "succeeded":
            if self.route.completes_task:
                await self.lithos.task_complete(task_id=task_id, agent=self.agent_id)
                logger.info("RouteRunner %s: completed %s", self.route.name, task_id)
            else:
                # PR-producing route: success means a reviewed branch + PR
                # exist, awaiting human merge — NOT that the task is done.
                # Leave it open (release the claim) and mark it delivered so a
                # restart doesn't re-develop it. Completion happens on merge.
                await self._mark_delivered_and_release(task_id)
            return True
        if status == "failed":
            err = result.get("error") or {}
            err_msg = err.get("message") if isinstance(err, dict) else None
            await self._release_with_finding(
                task_id,
                f"plugin reported failure: {err_msg or 'no error message'}",
            )
            return False
        if status == "interrupted":
            # Release the claim either way: a shutdown signal frees the task
            # for a future run; a usage-limit checkpoint must not hold the
            # claim across the (potentially hours-long) wait. No
            # [BlockerFailed] finding — neither case is an error.
            with contextlib.suppress(Exception):
                await self.lithos.task_release(
                    task_id=task_id,
                    aspect=self.route.name,
                    agent=self.agent_id,
                )
            logger.info(
                "RouteRunner %s: released %s (plugin interrupted)",
                self.route.name,
                task_id,
            )
            # T10: a `resume` block makes the interruption retryable — the
            # plugin says WHEN a re-run is expected to succeed.
            resume = result.get("resume")
            if isinstance(resume, Mapping):
                await self._maybe_schedule_resume(task_id, resume)
            return False
        await self._release_with_finding(
            task_id,
            f"plugin returned unknown status {status!r}",
        )
        return False

    # ── usage-limit re-dispatch (T10) ─────────────────────────────────

    async def _maybe_schedule_resume(
        self,
        task_id: str,
        resume: Mapping[str, Any],
    ) -> None:
        raw = resume.get("resume_after")
        try:
            resume_after = datetime.fromisoformat(str(raw))
        except ValueError:
            logger.warning(
                "RouteRunner %s: %s has unparseable resume_after %r; not "
                "re-dispatching",
                self.route.name,
                task_id,
                raw,
            )
            return
        if resume_after.tzinfo is None:
            resume_after = resume_after.replace(tzinfo=UTC)
        used = self._resume_counts.get(task_id, 0)
        if used >= MAX_RESUMES_PER_TASK:
            logger.warning(
                "RouteRunner %s: %s exhausted its resume budget (%d); leaving open",
                self.route.name,
                task_id,
                MAX_RESUMES_PER_TASK,
            )
            with contextlib.suppress(Exception):
                await self.lithos.finding_post(
                    task_id=task_id,
                    summary=(
                        f"[Friction] route {self.route.name}: usage-limited run "
                        f"resume budget exhausted ({MAX_RESUMES_PER_TASK} "
                        "re-dispatches); the task stays open for the operator"
                    ),
                    agent=self.agent_id,
                )
            return
        self._resume_counts[task_id] = used + 1
        existing = self._resume_tasks.get(task_id)
        if existing is not None and not existing.done():
            existing.cancel()
        delay = max(0.0, (resume_after - datetime.now(UTC)).total_seconds())
        logger.info(
            "RouteRunner %s: scheduling re-dispatch of %s in %.0fs "
            "(resume %d/%d, at %s)",
            self.route.name,
            task_id,
            delay,
            used + 1,
            MAX_RESUMES_PER_TASK,
            resume_after.isoformat(timespec="seconds"),
        )
        sleeper = asyncio.create_task(
            self._resume_dispatch(task_id, delay),
            name=f"resume-{task_id}",
        )
        self._resume_tasks[task_id] = sleeper

        def _cleanup(done: asyncio.Task[None]) -> None:
            # Only remove the entry this task still owns: a cancelled old
            # sleeper's callback can fire AFTER a replacement was stored,
            # and must not evict the replacement.
            if self._resume_tasks.get(task_id) is done:
                del self._resume_tasks[task_id]

        sleeper.add_done_callback(_cleanup)

    async def _resume_dispatch(self, task_id: str, delay: float) -> None:
        """Sleep until ``resume_after``, then re-claim + re-run the task.

        The synthetic event is built from a FRESH ``task_get`` snapshot, not
        the payload captured when the run was interrupted: an operator may
        edit the task (body, acceptance criteria, reviewer override, deps)
        during the pause window, and the plugin re-reads all of that from the
        task.json the runner writes from this payload. Re-using the stale
        payload would silently develop against the old instructions.
        """
        try:
            await asyncio.sleep(delay)
            task = await self.lithos.task_get(task_id=task_id)
            if task is None or task.status != "open":
                logger.info(
                    "RouteRunner %s: %s no longer open at resume time; dropping",
                    self.route.name,
                    task_id,
                )
                return
            # Re-check the route's tag filter. The re-dispatch calls _handle
            # directly, bypassing the bus matcher that gates normal events, so
            # an operator who retagged the task during the pause window (e.g.
            # pulled the trigger tag to cancel it) would otherwise still get a
            # resumed run against a task that no longer matches this route.
            if not set(self.route.match.tags).issubset(set(task.tags)):
                logger.info(
                    "RouteRunner %s: %s no longer carries the route's trigger "
                    "tags at resume time; dropping",
                    self.route.name,
                    task_id,
                )
                return
            # Drop the dedup entry so _handle's claim path re-runs.
            self._processed_tasks.discard(task_id)
            await self._handle(
                Event(
                    type="loom.route.resume",
                    timestamp=datetime.now(UTC),
                    payload=_task_to_payload(task),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "RouteRunner %s: re-dispatch of %s failed",
                self.route.name,
                task_id,
            )

    async def _release_with_finding(self, task_id: str, detail: str) -> None:
        summary = f"[BlockerFailed] route {self.route.name}: {detail}"
        logger.info(
            "RouteRunner %s: releasing %s with finding: %s",
            self.route.name,
            task_id,
            detail,
        )
        try:
            await self.lithos.finding_post(
                task_id=task_id, summary=summary, agent=self.agent_id
            )
        except Exception:
            logger.exception(
                "RouteRunner %s: finding_post failed for %s",
                self.route.name,
                task_id,
            )
        try:
            await self.lithos.task_release(
                task_id=task_id, aspect=self.route.name, agent=self.agent_id
            )
        except Exception:
            logger.exception(
                "RouteRunner %s: task_release failed for %s",
                self.route.name,
                task_id,
            )

    async def _mark_delivered_and_release(self, task_id: str) -> None:
        """Leave a delivered task open for merge (``completes_task=false``).

        Marks ``metadata.loom_delivered`` so a restart's bootstrap won't
        re-develop it, then releases the claim (don't hold it across a
        potentially long wait for a human merge). The work exists on the
        branch + PR regardless, so neither step is fatal — but a failure of
        either has an operational consequence (a missing marker re-opens the
        duplicate-PR-on-restart hazard; a stuck claim can block other
        runners), so on failure we post a ``[Friction]`` finding rather than
        only logging, making the degraded state visible in Lithos.
        """
        marked = True
        try:
            await self.lithos.task_update(
                task_id=task_id,
                agent=self.agent_id,
                metadata={"loom_delivered": True},
            )
        except Exception:
            marked = False
            logger.exception(
                "RouteRunner %s: marking %s delivered failed", self.route.name, task_id
            )
        released = True
        try:
            await self.lithos.task_release(
                task_id=task_id, aspect=self.route.name, agent=self.agent_id
            )
        except Exception:
            released = False
            logger.exception(
                "RouteRunner %s: task_release failed for %s", self.route.name, task_id
            )
        if marked and released:
            logger.info(
                "RouteRunner %s: delivered %s — left open for human merge",
                self.route.name,
                task_id,
            )
            return
        problems: list[str] = []
        if not marked:
            problems.append(
                "could not set metadata.loom_delivered — a daemon restart may "
                "re-develop this task into a duplicate PR; merge the PR or set "
                "the marker manually"
            )
        if not released:
            problems.append(
                "could not release the claim — it will linger until its TTL "
                "expires, briefly blocking other runners"
            )
        summary = (
            f"[Friction] route {self.route.name}: delivered task (PR raised) but "
            + "; ".join(problems)
        )
        with contextlib.suppress(Exception):
            await self.lithos.finding_post(
                task_id=task_id, summary=summary, agent=self.agent_id
            )

    def _cleanup_work_dir(self, work_dir: Path, *, success: bool) -> None:
        if success or not self.retain_failed_workdirs:
            with contextlib.suppress(OSError):
                shutil.rmtree(work_dir)
