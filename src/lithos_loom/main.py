"""Top-level CLI dispatcher for the ``lithos-loom`` binary.

Subcommands per ``docs/PLAN.md`` and ``docs/prd/integration.md`` Slice 0:

* ``lithos-loom run`` — start the daemon (supervisor + child processes)
* ``lithos-loom doctor`` — verify Lithos connectivity (deferred US-35)
* ``lithos-loom validate-config`` — typecheck the TOML config
* ``lithos-loom validate-config --dry-run`` — also poll Lithos and print
  which routes / subscriptions would fire for each open task (Slice 0 US6)
* ``lithos-loom config --show`` — print the merged effective config
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import typer

from lithos_loom.bus import Event, EventBus
from lithos_loom.config import (
    LoomConfig,
    RouteConfig,
    SubscriptionConfig,
    load_config,
)
from lithos_loom.errors import LithosLoomError
from lithos_loom.lithos_client import LithosClient, Task
from lithos_loom.subscriptions import (
    SubscriptionContext,
    build_runners,
    discover_handlers,
)
from lithos_loom.supervisor import Supervisor, default_categories

app = typer.Typer(
    name="lithos-loom",
    help="Workflow orchestration daemon for Lithos tasks.",
    no_args_is_help=True,
    add_completion=True,
)


@app.command()
def run(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path (overrides LITHOS_LOOM_CONFIG).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview matched tasks; no claims or writes.",
    ),
) -> None:
    """Start the daemon: poll Lithos, claim matching tasks, dispatch plugins."""
    cfg = _load_or_exit(config)
    if dry_run:
        # `lithos-loom run --dry-run` is shorthand for the dedicated
        # validate-config subcommand below, which is the canonical home
        # for the simulation logic. Forward and exit with its code.
        raise typer.Exit(_run_dry_run(cfg))
    sup = Supervisor(cfg, default_categories())
    exit_code = asyncio.run(sup.run())
    raise typer.Exit(exit_code)


@app.command()
def doctor(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path.",
    ),
) -> None:
    """Verify Lithos connectivity and ``task.metadata`` support (deferred)."""
    cfg = _load_or_exit(config)
    raise NotImplementedError(
        "doctor.run(cfg) — implement per docs/prd/mvp.md US-35 "
        f"(loaded {cfg.source_path})"
    )


@app.command("validate-config")
def validate_config(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help=(
            "Connect to Lithos and simulate routing against the current "
            "open-task list; print which routes/subscriptions would fire. "
            "Non-mutating."
        ),
    ),
) -> None:
    """Typecheck the TOML; with ``--dry-run`` also simulate routing."""
    cfg = _load_or_exit(config)
    typer.echo(f"OK: {cfg.source_path}")
    typer.echo(f"  orchestrator.agent_id: {cfg.orchestrator.agent_id}")
    typer.echo(f"  orchestrator.lithos_url: {cfg.orchestrator.lithos_url}")
    typer.echo(f"  projects: {sorted(cfg.projects)}")
    typer.echo(f"  routes: {[r.name for r in cfg.routes]}")
    typer.echo(f"  subscriptions: {[s.name for s in cfg.subscriptions]}")
    if cfg.environment:
        typer.echo(f"  environment: {cfg.environment}")
    if dry_run:
        raise typer.Exit(_run_dry_run(cfg))


@app.command("config")
def show_config(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path.",
    ),
    show: bool = typer.Option(
        False, "--show", help="Print the merged effective config."
    ),
) -> None:
    """Inspect the loaded configuration."""
    if not show:
        typer.echo("Use --show to print the merged effective config.")
        raise typer.Exit(2)
    cfg = _load_or_exit(config)
    typer.echo(repr(cfg))


def _load_or_exit(config: Path | None) -> LoomConfig:
    try:
        return load_config(config)
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: {exc}", err=True)
        sys.exit(1)


# ── --dry-run simulation (Slice 0 US6) ─────────────────────────────────


def _run_dry_run(cfg: LoomConfig) -> int:
    """Execute the dry-run simulation and return a CLI exit code."""
    try:
        return asyncio.run(_dry_run_async(cfg))
    except OSError as exc:
        typer.echo(
            f"lithos-loom: could not reach Lithos at "
            f"{cfg.orchestrator.lithos_url} ({exc}); "
            "run `lithos-loom doctor` to diagnose connectivity",
            err=True,
        )
        return 2
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: dry-run failed: {exc}", err=True)
        return 1


async def _dry_run_async(cfg: LoomConfig) -> int:
    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        tasks = await client.task_list(status="open", with_claims=True)
        dep_status = await _resolve_dep_statuses(client, tasks)
    _print_dry_run_report(cfg, tasks, dep_status)
    return 0


async def _resolve_dep_statuses(
    client: Any, tasks: list[Task]
) -> dict[str, str | None]:
    """Resolve the status of every ``metadata.depends_on`` referenced by ``tasks``.

    Mirrors what :class:`~lithos_loom.subscriptions.route_runner.RouteRunner`
    does at runtime: ``task_status`` per unique dep id. Returns the status
    string (``"completed"`` / ``"open"`` / ``"cancelled"``) or ``None`` for
    ``task_not_found``. Without this the dry-run would report ``✓ (claim)``
    for tasks the real runner would defer because their deps aren't done.
    """
    dep_ids: set[str] = set()
    for task in tasks:
        for dep_id in task.metadata.get("depends_on") or []:
            if isinstance(dep_id, str) and dep_id:
                dep_ids.add(dep_id)
    statuses: dict[str, str | None] = {}
    for dep_id in dep_ids:
        result = await client.task_status(task_id=dep_id)
        statuses[dep_id] = result.status if result is not None else None
    return statuses


def _print_dry_run_report(
    cfg: LoomConfig,
    tasks: list[Task],
    dep_status: dict[str, str | None],
) -> int:
    """Emit the dry-run table + orphan / dead-config summary."""
    typer.echo("")
    typer.echo("── Dry-run simulation ──────────────────────────────────")
    typer.echo(f"  open tasks:     {len(tasks)}")
    typer.echo(f"  routes:         {len(cfg.routes)}")
    typer.echo(f"  subscriptions:  {len(cfg.subscriptions)}")
    typer.echo("")

    fired_routes: set[str] = set()
    fired_subs: set[str] = set()
    orphan_tasks: list[Task] = []

    sub_predicates = _build_subscription_predicates(cfg.subscriptions)

    if not tasks:
        typer.echo("  (no open tasks; nothing to simulate)")
    for task in tasks:
        any_match = False
        title_summary = f"{task.id}  {task.title!r}"
        typer.echo(title_summary)
        for route in cfg.routes:
            would_fire, defer_reason = _route_outcome(route, task, dep_status)
            if would_fire:
                marker = "✓ (claim)"
            elif defer_reason:
                marker = f"deferred ({defer_reason})"
            else:
                marker = "—"
            typer.echo(f"    route:{route.name:<30} {marker}")
            if would_fire:
                fired_routes.add(route.name)
                any_match = True
        for spec in cfg.subscriptions:
            would_fire = sub_predicates[spec.name](task)
            marker = "✓ (would fire)" if would_fire else "—"
            typer.echo(f"    subscription:{spec.name:<23} {marker}")
            if would_fire:
                fired_subs.add(spec.name)
                any_match = True
        if not any_match:
            orphan_tasks.append(task)

    typer.echo("")
    typer.echo("── Summary ─────────────────────────────────────────────")
    if orphan_tasks:
        typer.echo(f"  orphan tasks ({len(orphan_tasks)}):")
        for task in orphan_tasks:
            typer.echo(f"    {task.id}  {task.title!r}")
    else:
        typer.echo("  no orphan tasks")

    dead_routes = [r.name for r in cfg.routes if r.name not in fired_routes]
    dead_subs = [s.name for s in cfg.subscriptions if s.name not in fired_subs]
    if dead_routes:
        typer.echo(f"  dead routes ({len(dead_routes)}):")
        for name in dead_routes:
            typer.echo(f"    {name}")
    if dead_subs:
        typer.echo(f"  dead subscriptions ({len(dead_subs)}):")
        for name in dead_subs:
            typer.echo(f"    {name}")
    if not dead_routes and not dead_subs:
        typer.echo("  no dead config (every route + subscription matched ≥1 task)")

    return 0


def _route_outcome(
    route: RouteConfig,
    task: Task,
    dep_status: dict[str, str | None],
) -> tuple[bool, str | None]:
    """Mirror :class:`RouteRunner` exactly: status + tags + deps gate.

    Returns ``(would_fire, defer_reason)``. The defer reason is non-None
    when the tag filter passes but dependencies are not yet completed —
    the operator should see "deferred" not just "—" so the difference
    between "doesn't match" and "matches-but-blocked" is visible.
    """
    if task.status != "open":
        return False, None
    if not set(route.match.tags).issubset(set(task.tags)):
        return False, None
    pending = _pending_deps(task, dep_status)
    if pending:
        return False, f"deps not complete: {', '.join(sorted(pending))}"
    return True, None


def _pending_deps(task: Task, dep_status: dict[str, str | None]) -> list[str]:
    deps = task.metadata.get("depends_on") or []
    return [
        str(dep_id)
        for dep_id in deps
        if isinstance(dep_id, str) and dep_status.get(dep_id) != "completed"
    ]


def _build_subscription_predicates(
    subs: Iterable[SubscriptionConfig],
) -> dict[str, Any]:
    """Compile each subscription into a callable ``(task) -> bool`` predicate.

    Uses :func:`build_runners` so the dry-run uses exactly the matcher
    machinery the runtime would — same structural-match semantics, same
    where-expression scope, same handler-action validation.
    """
    handlers = discover_handlers()
    bus = EventBus()
    ctx = SubscriptionContext(
        lithos=None,  # never invoked: dry-run does not dispatch handlers
        logger=logging.getLogger("lithos_loom.dry_run"),
        agent_id="dry-run",
    )
    runners = build_runners(bus=bus, specs=tuple(subs), handlers=handlers, ctx=ctx)
    sub_to_test: dict[str, Any] = {}
    for runner in runners:
        sub = runner.subscription

        def _predicate(task: Task, sub_local: Any = sub) -> bool:
            # A subscription "would fire" for this task iff there is at
            # least one event type in its on-list whose synthetic event
            # for this task passes the structural match + where predicate.
            # Hard-coding type="lithos.task.created" would silently report
            # `on = "lithos.task.updated"` subscriptions as never firing.
            payload = MappingProxyType(
                {
                    "id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "tags": list(task.tags),
                    "metadata": dict(task.metadata),
                    "claims": [dict(c) for c in task.claims],
                }
            )
            timestamp = datetime.now(UTC)
            for event_type in sub_local.event_types:
                evt = Event(type=event_type, timestamp=timestamp, payload=payload)
                if sub_local.matches(evt):
                    return True
            return False

        sub_to_test[runner.spec.name] = _predicate
    return sub_to_test


if __name__ == "__main__":
    app()
