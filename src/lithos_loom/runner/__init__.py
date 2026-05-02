"""Salvaged helpers from Ralph++.

Lifted to ``lithos_loom/runner/`` and adapted for Loom's plugin contract.
The Ralph++ project is not retained as a runtime dependency.

Modules:

* :mod:`lithos_loom.runner.worktree` — per-task git worktree creation/removal
* :mod:`lithos_loom.runner.agents` — claude/codex subprocess + stream-json capture
* :mod:`lithos_loom.runner.git` — base SHA, commits-since, dirty-check
"""
