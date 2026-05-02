"""story-implement entry point.

Invoked by the daemon as::

    python -m lithos_loom.plugins.story_implement \\
        --task-json <path> --work-dir <path> --result-file <path>

Stub — see docs/prd/mvp.md US-10, US-14-17.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Stub — implement per docs/prd/mvp.md US-10, US-14-17."""
    raise NotImplementedError("story-implement plugin — implement per US-10, US-14-17")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
