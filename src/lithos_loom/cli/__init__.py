"""CLI sub-apps for the capture-macro surface and other operator
surfaces. Kept separate from :mod:`lithos_loom.main` so ``main.py``
stays scannable as more subcommand groups land."""

from __future__ import annotations

from lithos_loom.cli.obsidian_sync import obsidian_sync_app
from lithos_loom.cli.project import project_app
from lithos_loom.cli.task import task_app

__all__ = ["obsidian_sync_app", "project_app", "task_app"]
