"""prd-decompose entry point.

Invoked by the daemon as::

    python -m lithos_loom.plugins.prd_decompose \\
        --task-json <path> --work-dir <path> --result-file <path>

Stub — see docs/prd/mvp.md US-12 / US-13.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Stub — implement per docs/prd/mvp.md US-12 / US-13."""
    raise NotImplementedError("prd-decompose plugin — implement per US-12 / US-13")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
