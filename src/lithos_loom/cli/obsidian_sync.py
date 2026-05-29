"""``lithos-loom obsidian-sync`` sub-app.

Exposes the configured ``[obsidian_sync]`` block to operators and to
the capture macro. The macro needs the configured
``tasks_file`` (which is operator-configurable — see
``src/lithos_loom/config.py:188``) to build a wikilink pointing at
the right projection file, rather than hardcoding the default
``_lithos/tasks.md``. Without this command, a host that customises
``tasks_file`` would get the daemon writing to the configured path
while the macro inserts a dangling link to the default path.

Currently only exposes ``show``; the namespace gives room to grow
(``obsidian-sync doctor``, ``obsidian-sync stats``, etc.) without
proliferating top-level commands.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from lithos_loom.config import load_config
from lithos_loom.errors import LithosLoomError

obsidian_sync_app = typer.Typer(
    name="obsidian-sync",
    help="Inspect the [obsidian_sync] block from your TOML config.",
    no_args_is_help=True,
)


_FORMAT_TEXT = "text"
_FORMAT_JSON = "json"


@obsidian_sync_app.command("show")
def show(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path (overrides LITHOS_LOOM_CONFIG).",
    ),
    output_format: str = typer.Option(
        _FORMAT_TEXT,
        "--format",
        "-f",
        help="Output format: 'text' (key: value lines) or 'json' "
        "(single object). The capture macro uses 'json' to read the "
        "configured tasks_file at runtime.",
    ),
) -> None:
    """Print the resolved ``[obsidian_sync]`` block from the active config.

    Exits non-zero with a clear stderr message if the section is
    absent (the host doesn't run the obsidian-sync child) or if the
    config can't be loaded. JSON keys mirror the
    :class:`~lithos_loom.config.ObsidianSyncConfig` dataclass
    fields verbatim.
    """
    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: {exc}", err=True)
        sys.exit(1)

    obs = cfg.obsidian_sync
    if obs is None:
        typer.echo(
            "lithos-loom: [obsidian_sync] is not configured in the active "
            "config — this host doesn't project tasks to a vault",
            err=True,
        )
        sys.exit(1)

    data = {
        "vault_path": str(obs.vault_path),
        "tasks_file": str(obs.tasks_file),
        "projects_dir": str(obs.projects_dir),
        "resolved_ttl_days": obs.resolved_ttl_days,
        "include_blocked": obs.include_blocked,
        "exclude_tags": list(obs.exclude_tags),
    }

    if output_format == _FORMAT_JSON:
        typer.echo(json.dumps(data))
        return
    if output_format == _FORMAT_TEXT:
        for key, value in data.items():
            typer.echo(f"{key}: {value}")
        return
    typer.echo(
        f"lithos-loom: unknown --format {output_format!r} "
        f"(expected one of: {_FORMAT_TEXT}, {_FORMAT_JSON})",
        err=True,
    )
    sys.exit(2)
