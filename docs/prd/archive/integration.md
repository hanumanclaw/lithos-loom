---
title: Lithos Loom — Track 1: Obsidian Bridge (Integration MVP)
milestone: Track 1
status: draft
target_version: 0.1.0
references:
  - docs/PLAN.md (architectural decisions, locked sequencing, plugin contract)
  - docs/prd/mvp.md (Track 2: PRD → PR automation MVP)
  - docs/prd/full.md (Full automated workflow system)
  - /home/dns/projects/lithos/code/lithos/docs/SPECIFICATION.md (Lithos surface, verified 2026-05-05)
labels: [needs-triage, integration, lithos-loom, obsidian, projection]
---

# Lithos Loom — Track 1: Obsidian Bridge (Integration MVP)

## Problem Statement

The original Lithos Loom plan (`docs/PLAN.md`) sequences plugin orchestration first and Obsidian integration as a post-MVP enhancement. Re-evaluating against actual daily friction reorders the priority:

- **Project file management is manual today.** Project context lives in two places — a Lithos KB doc *and* a hand-maintained Obsidian project file — and they drift apart. Updates require typing the same thing twice or accepting the drift.
- **Lithos tasks are invisible from the Obsidian todo surface.** The Tasks plugin already aggregates a unified daily/weekly/inbox view across the vault; Lithos tasks don't appear, so any work tracked in Lithos requires switching surfaces to see.
- **Capture is bifurcated.** Personal items go into Obsidian; project work has to be captured directly in Lithos via CLI/MCP. The friction of switching tools means project work is sometimes captured in Obsidian and never reaches Lithos.
- **The PRD-driven coding loop (Track 2) is high-leverage but lower-frequency.** Even when running well, it kicks in once per PRD. The Obsidian bridge applies to every working day.

The architectural reframe forced by this re-prioritisation is itself valuable: Loom is not a poll-claim-execute task daemon with extras bolted on — it is an event router whose existing route-runner is one kind of subscriber on a shared event bus. Obsidian sync slots into this architecture as additional sources and additional subscribers without changing the plugin contract.

## Solution

A new track of work, **Track 1**, that ships a Lithos ↔ Obsidian bidirectional bridge **before** the Track 2 plugin MVP currently described in `docs/prd/mvp.md`. Track 2's architecture is pre-invested during Track 1 (~half day), so when Track 2 ships its plugins, they slot into the existing bus.

### Three-system view

```
                ┌───────────────┐
                │   OBSIDIAN    │  ← human focus surface
                │  (projection  │     (Tasks plugin queries)
                │  + native     │
                │  todos)       │
                └───────▲───────┘
                        │ projects human-actionable tasks + project contexts
                        │ ticks, edits flow back to Lithos
                ┌───────┴───────┐
                │    LITHOS     │  ← canonical work store + KB
                │  (tasks,      │     (single source of truth)
                │  findings,    │
                │  knowledge)   │
                └───────▲───────┘
                        │ inbound: select GH issues import (deferred)
                        │ outbound: per-project transient issues + PRs (Track 2)
                ┌───────┴───────┐
                │    GITHUB     │  ← external collaboration + PR linkage
                │  (issues,     │     (default-off per project)
                │  PRs)         │
                └───────────────┘
```

Lithos is the spine. Obsidian is the editable read/write surface for selected KB doctypes. GitHub is at the edges — selective per project, default off in v1.

### Architectural reframe: sources → bus → subscribers

```
SOURCES (produce events)              SUBSCRIBERS (consume events)
─────────────────────                 ────────────────────────────────
- Lithos poller                       Routes (claim-bound; existing concept):
- Lithos SSE subscriber  ───►  BUS  ─►  · prd-decompose, story-implement, ...
- Filesystem watcher                  Subscriptions (fire-and-forget):
- GitHub webhook (later)                · obsidian-projection
- A2A endpoint (later)                  · obsidian-completion
- Cron (later)                          · github-import-issue (later)
                                        · github-close-issue (later)
                                        · cost-aggregator (later)
                                        · friction-digest (later)
```

Routes and subscriptions share a single internal type; TOML uses distinct `[[routes]]` and `[[subscriptions]]` stanzas for ergonomics. Routes claim a task and own its lifecycle to `result.json`. Subscriptions are fire-and-forget side effects — concurrent, retry-with-backoff on failure, idempotent by construction.

A supervisor process reads the TOML config and forks subprocess children for each enabled source/subscriber category. v1 lifecycle is monolithic: SIGTERM the supervisor, all children stop. Lithos is the durable bus for state that must survive a daemon restart; the in-process bus is ephemeral, and sources are re-authoritative on restart.

## Locked Design Decisions

The following decisions are locked. References are to the conversation that produced each decision; rationale is in the further-notes section.

| # | Area | Decision |
|---|------|----------|
| D1 | Obsidian's role | Hybrid: Obsidian-source for personal/admin items; projection from Lithos for engineering work |
| D2 | Projection target shape | Single `_lithos/tasks.md` for tasks; folder per project at `_lithos/projects/<slug>/` for KB docs |
| D3 | Architecture | Supervisor + bus + sources → subscribers; one TOML config; subprocess children; monolithic v1 lifecycle |
| D4 | TOML shape | `[[routes]]` and `[[subscriptions]]` as distinct stanzas, structurally one type internally |
| D5 | Filter language | Structural `match.*` tables by default + optional `where = "<expr>"` Python predicate escape hatch |
| D6 | Task projection filter | Project a Lithos task iff `is_human_actionable(task)` — open AND not claimable by any route, OR claimed by a `human_blocking = true` route. Dependency-blocked tasks still project (with `⛔` markers per D19); Tasks-plugin queries decide whether to surface or hide blocked tasks. |
| D7 | GitHub integration | Selective inbound import + transient outbound mirror, per-project, `enable_github_issues = false` default |
| D8 | Capture flow | Multiple direct-to-Lithos entry points (CLI, Obsidian macro, coding MCP, A2A); no Obsidian → Lithos promotion mechanism |
| D9 | Doctypes projected | v1 ships tasks + project contexts only; PRDs/ADRs/story briefs added later as `[[subscriptions]]` |
| D10 | Date semantics | Hybrid: `task.metadata.scheduled_for` if set; else `📅 today` for human-blocking; else undated (backlog) |
| D11 | Migration path | Coexist with existing `projects/` folder initially; `_lithos/projects/<slug>/` becomes steady state |
| D12 | Subscription firing | Concurrent fire-and-forget; per-subscription retry with exponential backoff; `[Friction]` on persistent failure |
| D13 | Recovery model | Sources are re-authoritative on restart; no persistent event log; subscriptions idempotent by construction |
| D14 | Project slug | Slug = directory name under `knowledge/projects/<slug>/` in Lithos KB; Loom TOML `[projects.<slug>]` matches; no v1 rename mechanism |
| D15 | Multi-host | Samsara is the vault host (only Loom host running `obsidian-sync`); other Loom hosts run headless |
| D16 | Subscription handler model | Built-in async coroutines for v1; subprocess opt-in via SDK later |
| D17 | Untick semantics | Unticking a projected task posts a `[ReopenRequested]` finding on the completed task; no automatic reopen |
| D18 | Priority | `task.metadata.priority` enum (`highest \| high \| medium \| low \| lowest`); projection maps to `🔺⏫🔼🔽⏬`, absent = no emoji; bidirectional (Obsidian emoji edits push to Lithos via fs-watcher) |
| D19 | Dependencies | `task.metadata.depends_on` projected as one `⛔ lithos:<dep_id>` per entry; one-way (Lithos canonical, no push-back from Obsidian line edits — dependency graph is structurally important and line-diff editing is fragile) |
| D20 | Resolved-task TTL | Completed and cancelled tasks remain in projection for 7 days (configurable: `[obsidian_sync] resolved_ttl_days = 7`) before dropping; existing `status.type is TODO` queries naturally exclude them so no inbox clutter |
| D21 | Status type sync | `[ ]` ↔ `open`, `[x]` ↔ `completed`, `[-]` ↔ `cancelled` are bidirectional; `[/]` (in progress) and `[>]` (rescheduled) on projected lines are detected by fs-watcher but no-op (informational only, not synced to Lithos) |
| D22 | Created date | `task.created_at` exists in Lithos but is **not** projected — adds visual noise without query-driving value |

## Lithos Prerequisites (Verified 2026-05-05)

Verified against `/home/dns/projects/lithos/code/lithos/docs/SPECIFICATION.md` v0.1.5 (2026-03-18, "Aligned with Implementation").

### Available primitives Track 1 builds on

| Primitive | Source | Use |
|-----------|--------|-----|
| `lithos_task_list` (with `with_claims=true`) | §5.4 | Avoid N+1 calls when projecting list views |
| `lithos_task_status`, `_create`, `_complete`, `_cancel`, `_update` | §5.4 | Task lifecycle from Obsidian-side actions |
| `lithos_finding_post` | §5.4 | `[ReopenRequested]`, `[Friction]` postings |
| `lithos_write` (with `id` for update, `expected_version` for optimistic locking) | §5.1 | Create + update KB docs from Obsidian edits |
| `lithos_read` | §5.1 | Pull canonical doc body for projection |
| `lithos_list(path_prefix="projects/")` | §5.1 | Enumerate project context docs to project |
| `note.created`, `note.updated`, `note.deleted` events | §8.2 | Subscriptions on doc lifecycle |
| `task.created`, `task.claimed`, `task.completed`, etc. events | §8.2 | Subscriptions on task lifecycle |
| `GET /events` SSE endpoint | §8.7 | Live event stream for the Lithos source |
| `expected_version` + `version_conflict` envelope | §5.1 | Bidirectional editing conflict detection |
| `status: active|archived|quarantined` frontmatter | §3.2 | Project-active filtering matches user's existing query patterns |

### Revisions to earlier assumptions (forced by verification)

| Assumed | Actual | Resolution |
|---------|--------|------------|
| Slug as frontmatter field on project context doc | Slug = filename stem; Lithos enforces uniqueness with `slug_collision` envelope | D14: slug is structural (directory name under `knowledge/projects/<slug>/`); no frontmatter convention needed |
| `lithos_task_reopen` tool | Missing; tasks are `open → {completed, cancelled}` terminal | D17: untick posts `[ReopenRequested]` finding; file Lithos issue for proper reopen |
| `note_type: project_context` enum value | Not in enum (`observation \| agent_finding \| summary \| concept \| task_record \| hypothesis`) | Use `note_type: concept` + tag `project-context` |
| Doc events named `lithos.doc.*` | Named `note.created`, `note.updated`, `note.deleted` | Subscriptions reference `lithos.note.*` event names |
| Body update via separate `lithos_doc_update` | Reuse `lithos_write` with `id` parameter; richer than expected | Push uses `lithos_write(id=..., expected_version=...)`; `version_conflict` triggers conflict file |

### Upstream Lithos blockers / pending issues

| Item | Blocking | Status |
|------|----------|--------|
| `task.metadata` field on tasks | All `metadata.*` references throughout Loom design (including `metadata.priority`, `metadata.depends_on`, `metadata.scheduled_for`, `metadata.project`, `metadata.story_doc_id`, `metadata.prd_doc_id`, `metadata.integration_branch`, `metadata.pr_url`, `metadata.host_affinity`, `metadata.github_issue_url`, `metadata.parent_task_id`) | `agent-lore/lithos#215` (already known from PLAN.md) |
| `lithos_task_reopen` tool | Clean untick semantics (current workaround: `[ReopenRequested]` finding per D17) | `agent-lore/lithos#243` |

Track 1 slices 1–5 are unblocked by the existing Lithos surface modulo `task.metadata`. The latter is on the existing PLAN.md critical path. **Slice 0 (bus + supervisor scaffolding) is independent of `#215`** and can begin immediately while `#215` lands; the metadata dependency only bites from slice 1 onwards.

## User Stories

Vertical slices, ordered by build sequence within each phase. Each slice is independently shippable and delivers a standalone value increment.

### Slice 0 — Bus architecture (no user-visible features)

1. As a maintainer, I want a `Supervisor` class that reads a single TOML config, forks subprocess children for each enabled source/subscriber category, propagates SIGTERM, and waits on child exit, so that Track 1 has a single start/stop surface and Track 2's plugin runner slots into the same lifecycle.
2. As a maintainer, I want an asyncio `EventBus` with typed `Event` objects, structural-match filters with optional `where` predicates, concurrent fire-and-forget delivery to subscribers, and bounded per-subscriber queues with drop counters, so that any source can publish without coupling to subscribers.
3. As a maintainer, I want a `LithosPoller` source that wraps the existing Lithos client, polls open tasks at a configured interval, normalises task state changes into `lithos.task.*` events, and publishes them onto the bus, so that Track 2 routes can consume the same source as Track 1 subscriptions.
4. As a maintainer, I want a `Subscription` registry keyed on event-type + filter, instantiating handlers via Python entry points, with per-subscription retry policy, idempotency assumed, and `[Friction]` finding on persistent failure, so that adding a subscription is a TOML stanza + a registered handler module.
5. As a maintainer, I want the existing route-runner re-implemented as a special subscriber type (`claim = true`, plugin invocation, `result.json` parse), so that the legacy poll-claim-execute behaviour preserved by the locked decisions still works on top of the bus.
6. As a maintainer, I want `lithos-loom validate-config --dry-run` that lints the TOML, simulates routing against the current open-task list, and prints which subscriptions/routes would fire without executing them, so that misconfiguration is caught before runtime.

### Slice 1 — Read-only task projection

7. As an operator, I want a TOML `[obsidian_sync]` section declaring `vault_path`, `tasks_file = "_lithos/tasks.md"`, and projection filter knobs, so that the Supervisor knows whether to spawn `obsidian-sync` on this host (samsara only).
8. As an operator, I want an `obsidian-projection` subscription that listens to `lithos.task.{created,updated,completed,cancelled}`, filters via `is_human_actionable(task, routes=...)`, and rewrites `_lithos/tasks.md` with one projected line per matching task, so that my Tasks-plugin daily and inbox queries naturally include human-actionable Lithos work.
9. As an operator, I want each projected line to carry `🆔 lithos:<id>`, computed `📅 <date>` (metadata override else state-driven), `#project/<slug>`, and `#lithos/<route-name>` tags, so that downstream Tasks-plugin queries can sort and filter by route, project, and date without parsing the line.
10. As an operator, I want priority emoji (`🔺⏫🔼🔽⏬`) projected from `task.metadata.priority` (omitted when absent), so that high-priority human-actionable Lithos work is visually distinct in my daily and inbox views and existing Tasks-plugin priority sorting works unchanged.
11. As an operator, I want one `⛔ lithos:<dep_id>` marker per entry in `task.metadata.depends_on` rendered on each projected line, so that the Tasks plugin recognises blocked tasks natively and my queries can hide or surface blocked work as appropriate.
12. As an operator, I want dependency-blocked tasks to still project (D6 revised), so that I have visibility into upcoming work without their being silently hidden behind a runtime predicate.
13. As an operator, I want completed and cancelled tasks resolved within `resolved_ttl_days` (default 7) to remain in `_lithos/tasks.md` with `[x]` / `[-]` status and `✅ <date>` / `❌ <date>` markers, dropping after the TTL elapses, so that I can run "tasks done this week" / "tasks cancelled this week" queries without polluting the open-task inbox.
14. As an operator, I want the projected file to be written atomically (temp + fsync + rename) and only when content has actually changed, so that Obsidian Sync sees clean atomic updates and idempotent re-runs are no-ops.
15. As an operator, I want `lithos-loom doctor` (run on first boot) to verify the configured `vault_path` exists, the `_lithos/` subdirectory is creatable, and a probe write+read round-trip works, so that misconfiguration surfaces immediately.

### Slice 2 — Status push and bidirectional editing

16. As an operator, I want a filesystem watcher source that watches `<vault>/_lithos/tasks.md` and emits `obsidian.task.status_changed` events with prior+new status enum (`[ ]` / `[x]` / `[-]` / `[/]` / `[>]`) and parsed Lithos task ID, so that subscriptions can react to status transitions without polling the file.
17. As an operator, I want an `obsidian-status-transition` subscription that on `[ ]` → `[x]` calls `lithos_task_complete(task_id, agent="lithos-orchestrator-<host>")`, so that ticking a projected task in Obsidian completes it in Lithos.
18. As an operator, I want the same subscription to handle `[ ]` → `[-]` by calling `lithos_task_cancel(task_id, agent=...)`, so that I can cancel a task from Obsidian by changing its status marker.
19. As an operator, I want `[x]` → `[ ]` (untick) transitions to post a `[ReopenRequested]` finding on the completed Lithos task per D17, so that I have a signal in lithos-lens that the task should be revisited, until upstream `lithos_task_reopen` lands.
20. As an operator, I want `[/]` and `[>]` transitions on projected lines detected by the fs-watcher but no-op (with a debug log), so that my Obsidian-only conventions for "in progress" and "rescheduled" don't accidentally trigger Lithos state changes.
21. As an operator, I want an `obsidian-priority-changed` event emitted by the fs-watcher when the priority emoji on a projected line changes, and a corresponding subscription that calls `lithos_task_update(task_id, metadata={"priority": <enum>})`, so that priority adjustments in Obsidian flow back to Lithos canonically.
22. As an operator, I want subscription idempotency enforced: re-firing `obsidian-status-transition` for an already-completed/cancelled task is a no-op (gated on a pre-check via `lithos_task_status`), and re-firing `obsidian-priority-changed` for an unchanged priority is a no-op, so that source-replay on restart is safe.
23. As an operator, I want the fs-watcher to suppress events caused by its own subscriptions' rewrites (mtime + content-hash compare against last-known), so that the projection-write-then-watcher-fires-then-push feedback loop cannot occur.

### Slice 3 — Capture macro

24. As an operator, I want a Templater (or QuickAdd) macro `Create Lithos task` bound to a hotkey that prompts for project (autocompleted from Loom TOML), title (defaulting to selected text), optional brief, optional scheduled date, optional priority, and tags, then calls `lithos_task_create` and inserts a projected line at cursor, so that I can capture project work from Obsidian without leaving the editor.
25. As an operator, I want the inserted line to be born projected — `🆔 lithos:<id>` + frontmatter-consistent date, priority, and tags — so that the next sync recognises it and does not duplicate, and there is no Obsidian-only "captured but not yet promoted" intermediate state.
26. As an operator, I want the macro to surface Lithos errors (validation, duplicate, network) to a notice popup with retry guidance, so that capture failures are obvious rather than silent.
27. As an operator, I want optional macro arguments (`--no-insert`, `--target-file`) so that the same macro powers more advanced flows like "create from CLI but write the line to a specified note," so that capture is composable with my existing daily-note conventions.

### Slice 4 — Project context one-way pull

28. As an operator, I want an SSE source that subscribes to Lithos's `GET /events` filtered to `note.{created,updated,deleted}`, replays buffered events on reconnect via `Last-Event-ID`, falls back to polling when SSE returns 503, and publishes `lithos.note.*` events onto the bus, so that doc lifecycle events drive subscriptions live rather than on poll cadence.
29. As an operator, I want a `project-context-projection` subscription that listens to `lithos.note.*` events, filters via `path_prefix == "projects/" AND tags includes "project-context"`, and writes/rewrites `_lithos/projects/<slug>/<filename>.md` with the doc body and the Lithos-managed frontmatter, so that project context docs appear as Markdown files in my vault.
30. As an operator, I want the projected frontmatter to include `lithos_id`, `lithos_version`, `lithos_updated_at`, `slug` (= directory name), `status` (mirroring Lithos `status` field), and any tags, so that my existing `task.file.property('status') === 'active'` query patterns continue to filter projected project files identically.
31. As an operator, I want a `lithos-loom project list` CLI subcommand that enumerates Lithos project context docs (via `lithos_list(path_prefix="projects/")`), shows their slug, status, and presence in the local TOML, so that I can see at a glance which projects are KB-canonical vs. Loom-managed on this host.
32. As an operator, I want `lithos-loom doctor` to verify that every TOML `[projects.<slug>]` entry has a corresponding Lithos project context doc at `knowledge/projects/<slug>/`, so that machine-local automation cannot reference non-existent projects.

### Slice 5 — Bidirectional project context + create-project macro

33. As an operator, I want the filesystem watcher to also watch `_lithos/projects/<slug>/*.md`, debounce file-save events (250ms), and emit `obsidian.note.modified` events, so that subscriptions can react to vault edits.
34. As an operator, I want a `note-push` subscription that listens to `obsidian.note.modified`, parses the file's frontmatter to extract `lithos_id` and `lithos_version`, calls `lithos_write(id=lithos_id, content=body, expected_version=lithos_version)`, and updates the local frontmatter `lithos_version` on success, so that vault edits propagate to Lithos.
35. As an operator, I want `version_conflict` envelopes from `lithos_write` to trigger a conflict procedure: the local file is moved to `_lithos/conflicts/<slug>.<filename>.<timestamp>.md`, the canonical Lithos version is pulled into the original path, and a notification is posted, so that concurrent edits are surfaced explicitly rather than silently overwritten.
36. As an operator, I want a Templater macro `Create Lithos project` that prompts for title and slug (defaulting to slugified title, validated against `[a-z0-9-]+` and slug-collision via `lithos_list`), creates the Lithos project context doc at `knowledge/projects/<slug>/context.md` via `lithos_write`, and lets the daemon's pull populate the local file, so that new projects can be seeded from Obsidian without leaving the editor.
37. As an operator, I want a `lithos-loom project import <path>` CLI helper that reads a local Markdown file, creates a Lithos project context doc with its body + tags + slug, and prints the projected file location, so that I can migrate my existing `projects/<x>.md` files into Lithos-canonical form one at a time.
38. As an operator, I want frontmatter edits (tags, status) to **not** push from Obsidian via the `note-push` subscription; only body changes push. Frontmatter changes go through a separate `Edit doc tags` macro, so that accidental tag-list edits in Obsidian don't get reflected unintentionally as Lithos doc updates.

## Implementation Decisions

### Modules to build (deep modules where possible)

- **Supervisor** (`lithos_loom.supervisor`) — TOML parser → child fork list → subprocess management → SIGTERM propagation → exit. Deep module.
- **EventBus** (`lithos_loom.bus`) — async pub/sub with structural + predicate filtering, bounded queues, drop counters. Deep module.
- **LithosPoller** (`lithos_loom.sources.lithos_poller`) — scheduled poll over `lithos_task_list(with_claims=True)`, diff against last-known, emit task events.
- **LithosSSE** (`lithos_loom.sources.lithos_sse`) — long-running async client for `GET /events` with reconnect, `Last-Event-ID`, polling fallback.
- **FilesystemWatcher** (`lithos_loom.sources.fs_watcher`) — `watchdog` wrapper, debounce, emit `obsidian.*` events, suppress self-write loop via mtime+hash compare.
- **Subscription registry** (`lithos_loom.subscriptions.__init__`) — entry-point discovery, retry policy enforcement, `[Friction]` posting on persistent failure.
- **Projection subscriptions** (`lithos_loom.subscriptions.{obsidian_projection, obsidian_status_transition, obsidian_priority_changed, project_context_projection, note_push}`) — atomic file rewrite, content-hash dedup, frontmatter management, status enum mapping, priority emoji parsing.
- **Tag-mapping helpers** — `is_human_actionable`, `route_name_to_tag`, `slugify`.
- **Optimistic locking helper** — wraps `lithos_write` with `expected_version` plumbing and conflict directory move.
- **Conflict directory** (`lithos_loom.conflicts`) — atomic move of local file, pull canonical, notification.

### Obsidian Tasks plugin attribute mapping

Track 1 must integrate with the user's existing Tasks-plugin queries (daily, inbox, weekly, etc.). The full attribute table:

| Plugin attribute | Plugin syntax | Lithos source | Direction | Notes |
|---|---|---|---|---|
| Tick state — TODO | `[ ]` | `task.status == "open"` | bidirectional | default for open tasks |
| Tick state — DONE | `[x]` | `task.status == "completed"` | bidirectional | tick triggers `lithos_task_complete`; auto-`✅ <date>` from plugin |
| Tick state — CANCELLED | `[-]` | `task.status == "cancelled"` | bidirectional | `[ ]` → `[-]` triggers `lithos_task_cancel` |
| Tick state — IN PROGRESS | `[/]` | — | none | detected by fs-watcher, no-op (Obsidian-only convention) |
| Tick state — RESCHEDULED | `[>]` | — | none | detected, no-op (Obsidian-only convention) |
| Due date | `📅 YYYY-MM-DD` | `metadata.scheduled_for` (override) or computed | bidirectional via metadata | computed `today` for human-blocking; absent for backlog |
| Priority | `🔺 ⏫ 🔼 🔽 ⏬` | `metadata.priority` enum | bidirectional | absent emoji = no priority |
| Task ID | `🆔 lithos:<uuid>` | `task.id` | one-way | identity, never edited by user |
| Dependencies | `⛔ lithos:<dep_id>` | `metadata.depends_on[]` | one-way (Lithos canonical) | one marker per dep; line edits don't push back |
| Tags | `#project/<slug>`, `#lithos/<route-name>`, plus Lithos `task.tags` | `task.metadata.project`, claim's route, `task.tags` | one-way for `#project` and `#lithos`; Lithos tags pass through | `#daily` is reserved (never emitted) |
| Done date | `✅ YYYY-MM-DD` | (auto-set by plugin on tick) | n/a | Lithos has `task.completed_at`; not separately rendered |
| Cancelled date | `❌ YYYY-MM-DD` | rendered for `[-]` cancelled tasks within TTL | one-way | dropped after `resolved_ttl_days` |
| Scheduled date | `⏳` | — | none | not differentiated from due date in v1 |
| Start date | `🛫` | — | none | deferred |
| Created date | `➕` | — | none | `task.created_at` exists in Lithos but not projected (avoids noise) |
| Recurrence | `🔁` | — | none | no Lithos concept; deferred |

### Projected line shape (rendered example)

```markdown
- [ ] Review PR for story 03 ⏫ 🆔 lithos:abc123 ⛔ lithos:def456 📅 2026-05-05 #project/lithos-loom #lithos/review-human
```

Field order is operator-readable; the Tasks plugin parses positionally-flexible. A cancelled task within the TTL window:

```markdown
- [-] Update old README ❌ 2026-05-04 🆔 lithos:xyz789 #project/lithos-loom
```

### Configuration schema additions

```toml
[obsidian_sync]
vault_path = "/home/dns/vault"
tasks_file = "_lithos/tasks.md"
projects_dir = "_lithos/projects"
conflicts_dir = "_lithos/conflicts"
fs_debounce_ms = 250
resolved_ttl_days = 7      # how long completed/cancelled tasks linger before dropping

[lithos]
url = "http://localhost:8765"
poll_interval_seconds = 30
sse_enabled = true
sse_reconnect_backoff = "exponential"

[[routes]]
on = "lithos.task.created"          # implicit default
match.tags = ["trigger:story-implement"]
plugin = "story-implement"
human_blocking = false              # affects is_human_actionable

[[subscriptions]]
on = ["lithos.task.created", "lithos.task.updated", "lithos.task.completed"]
where = "is_human_actionable(task, routes=ctx.routes)"
action = "obsidian-projection"
retry.attempts = 5
retry.backoff = "exponential"
on_persistent_failure = "friction"

[[subscriptions]]
on = "obsidian.task.toggled"
match.transition = "tick_on"
action = "obsidian-completion"
retry.attempts = 5
retry.backoff = "exponential"
```

### Frontmatter convention for projected project context files

```yaml
---
lithos_id: <uuid>                  # Lithos canonical ID, immutable
lithos_version: <int>              # for optimistic locking on push
lithos_updated_at: <ISO 8601>      # display only
slug: <directory-name>             # = parent directory name; informational
status: active|archived            # mirrors Lithos status field
tags: [project-context, ...]       # mirrors Lithos tags
title: <title>                     # mirrors Lithos title
---
```

The body below the frontmatter is the Lithos doc body. Body edits push; frontmatter edits do not (Slice 5 user story 31).

### Capture flow

Direct-to-Lithos entry points, no Obsidian → Lithos promotion:

| Surface | Implementation |
|---------|---------------|
| CLI | `lithos task new --project <slug> "<title>"` (wraps `lithos_task_create`) |
| Obsidian command palette | Templater/QuickAdd macros `Create Lithos task` / `Create Lithos project` |
| Coding MCP | `lithos-coding-mcp.log_task` (deferred to Track 2 / A9) |
| A2A | Agent Zero / Hanuman → Loom A2A endpoint (deferred to A6) |

### Multi-host deployment

- Samsara is the only Loom host with a vault. Its TOML has `[obsidian_sync]`; the supervisor spawns the obsidian-sync child subprocess (which itself contains the fs-watcher source, projection subscriptions, completion subscription).
- Other Loom hosts (mac mini, future servers) run headless: their TOML has no `[obsidian_sync]` section; the supervisor doesn't spawn an obsidian-sync child; route runners and other sources/subscribers continue normally.
- Obsidian Sync (the app) handles delivering the projected vault to the user's other Obsidian clients (laptop, phone). Loom doesn't see those clients.

### Subscription handler model

Built-in async coroutines registered via Python entry points under `lithos_loom.subscriptions.handlers`. Each handler accepts an `Event` and a `SubscriptionContext` (shared `LithosClient`, filesystem helpers, retry-aware sleep, scoped logger). Idempotency is the handler's responsibility, exercised in tests. Subprocess opt-in (`runner = "subprocess"`) lands later as part of A1's plugin SDK.

## Testing Decisions

**Test philosophy:** test external behaviour, not implementation details. Specifically:

- Subscription tests assert "given event E, side effect F is observed" — not "function G was called".
- Idempotency tests are mandatory for every subscription. Re-firing the same event must produce a no-op or convergent state.
- LLM calls are not part of Track 1 (no `prd-decompose` or coding agents in this track).

**Modules with mandatory unit test coverage:**

- EventBus — fan-out, bounded queue drop, filter combinations, `where` predicate eval (with restricted globals), unsubscribe.
- Supervisor — config parse, child fork on enabled section, SIGTERM propagation, child crash detection (informational; restart not required in v1).
- LithosPoller — diff detection across polls, claim metadata inclusion, debounce.
- LithosSSE — reconnect with `Last-Event-ID`, polling fallback when SSE returns 503, JSON event parsing.
- FilesystemWatcher — debounce, self-write suppression, tick/untick parsing from line diffs.
- Projection helpers — atomic write, content-hash dedup, frontmatter merge, line-diff parsing.
- Optimistic locking helper — happy path push, `version_conflict` triggering conflict directory move, retry-after-pull semantics.

**Modules with integration test coverage:**

- End-to-end task projection — fixture Lithos with a few tasks, assert `_lithos/tasks.md` matches expected; create a new task, assert file updates within poll interval.
- End-to-end completion — tick a projected line, assert Lithos task is `completed` and `[ReopenRequested]` finding is absent. Untick, assert finding present.
- End-to-end project context bidirectional — Lithos write → projected file appears; vault edit → Lithos doc body updates with `expected_version` flow; concurrent edit → conflict file produced.
- Capture macro flow — script-driven Templater invocation; assert created task in Lithos and projected line in vault.
- Source-replay safety — kill the daemon mid-run, restart, assert no duplicate side effects.

**Manual acceptance tests** (not automated):

- A full week of daily use with Slice 1 active. Subjective check: does the Obsidian inbox view show what I expect? Are projected lines noisy?
- Migration of one existing `projects/<x>.md` file into Lithos via the import helper, then verifying bidirectional edits work.

## Out of Scope

The following are deferred to Track 2 (`docs/prd/mvp.md`) or to the full PRD (`docs/prd/full.md`) and must not be added to Track 1:

- All Loom plugin work: `prd-decompose`, `story-implement`, `story-review-human`, `story-review-agent`, `story-fix`, `merge-stories`, `prd-generate`, `prd-review-*`, `decide-next`, `bash-runner` (covered by Track 2 / full PRD)
- Plugin SDK (`lithos_loom.plugin_api`) and pluggable subscription runner (covered by full PRD A1)
- A2A endpoint (covered by full PRD A6)
- GitHub webhook receiver and inbound issue import (covered by full PRD A7 + per-project flag — wired in Track 2)
- Multi-host PRD-affinity (covered by full PRD A7)
- Docker sandbox option (covered by full PRD A10)
- `lithos-coding-mcp` integration (covered by full PRD A9)
- Crash recovery with persistent event log (sources-replay is sufficient for Track 1)
- Cost / token budget tracking
- Hot-reload of TOML config (operator restarts the supervisor)
- Slug rename mechanism (D14 — explicitly out of scope)
- Doctype projection beyond tasks + project contexts (PRDs, ADRs, story briefs, run-logs, findings — all deferred)
- Frontmatter editing from Obsidian pushed to Lithos (Slice 5 user story 38; defer until clear demand)
- Native Obsidian plugin (custom plugin code is heavier than Templater/QuickAdd; reconsider after Slice 3 daily use)
- **Per-project completion log** — append-only `_lithos/projects/<slug>/done.md` capturing every completed task for a project over time, useful for retrospectives and "what shipped this quarter" queries. Conceptually a new `project-completion-log` subscription on `lithos.task.completed` filtered by `metadata.project`. Independent of the global `_lithos/tasks.md` projection's TTL behaviour. Track 1.5 candidate.
- Created date in projected lines (`➕`) — present in Lithos as `task.created_at`, deliberately not projected to keep Obsidian lines uncluttered
- Start date (`🛫`), scheduled-vs-due distinction (`⏳`), and recurrence (`🔁`) — not represented in v1; revisit if query patterns require differentiation
- `[/]` (in progress) representation in Lithos — Obsidian uses it locally; not synced. If demand emerges, candidates are: a Lithos `task.status` extension to include `in_progress`, or a `metadata.in_progress` flag, or an `[InProgress]` finding

## Further Notes

### Why Track 1 ships before Track 2

The original PLAN sequenced Track 2 (PRD → PR automation) first because Loom was the framing object. That ordering optimises for proving the orchestrator end-to-end on lithos-lens M1. But the daily-friction value calculus differs: Track 1's bridge applies every working day; Track 2's PRD loop kicks in once per PRD. Re-prioritising Track 1 trades a delayed Ralph++ replacement for immediate daily ergonomics. The trade is worth it because:

- Track 1's bus architecture is identical to what Track 2 needs anyway (~half day of pre-investment).
- Track 1 exercises the bus, retry policy, idempotency discipline, finding-prefix conventions, and Lithos client *before* the more complex Track 2 plugins land.
- Track 2 plugins, once they ship, immediately benefit from Track 1: every Lithos task created by `prd-decompose` or `story-implement` projects into the Obsidian inbox without additional code.

### Why bus + supervisor + sources/subscribers is "free"

The supervisor + bus + source/subscriber structure is approximately the same code volume as the original poll-claim-execute loop, organised differently. A small `EventBus` class, one async source coroutine per source, a subscriber registry, and the supervisor's fork loop together amount to perhaps 600-800 lines of Python. The original PLAN's poll-loop daemon is a similar size when you include claim/dispatch/retry. The architecture refactor is therefore a structuring choice, not an additive cost.

The payoff is in the roadmap: A6 (A2A endpoint), A7 (webhook receiver), 53a (SSE subscription) all become "add a source" with no plumbing changes. Without the bus architecture, each of those is its own integration project against a poll-loop daemon.

### Why "Lithos canonical, Loom is overlay" is load-bearing

Two consequences fall out of taking Lithos as the canonical store seriously:

- **Project existence is a Lithos fact, not a Loom fact.** A project exists in Lithos when there is a project context doc at `knowledge/projects/<slug>/`. Loom on a given host may or may not have machine-local automation for it (a TOML stanza with repo path, Claude config, etc.). This is the right asymmetry: a project's identity is portable across hosts; its automation is host-specific.
- **The slug is structural, not declarative.** Filesystem layout in Lithos already enforces uniqueness (slug collisions return an error envelope). Re-declaring the slug in frontmatter would be redundant. D14 leans into this.

### Why source-replay over event log

Lithos already is the durable bus for state that must survive a restart — tasks, findings, agent state. The event bus is for ephemera. On restart, the LithosPoller re-discovers open tasks and re-emits `task.*` events; the LithosSSE replays buffered events via `Last-Event-ID`; the FilesystemWatcher re-scans the vault and reconciles state. Subscriptions are idempotent, so re-firing them on the same input is safe.

Inventing a persistent event log to handle daemon crashes would buy nothing because every source is already re-authoritative. The discipline (idempotency-by-construction, atomic writes, content-hash dedup) is what makes the architecture safe — not durability of the bus itself.

### Daily workflow narrative (validation target)

End-state: in the morning, I open my daily note in Obsidian. The Tasks-plugin daily-view query lifts any human-blocking projected Lithos tasks (PR reviews waiting on me, anything claimed by `human_blocking` routes) to the top, with `📅 today` and `#lithos/review-human` tags. My native daily todos appear interleaved with date-driven Lithos tasks. The inbox query (no date, not in `projects/<archived>/`) shows backlog Lithos tasks alongside personal items. When I tick a projected task, Lithos sees it complete. When I capture a new project task via the macro, it lands in Lithos and the projected line appears immediately. Project context files in `_lithos/projects/<slug>/context.md` are bidirectional — I can edit either side and the other catches up, with `_lithos/conflicts/` capturing any concurrent edits I need to resolve.

This is the success bar for Track 1 being daily-useful. We test it manually by living with Slice 1 for several days before adding Slice 2, etc.
