"""Bundled plugins shipped with lithos-loom.

Each plugin is a standalone subprocess invoked by the daemon with the
contract::

    python -m lithos_loom.plugins.<name> \\
        --task-json <path> --work-dir <path> --result-file <path>

See ``docs/prd/mvp.md`` for plugin-specific behaviour.
"""
