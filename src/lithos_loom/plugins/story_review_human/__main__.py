"""story-review-human entry point.

Invoked by the daemon as::

    python -m lithos_loom.plugins.story_review_human \\
        --task-json <path> --work-dir <path> --result-file <path>

Stub — see docs/prd/mvp.md US-11, US-18-20.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Stub — implement per docs/prd/mvp.md US-11, US-18-20."""
    raise NotImplementedError(
        "story-review-human plugin — implement per US-11, US-18-20"
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
