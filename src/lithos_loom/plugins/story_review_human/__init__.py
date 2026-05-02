"""story-review-human plugin (US-11, US-18-20).

Polls ``gh pr view`` for the PR opened by ``story-implement``. On MERGED:
posts ``[ReviewMerged]`` and completes the task. On CLOSED without merge:
posts ``[ReviewRejected]`` and fails the task. Idempotent across daemon
restarts (re-polls the same PR URL).
"""
