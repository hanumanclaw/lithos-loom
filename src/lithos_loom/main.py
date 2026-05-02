"""Top-level CLI dispatcher for the ``lithos-loom`` binary.

Subcommands per ``docs/PLAN.md`` and ``docs/prd/mvp.md``:

* ``lithos-loom run`` — start the daemon (poll loop)
* ``lithos-loom doctor`` — verify Lithos connectivity (US-35)
* ``lithos-loom validate-config`` — typecheck the TOML config (A1)
* ``lithos-loom config --show`` — print the merged effective config (US-4)
* ``lithos-loom --dry-run`` — preview matched tasks (US-5a)

Implementations live in :mod:`lithos_loom.daemon`, :mod:`lithos_loom.config`,
and :mod:`lithos_loom.route`. This module is the dispatcher only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from lithos_loom.config import load_config
from lithos_loom.errors import LithosLoomError

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
        help="Preview matched tasks; no claims or writes (US-5a).",
    ),
) -> None:
    """Start the daemon: poll Lithos, claim matching tasks, dispatch plugins."""
    cfg = _load_or_exit(config)
    if dry_run:
        raise NotImplementedError("dry-run mode — implement per docs/prd/full.md US-5a")
    raise NotImplementedError(
        "daemon.run(cfg) — implement per docs/prd/mvp.md US-5 "
        f"(loaded {cfg.source_path})"
    )


@app.command()
def doctor(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path.",
    ),
) -> None:
    """Verify Lithos connectivity and ``task.metadata`` support (US-35)."""
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
) -> None:
    """Typecheck the TOML against the route schema, surface unknown plugins."""
    cfg = _load_or_exit(config)
    typer.echo(f"OK: {cfg.source_path}")
    typer.echo(f"  orchestrator.agent_id: {cfg.orchestrator.agent_id}")
    typer.echo(f"  orchestrator.lithos_url: {cfg.orchestrator.lithos_url}")
    typer.echo(f"  projects: {sorted(cfg.projects)}")
    typer.echo(f"  routes: {[r.name for r in cfg.routes]}")
    if cfg.environment:
        typer.echo(f"  environment: {cfg.environment}")


@app.command("config")
def show_config(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path.",
    ),
    show: bool = typer.Option(
        False, "--show", help="Print the merged effective config (US-4)."
    ),
) -> None:
    """Inspect the loaded configuration."""
    if not show:
        typer.echo("Use --show to print the merged effective config.")
        raise typer.Exit(2)
    cfg = _load_or_exit(config)
    # A pretty-printer per US-4 lands when daemon impl does; for now, repr.
    typer.echo(repr(cfg))


def _load_or_exit(config: Path | None):
    try:
        return load_config(config)
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    app()
