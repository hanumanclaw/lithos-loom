"""story-implement plugin (US-10, US-14-17).

Claims a story task, creates a worktree off the integration branch, runs
Claude with PRD + story brief + project AGENTS.md context, detects new
commits, opens a GitHub PR, retags the task for human review.
"""
