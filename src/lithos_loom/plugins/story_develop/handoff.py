"""Handoff directory + filename helpers.

T1 only needs to seed the ``.handoff/`` directory (with ``FORMAT.md``) and name
the coder's per-round file. Structured-finding parsing/validation arrives in T2.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

_PROMPTS = "lithos_loom.plugins.story_develop.prompts"


def load_prompt(name: str) -> str:
    """Read a packaged prompt template (e.g. ``coder_init.md``)."""
    return resources.files(_PROMPTS).joinpath(name).read_text(encoding="utf-8")


def coder_handoff_name(round_no: int) -> str:
    """Filename for the coder's handoff in a given round (1-based)."""
    return f"round_{round_no:02d}_coder_done.md"


def seed_handoff_dir(handoff_dir: Path) -> Path:
    """Create *handoff_dir* and write ``FORMAT.md`` into it.

    *handoff_dir* lives outside the git worktree and is mounted into the
    container at ``/workspace/.handoff``. Returns the directory path.
    """
    handoff_dir.mkdir(parents=True, exist_ok=True)
    (handoff_dir / "FORMAT.md").write_text(load_prompt("FORMAT.md"), encoding="utf-8")
    return handoff_dir
