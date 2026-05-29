---
title: Lithos Loom — Per-Project Task Archive
milestone: Track 1 (post-Slice 5 enhancement)
status: draft
target_version: 0.2.0
references:
  - docs/prd/integration.md (Track 1 PRD — Slices 0–5)
  - docs/PLAN.md (architectural decisions D1–D22)
  - /home/dns/.claude/plans/make-a-plan-to-frolicking-sonnet.md (D23–D30, Slices 4+5 plan)
labels: [needs-triage, lithos-loom, obsidian, archive, projection]
---

# Lithos Loom — Per-Project Task Archive

## Problem Statement

Slice 1's task projection writes every human-actionable Lithos task into a single global file (`_lithos/tasks.md`). Slice 1's resolved-task TTL (D20) keeps completed and cancelled tasks visible there for 7 days before dropping them. Two real frictions with this model:

- **No durable per-project record of completed work.** Once a task ages past `resolved_ttl_days`, it's gone from the operator's vault entirely. The history exists in Lithos but isn't queryable from Obsidian, and grep-against-the-vault for "what did I ship for project X" returns nothing.
- **The global file's resolved-task lingering is operator noise.** The Tasks-plugin daily / inbox queries filter on `status.type is TODO`, which naturally hides `[x]` and `[-]` lines, so the lingering is invisible in the operator's primary view. But it inflates the file, and any "look at recent done work" query has to remember to scope to project, which is exactly the cross-cut a per-project archive makes free.

A separate per-project completed-tasks file fixes both: persistence becomes operator-visible at the project layer, and the global file can drop completed work immediately.

## Solution

A new `task-archive` subscription that appends a Tasks-plugin-compatible line to `<vault>/_lithos/projects/<slug>/<slug>-done.md` whenever a human-surfaced Lithos task transitions to `completed` or `cancelled`. Companion change to the existing `obsidian-projection` subscription: immediately evict resolved tasks from the global file (rather than TTL-lingering) once they've been archived.

Per-project done files are **vault-only and append-only** — the live event path never rewrites them. They are not Lithos-canonical and have no upstream representation; the dir-watcher excludes them by suffix so operator edits are inert. The archive is a one-way operator-visible artifact derived from Lithos events. (An operator can rebuild one on demand — writing *all* resolved tasks, not the surfaced-only set — with `lithos-loom project regenerate-done`; see [`docs/cli/project-regenerate-done.md`](../cli/project-regenerate-done.md).)

### Flow per event

```
Lithos KB
  └─> task.completed / task.cancelled SSE
      └─> task-archive subscription
          ├─> filter: was this task ever surfaced to the human via the global projection?
          │     (in-memory `surfaced[task_id]` flag, set by the obsidian-projection
          │      handler whenever it writes a task line into `_lithos/tasks.md`)
          ├─> resolve target file from `task.metadata.project`
          │     (fallback to _unassigned bucket if missing / unknown)
          ├─> renderer reuses `render.render_line` (already drops priority + due-date
          │     markers on terminal status; emits `✅ <date>` / `❌ <date>` correctly)
          ├─> dedup against on-disk done file (read on first event per project; cache in memory)
          ├─> O_APPEND single-syscall write to <vault>/_lithos/projects/<slug>/<slug>-done.md
          └─> set `sync_state.archived[task_id] = True` so projection's next flush evicts
              the task from the global file
```

## Locked Design Decisions

| # | Area | Decision |
|---|------|----------|
| D31 | Per-project done file path | `<vault>/_lithos/projects/<slug>/<slug>-done.md` — alongside the project-context doc; Slice 5 dir-watcher excludes `-done.md` suffix from its walk so operator edits to archive files are inert |
| D32 | Global-file lifecycle for resolved tasks | **Immediate evict** once archived — no TTL lingering. `resolved_ttl_days` config knob is repurposed as the bootstrap-replay window for the archiver (same semantic, different consumer) |
| D33 | Cancelled tasks | Archived to the same per-project done file as completed tasks; renderer emits `- [-] ... ❌ <date>` lines. Tasks plugin distinguishes `[x]` vs `[-]` so operator queries can filter |
| D34 | Cold-start replay | On daemon start, fetch tasks resolved within the (repurposed) `resolved_ttl_days` window via the existing `task_list(resolved_since=...)` path; dedup against task ids already on disk by lazily reading each project's done file on first event for that project |
| D35 | Reopen-request flow | Killed under immediate-evict. D17's `[x]→[ ]` reopen-request handler becomes dead code on the global file (the line vanishes within ~250ms of completion). Reopen is via Lithos directly, awaiting upstream `lithos_task_reopen` (`agent-lore/lithos#243`). The dir-watcher's `-done.md` exclusion means unticks in archive files are also inert |
| D36 | Missing `metadata.project` | Fallback to `<vault>/_lithos/projects/_unassigned/_unassigned-done.md`. Never lose archive lines because of metadata drift; operator can grep / re-categorise later |
| D37 | Done file shape | Bare append-only Tasks-plugin lines, no header, no frontmatter. Simplest first-write logic; queries don't need a header to match `- [x]` / `- [-]` patterns; operator can `cat` / grep cleanly |
| D38 | "Surfaced to human" detection | In-memory flag `surfaced[task_id] = True` set by `obsidian-projection`'s render path whenever it writes a task line. On `task.completed` / `task.cancelled`, check the flag — if unset, the task was never operator-visible (background / route-claimed only) and we skip the archive. Acceptable lossiness: tasks that completed during a daemon-down window aren't in the flag set on replay (the projection rebuilds from scratch on bootstrap; tasks currently in the global file get re-flagged as surfaced before replay events fire) |
| D39 | Archive-then-evict coupling | The projection's evict predicate reads `sync_state.archived[task_id]`. The archiver sets the flag *after* the O_APPEND succeeds; the projection flushes globally, finding the flag, dropping the line. Failure of the archive write leaves the flag unset → the task stays in the global file with `[x]` / `[-]`. No data-loss window |

## Relationship to Existing Track 1 Architecture

This feature **extends** Slice 1's `obsidian-projection` and adds one new subscription. It does not introduce new sources, new event types, or new Lithos-side dependencies. Specifically:

- **Reuses** `lithos.task.completed` / `lithos.task.cancelled` events already published by `LithosEventStream` (Slice 1).
- **Reuses** `render.render_line` for the appended line (already drops priority + due-date on terminal status, uses `task.resolved_at` for the date marker).
- **Reuses** `task_list(resolved_since=...)` bootstrap path the projection already runs at startup.
- **Reuses** the Slice 5 `ObsidianDirWatcher` exclusion pattern (extends the file-suffix filter list).
- **Reuses** the Slice 4 `sync_state` shape (adds two maps: `surfaced` and `archived`).

No Lithos upstream changes required.

## User Stories

One vertical slice. Each story is a commit-or-two-sized increment.

### Slice 6 — Per-project task archive

39. As an operator, I want completed Lithos tasks I had surfaced in `_lithos/tasks.md` to be appended to `<vault>/_lithos/projects/<slug>/<slug>-done.md` as Tasks-plugin-compatible `- [x] ... ✅ <date>` lines, so that my vault carries a durable per-project history I can grep / query with Dataview.

40. As an operator, I want cancelled Lithos tasks I had surfaced to be appended to the same per-project file as `- [-] ... ❌ <date>` lines, so that I can run "tasks cancelled this quarter for project X" queries without filtering on a different file.

41. As an operator, I want the archiver to skip tasks that were never written into the global projection (automated / route-claimed-only / non-human-actionable), so that the archive carries the same "operator-visible work" semantic the global file does — no noise from background automation.

42. As an operator, I want completed and cancelled tasks evicted from `_lithos/tasks.md` immediately once they've been archived, so that the global file is purely "what's still actionable" and the archive is the durable record.

43. As an operator, I want tasks with missing or unknown `metadata.project` archived to `<vault>/_lithos/projects/_unassigned/_unassigned-done.md`, so that metadata drift never silently drops archive lines.

44. As an operator, I want the daemon to catch up tasks completed during downtime via the existing `resolved_since` bootstrap window, de-duplicated against task ids already on disk in each project's done file, so that a restart never misses or doubles archive entries.

45. As an operator, I want the Slice 5 dir-watcher to skip files ending in `-done.md` so that my untick / edit operations on archive files don't trigger spurious `note-push` events or reopen-request findings.

46. As an operator, I want the `resolved_ttl_days` config knob's semantic repurposed as the archiver's bootstrap-replay window (rather than the now-vestigial global-file lingering window), so that there's no new config surface to learn and migrations from Slice 1 are transparent.

## Implementation Decisions

### Modules to build / extend

- **`lithos_loom.subscriptions._task_archive`** (new) — stateful handler factory `make_handler(cfg, *, sync_state)`. Consumes `lithos.task.completed` and `lithos.task.cancelled`. Per-project lazy done-file scan on first event for that project (build dedup set). Resolves target file from `task.metadata.project` with `_unassigned` fallback. Calls `render.render_line` for the appended line. O_APPEND write. Sets `sync_state.archived[task_id] = True`.

- **`lithos_loom.subscriptions._obsidian_projection`** (extend) — `make_handler` already iterates open + recently-resolved tasks. Two changes:
  - Hook the render path: whenever a task line is written, set `sync_state.surfaced[task_id] = True`.
  - Change the resolved-task projection predicate from `resolved_at >= now - resolved_ttl_days` to `not sync_state.archived[task_id]`. Net effect: tasks linger in global until archived (which is the same poll cycle); never linger past that.

- **`lithos_loom.sources.obsidian_dir_watcher`** (extend) — add `-done.md` suffix to the exclusion check that the watcher's `_poll_one_file` already runs against `_unassigned` / dotfile patterns. One-line change.

- **`lithos_loom.sync_state.ProjectionSyncState`** (extend) — two new maps:
  - `surfaced: dict[str, bool]` — set by projection's render path, read by archiver's filter. Cleared on archive (drop `surfaced[task_id]` after archive succeeds; memory grows bounded).
  - `archived: dict[str, bool]` — set by archiver after O_APPEND success, read by projection's evict predicate. Persists for the life of the process (the projection's bootstrap re-evaluates every task, so the flag is consulted indefinitely).

- **`lithos_loom.children.obsidian_sync`** (extend) — register `task-archive` in `_CHILD_ACTIONS` and wire `make_handler` with the shared `sync_state` instance.

- **`examples/lithos-loom.toml`** (extend) — commented `[[subscriptions]]` stanza for `task-archive`. Note the repurposed `resolved_ttl_days` semantic in the `[obsidian_sync]` comment.

### Subscription handler shape

```toml
[[subscriptions]]
name = "task-archive"
on = ["lithos.task.completed", "lithos.task.cancelled"]
action = "task-archive"
on_persistent_failure = "friction"
[subscriptions.retry]
attempts = 5
backoff = "exponential"
initial_delay_seconds = 0.5
max_delay_seconds = 30.0
```

### Renderer reuse

`render.render_line(task, routes, today)` already returns the correct shape for terminal-status tasks:
- `- [x] <title> 🆔 lithos:<id> ✅ <resolved_at>` for completed
- `- [-] <title> 🆔 lithos:<id> ❌ <resolved_at>` for cancelled
- Priority emoji and due-date markers are dropped on terminal status (already exercised by Slice 1's TTL-lingering tests)

The archiver calls this verbatim. No new render path.

## Open Questions (Deferred)

1. **Sort order in done files.** Append-only = chronological by daemon-processing order, not by Lithos `resolved_at`. Bootstrap-replay tasks land first (in `resolved_since` order), then live events arrive in real-time order. Probably fine — operators query via Dataview / grep, not by reading top-to-bottom. Revisit if soak surfaces a real friction.

2. **Operator-deleted done file.** ~~Append-only + no regeneration means deleting the file loses archived history for that project.~~ **Resolved:** `lithos-loom project regenerate-done --slug <slug>` rebuilds the file from Lithos (all resolved tasks for the slug). New completions also re-append going forward. See [`docs/cli/project-regenerate-done.md`](../cli/project-regenerate-done.md).

3. **Long-running `surfaced` map memory pressure.** Cleared on archive but a long-uptime daemon accumulates entries for tasks that never reach a terminal state (still-open tasks indefinitely). Cap is the open-task count, which is bounded; not a real concern. Revisit if soak shows growth.

4. **Multi-doc-per-project done files.** Operators with multiple docs per project (`<slug>-project-context.md`, `architecture.md`, etc.) will see a `<slug>-done.md` file alongside. No special handling needed; it just sits there with the others. Consider whether the project-context doc should auto-link to the done file once it exists.

## Verification

After implementation, soak against staging Lithos with the slice-5-test config extended with the new subscription:

1. **Live completion.** Tick a projected task in Obsidian. Within ~250ms: `task-archive: appended <id> to <slug>-done.md`, line appears in the per-project done file, and the line disappears from `_lithos/tasks.md` on the next projection flush.

2. **Live cancellation.** Cancel-mark (`[-]`) a projected task. Same shape, but `- [-] ... ❌ <date>` line.

3. **Background task completion.** Drive a task to completion via MCP that was never human-actionable. Confirm: NO archive line written (filter via `surfaced` flag).

4. **Cold-start replay.** Stop the daemon. Complete a task via MCP (Lithos broadcasts `task.completed`). Restart the daemon. Confirm the archive line appears within the bootstrap window, and a second restart doesn't double-append (dedup against on-disk done file).

5. **Missing `metadata.project` fallback.** Manually create a task in Lithos without a `project` metadata field, surface it (open + human-blocking route), complete it. Archive line lands in `_unassigned-done.md`.

6. **Dir-watcher exclusion.** Edit a `-done.md` file in Obsidian (untick, modify body, etc.). Confirm: NO `obsidian.note.modified` event, NO push, NO reopen-request finding.

7. **Global immediate evict.** Confirm tasks no longer linger in `_lithos/tasks.md` for any time after completion — the eviction is on the next flush (≤ 50ms debounce).

8. **Archive write failure.** Simulate (chmod the projects directory non-writable, or stuff the file with a permission flip). Confirm the task stays `[x]` in the global file (`sync_state.archived[task_id]` was never set), no data loss; on permission restore + retry, the archive lands and the global evicts.

## Risks

- **Append-only loses operator's ability to fix mistakes in archive files.** Mitigated by the dir-watcher exclusion (edits are inert anyway). Operator can manually edit the file for cosmetic fixes; appends still work correctly (POSIX O_APPEND seeks to end on each write).

- **`surfaced` flag dropped on daemon restart.** Cold-start replay re-flags every task currently in the global projection file before processing live events, so most cases are covered. Tasks that completed during the gap and were surfaced in a prior session but not in the current global file (e.g., they were already TTL-evicted under the old D20 behavior) won't be re-flagged. Acceptable lossiness given the rarity.

- **TOML repurposing semantic confusion.** `resolved_ttl_days` was "how long lingering in global"; becomes "how far back to look for replay." Operators with existing configs see no behavioral change unless they explicitly relied on lingering. Document the rename — or rename it cleanly to `archive_replay_days` with a deprecation alias. Decide during planning.
