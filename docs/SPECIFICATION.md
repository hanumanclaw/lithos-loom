# Lithos Loom — Specification

Version: 0.1.0
Date: 2026-05-29
Status: Aligned with Implementation

---

## 1. Goals

### 1.1 Primary Goals

1. **Project Lithos work into Obsidian.** Render open Lithos tasks the operator needs to see as Tasks-plugin-compatible lines in a vault file, so that an existing Obsidian daily-view workflow surfaces Lithos work alongside personal todos.
2. **Push vault-side edits back to Lithos.** Tick-to-complete, priority emoji edits, due-date edits, and project-context body edits made in Obsidian propagate to Lithos with optimistic locking.
3. **Capture Lithos work from inside Obsidian.** Templater macros for creating tasks and project-context docs let the operator stay in the editor.
4. **Adopt existing Obsidian project docs into Lithos.** A `project import` CLI extracts `- [ ]` task lines as real Lithos tasks with dependency edges derived from indentation.
5. **Run subprocess plugins against Lithos tasks.** A route-runner child claims tasks by tag, invokes a plugin subprocess with a small contract, and applies the result back to Lithos.
6. **Stay out of project repos.** Loom configuration is host-local TOML; project repo `AGENTS.md` / `CLAUDE.md` files carry no Lithos / Loom references.

### 1.2 Non-Goals

1. **Cloud sync, multi-tenant operation, web UI.** Single-operator, local-first. Obsidian Sync (or any other vault-sync layer) is the operator's choice.
2. **Real-time co-editing.** File-based, poll-driven; latency target is ≤500ms end-to-end for projection + push.
3. **Replacing Lithos as the source of truth.** Lithos owns the corpus and the task lifecycle. The vault is a projection plus a write surface for specific edits.
4. **Cross-host coordination.** The vault host is the only host running `obsidian-sync`; other hosts run headless route-runners. Loom does not coordinate across hosts via shared state.
5. **Reopening a completed task.** Lithos exposes no `task_reopen` primitive. Untick (`[x] → [ ]`) posts a `[ReopenRequested]` finding instead.
6. **Plugin bodies for PRD decomposition, story implementation, story review.** Scaffolding exists in `src/lithos_loom/plugins/`; the implemented surface stops at the orchestration spine plus the Obsidian bridge. Generating PRDs, reviewing diffs, and brain-driven decisions are not implemented today.

### 1.3 Compatibility Policy (Pre-1.0)

1. **TOML schema evolves.** Field renames or removals require a documented migration step but are otherwise free.
2. **Event names are stable.** Subscribers depend on dotted event names (`lithos.task.created`, `obsidian.note.modified`); changing them is a breaking change.
3. **`result.json` schema is versioned.** Plugins ship a `schema_version` integer; incompatible changes bump it.
4. **Vault-projected file layout is stable.** `_lithos/tasks.md`, `_lithos/projects/<slug>/<file>.md`, `_lithos/conflicts/<slug>.<file>.<ts>.md` are documented locations operators query and grep against.

---

## 2. Architecture

### 2.1 Component Overview

Loom is structured as `sources → bus → subscribers`. Sources publish typed events onto an in-process async bus; subscribers consume them. The route-runner is a special claim-bound subscriber that owns a task's lifecycle to `result.json`.

```
┌──────────────────── lithos-loom (one host process) ────────────────────┐
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Supervisor (lithos-loom run)                                     │  │
│  │  - Reads one TOML config                                         │  │
│  │  - Forks subprocess children per enabled category                │  │
│  │  - Propagates SIGTERM, waits on exit                             │  │
│  └────────────┬─────────────────────────────────────┬───────────────┘  │
│               │                                     │                  │
│  ┌────────────▼──────────────┐  ┌────────────▼─────────────┐  ┌───────▼───────────┐  │
│  │ route-runner child         │  │ obsidian-sync child       │  │ github-watcher    │  │
│  │  (enabled when [[routes]]) │  │  (enabled when           │  │  child (enabled   │  │
│  │                            │  │   [obsidian_sync]        │  │  when             │  │
│  │  Sources:                  │  │   is present)            │  │  [github_watcher] │  │
│  │   LithosEventStream        │  │                          │  │  enabled=true)    │  │
│  │                            │  │  Sources:                │  │                   │  │
│  │  Subscribers:              │  │   LithosEventStream      │  │  Sources:         │  │
│  │   one RouteRunner per      │  │   LithosNoteStream       │  │   LithosNoteStream│  │
│  │   [[routes]] stanza        │  │   ObsidianFSWatcher      │  │   GitHubIssue     │  │
│  │   (claim-bound)            │  │   ObsidianDirWatcher     │  │     Watcher       │  │
│  │                            │  │                          │  │                   │  │
│  │                            │  │  Subscribers (per        │  │  Subscribers      │  │
│  │                            │  │   configured action):    │  │   (auto-wired):   │  │
│  │                            │  │   obsidian-projection    │  │   github-issue-   │  │
│  │                            │  │   obsidian-status-       │  │     sync          │  │
│  │                            │  │     transition           │  │                   │  │
│  │                            │  │   obsidian-priority-     │  │                   │  │
│  │                            │  │     changed              │  │                   │  │
│  │                            │  │   obsidian-due-date-     │  │                   │  │
│  │                            │  │     changed              │  │                   │  │
│  │                            │  │   project-context-       │  │                   │  │
│  │                            │  │     projection           │  │                   │  │
│  │                            │  │   note-push              │  │                   │  │
│  │                            │  │   task-archive           │  │                   │  │
│  │                            │  │   noop                   │  │                   │  │
│  │                            │  │                          │  │                   │  │
│  │  In-process EventBus       │  │  In-process EventBus     │  │  In-proc EventBus │  │
│  └─────────────┬──────────────┘  └────────┬─────────────────┘  └─────────┬─────────┘  │
│                │                          │                              │            │
└────────────────┼──────────────────────────┼──────────────────────────────┼────────────┘
                 │                          │                              │
                 ▼                          ▼                              ▼
       ┌─────────────────┐         ┌────────────────┐           ┌────────────────────┐
       │ Lithos          │         │ Obsidian vault │           │ GitHub REST API    │
       │  /sse  /events  │         │  (fs)          │           │  api.github.com    │
       └─────────────────┘         └────────────────┘           └────────────────────┘
```

Each child runs its own EventBus instance. There is no inter-child IPC; all three children independently consume Lithos SSE. Restart safety relies on sources being re-authoritative (no persistent event log) and subscribers being idempotent.

### 2.2 Data Flow

**Task projection (Lithos → vault).**
`LithosEventStream` connects to `<lithos_url>/events` filtered to `task.*` events. It bootstraps once on connect by calling `lithos_task_list(status='open', with_claims=true)` and re-emitting `lithos.task.created` for every open task, then streams live events with `Last-Event-ID` resume. `obsidian-projection` filters via `is_human_actionable(task, routes)` and rewrites `<vault>/<tasks_file>` atomically.

**Status push (vault → Lithos).**
`ObsidianFSWatcher` polls `<vault>/<tasks_file>` (default 250ms), parses line-by-line, and emits `obsidian.task.status_changed`, `obsidian.task.priority_changed`, or `obsidian.task.due_date_changed` when a line diverges from the last-known state. Self-write suppression compares mtime + content hash against the projection's last write. Three subscriptions consume these events and call `lithos_task_complete` / `_cancel` / `_update` against Lithos.

**Project-context projection (Lithos → vault).**
`LithosNoteStream` connects to `/events` filtered to `note.*` events; bootstrap calls `lithos_list(path_prefix='projects/', tags=['project-context'])` and re-emits `lithos.note.created` for each match. `project-context-projection` re-fetches via `lithos_read` (events are summaries; tags need verification post-fetch), then writes `<vault>/<projects_dir>/<slug>/<filename>.md` with a frontmatter envelope.

**Body push (vault → Lithos).**
`ObsidianDirWatcher` polls `<vault>/<projects_dir>/**/*.md` (default 250ms), computes body-only hash, and emits `obsidian.note.modified` when divergent. `note-push` calls `lithos_write(id=..., expected_version=...)`. On `version_conflict`, the conflict resolver moves the operator's body to `<vault>/_lithos/conflicts/<slug>.<file>.<ts>.md`, pulls canonical to the original path, and logs a `[Friction]` WARNING.

**Task lifecycle (route-runner).**
`LithosEventStream` (running in the route-runner child) emits `lithos.task.*` events. Each `[[routes]]` stanza registers a claim-bound subscriber against `lithos.task.created` and `lithos.task.released` (only) that requires every tag in `match.tags` to be present on the task (same semantic as the bus matcher and `is_human_actionable`). `lithos.task.updated` is **not** subscribed to today — editing a task's tags after creation does not re-trigger route pickup. On match, the runner claims via `lithos_task_claim`, spawns the plugin subprocess, periodically renews the claim, and waits for `result.json`. It then reads only the `status` field:

- `succeeded` → `lithos_task_complete`.
- `failed` → `lithos_task_release` + `[BlockerFailed]` finding (the error message is pulled from `error.message` if present).
- `interrupted` → `lithos_task_release`, no finding. When the result also carries a `resume` block (`resume_after` timestamp — e.g. a story-develop run that checkpointed on a provider usage limit), the runner additionally schedules an in-process re-dispatch: at `resume_after` it re-checks the task is still open, then re-claims and re-runs the plugin. Bounded at `MAX_RESUMES_PER_TASK` (3) re-dispatches per task per daemon process; on exhaustion the task stays open with a `[Friction]` finding. The schedule is in-memory only — a daemon restart loses it, but the event-stream bootstrap re-surfaces open tasks on startup anyway.
- Unknown / missing status → `lithos_task_release` + `[BlockerFailed]`.

Other `result.json` fields (`metadata_updates`, `artifacts`, `commits`, `spawned_tasks`, `exit_code`, `error.retriable`) are schema-validated but currently ignored. On plugin timeout, the runner sends SIGTERM with a grace period before SIGKILL.

**GitHub issue mirror (GitHub → Lithos).**
`GitHubIssueWatcher` (running in the github-watcher child) polls every repo flagged for watching on its `[github_watcher].poll_interval_seconds` cadence (default 60s). Watch eligibility is derived from project-context metadata: a doc with `github_watch_enabled = true` and a non-empty `github_repos` list enrols its slug → repo mappings (a project may map several repos, each polled independently). Discovery is one filtered call — `note_list(path_prefix="projects/", metadata_match={"github_watch_enabled": true})` — and each returned item carries its metadata, so the repo list and exclude filters are read without a follow-up per-doc fetch. The watcher subscribes to `lithos.note.{created,updated}` on the in-process bus so a `project enable-github <slug>` mid-run takes effect without a daemon restart. Per-repo `updated_at` cursors persist in a daemon-owned Lithos doc (default `projects/_lithos-loom-internal/github-watcher-state.md`, configurable) so cold restart doesn't re-walk every open issue. Coord-doc writes are CAS-protected: on `version_conflict` the watcher merges the just-observed cursor advances with the remote cursors (latest timestamp wins per repo) and retries, so concurrent writes don't lose progress. Per-repo polls split into two paths: **bootstrap** (no cursor yet for this repo) lists `state=open` with full `Link: rel="next"` pagination so every open issue surfaces in one cycle regardless of historical-closure volume; **incremental** (cursor present) lists `state=all` since the cursor with the same pagination so closes on previously-seen issues — their `updated_at` advances at close time — surface alongside fresh opens. Each issue surfaced this poll publishes one `github.issue.seen` event onto the in-process bus; the auto-wired `github-issue-sync` subscriber resolves an `<!-- lithos:<task_id> -->` linkage marker in the issue body, then takes one of these branches:

- Marker → open Lithos task: drift-sync only (title / body / labels — see below).
- Marker → open Lithos task, GH closed-completed: drift-sync + `lithos_task_complete`.
- Marker → open Lithos task, GH closed-not_planned: drift-sync + `lithos_task_cancel`.
- Marker → terminal Lithos task: drift-sync only (idempotent close mirror). If GH state transitioned from closed back to open and `metadata.github_state_snapshot != "open"` on the task, also post a `[ReopenRequested]` finding (de-duped via the snapshot field).
- Marker → missing task (operator force-deleted): create a fresh task; the marker writer overwrites the stale id.
- No marker, Lithos task carries `metadata.github_issue_url` for this URL: re-write the canonical marker on GitHub. No duplicate task.
- No marker, no matching task, GH open: `lithos_task_create` with `title=issue.title`, `description=issue.body`, `tags=issue.labels + ["github-issue"]`, `metadata={project, github_issue_url, github_issue_number, github_labels, github_state_snapshot=issue.state}`. Then write the canonical `<!-- lithos:<task_id> -->` marker into the issue body via `PATCH /repos/{owner}/{repo}/issues/{n}` — fetched fresh via `get_issue` immediately before the PATCH so an operator edit during the poll-to-PATCH window survives.
- No marker, no matching task, GH closed: skip (historic closures are not backfilled).

**Per-project exclude filters.** The watcher ships each event with the project's import-time filters, sourced from these metadata keys on the project-context doc (applied to every repo the project maps):

- `github_exclude_labels` (list) — drop the issue at import time if it carries any of these labels.
- `github_exclude_authors` (list) — drop if the GH author login matches (e.g. `dependabot[bot]`).

Filters apply only on the create branch (no marker + no matching URL + GH open). Already-linked tasks are unaffected if an exclude tag is added after import — the PRD explicitly locks "exclude is only at import time" so the operator never has a once-imported task quietly stranded.

**Dispatch contract (GH → Lithos).** The watcher source dispatches each issue inline to the `github-issue-sync` handler before advancing the persistent cursor — the bus path is reserved for tests that assert on queue contents. Cursor advancement is per-issue: the watcher walks GitHub's `updated_at`-ascending list, advances the in-memory cursor to each issue's timestamp only after dispatch succeeds, and halts the loop on the first exception so the next poll re-fetches starting from the failed boundary. Issues that failed dispatch are tracked in a `_stuck_issues: dict[str, set[int]]` map and retried by direct `github.get_issue` lookup at the top of the next poll — that path is independent of the cursor and the `state=` filter, so a bootstrap walk that's about to lose a closed-before-retry issue still gets it. The stuck set is persisted in the coord doc as `stuck:<owner>/<name>#<number>` rows alongside the cursor rows, so daemon restart between a partial reconciliation (e.g. `task_create` succeeded, marker PATCH failed) and the next retry preserves the repair record. CAS-write semantics protect both halves: deletion tombstones are tracked at function entry for both cursors and stuck rows so a `version_conflict` reload-then-merge doesn't resurrect locally-drained state.

**Dispatch contract (Lithos → GH).** The push direction uses the bus because `LithosEventStream` already serves multiple subscribers across child processes. The consumer loop classifies handler exceptions: permanent errors (`GitHubAuthError`, `GitHubRepoNotFoundError`) log `[Friction]` and drop without retry — retry won't help. Other `GitHubError` subclasses (transients — 5xx, network blips, rate-limit exhausted) retry with exponential backoff capped at 60s, up to 8 attempts (inter-attempt waits 2/4/8/16/32/60/60 s ≈ 3 minutes total before drop). Outages outlasting that budget are caught by the **periodic reconciliation sweep**: every `[github_watcher].reconcile_interval_minutes` (default 60) the child re-fetches open Lithos tasks plus completed + cancelled tasks resolved within `resolved_replay_days` (skipped entirely when `resolved_replay_days = 0`), filters to those carrying `metadata.github_issue_url`, and re-dispatches each one through the push handler. Terminal tasks dispatch both a synthetic `task.updated` (so title drift reconciles) AND the matching close event. The handler is idempotent (re-fetches GH before PATCH) so the sweep is a no-op in steady state. The sweep keeps recovery cadence within the configured interval even without a daemon restart; set to 0 to disable.

**Drift sync** (GH → Lithos, Slice 7.2). Every poll that matches a known Lithos task layers three checks on top of the close mirror:

- **Title drift** — `issue.title != task.title` → `task_update(title=issue.title)`.
- **Body drift** — `strip_marker(issue.body) != task.description` → `task_update(description=...)`. The `<!-- lithos:<id> -->` marker is never reflected into the Lithos task description.
- **Label diff** — read `metadata.github_labels` snapshot; compute `removed = old − new` and `added = new − old`; new tag set is `(task.tags − removed) | added`. Operator-added Lithos tags never in any GH snapshot survive untouched. The snapshot in metadata rolls forward to `issue.labels`.
- **State snapshot** — `metadata.github_state_snapshot` rolls forward to `issue.state` on every poll. Reopen detection compares the *prior* value before drift sync overwrites it.

All four drifts in one poll batch into a single `task_update` call. Steady-state polls (nothing changed) cost zero round-trips.

**GitHub issue mirror (Lithos → GitHub, Slice 7.2).**
The `github-issue-push` subscription (auto-wired in the github-watcher child) consumes `lithos.task.{created,completed,cancelled,updated}` events from `LithosEventStream` on the in-process bus. The `task.created` event is the open-task snapshot replay surface at daemon startup — a Lithos rename that happened while the watcher was down only re-fires as `task.created` on restart, so the title branch consumes it identically to `task.updated`. The handler branches as follows:

- `lithos.task.completed` → fetch GH issue; if not already closed-as-completed, `PATCH state=closed state_reason=completed`.
- `lithos.task.cancelled` → same, with `state_reason=not_planned`.
- `lithos.task.updated` → if `task.title` differs from the current GH issue title, `PATCH title`.

Tasks without `metadata.github_issue_url` are filtered at the handler entry (the by-far-common case) and stay silent at INFO. GH errors (404 / auth) during the push surface as `[Friction]` log lines, not retries — a permanent failure shouldn't loop.

Pull requests are filtered at parse time (presence of GitHub's `pull_request` field on the row). A 404 on a watched repo drops it from the in-memory watch list with a `[Friction]` log line; the next bus-driven refresh re-adds the slug if the operator fixes the typo. GitHub rate-limit responses (403 with `X-RateLimit-Remaining: 0`) trigger a sleep until `X-RateLimit-Reset`; a 403 with non-zero remaining surfaces as auth/permission error rather than retried indefinitely.

### 2.3 Restart and Recovery

Loom has no persistent event log. On restart:

- `LithosEventStream` and `LithosNoteStream` bootstrap (re-list and re-emit) and then resume via `Last-Event-ID`.
- `ObsidianFSWatcher` and `ObsidianDirWatcher` re-scan their watched files on startup; sync-state baselines are rebuilt incrementally as projection events fire.
- `obsidian-projection` writes the full file from scratch on every flush. Idempotent re-runs are no-ops thanks to atomic-write + content-hash dedup.
- `project-context-projection` re-reads each doc and rewrites if the full-file hash differs.
- `note-push` and `obsidian-status-transition` pre-check Lithos state before mutating (re-firing an already-completed task is a no-op).
- The route-runner does NOT reclaim stale claims from a previous process at startup. Stale claims age out via Lithos's own claim-expiry mechanism; a future run picks the task back up when the claim TTL elapses.

Subscriptions are idempotent by construction; replay is safe.

---

## 3. Configuration

Loom reads one TOML file per process. Discovery order:

1. `LITHOS_LOOM_CONFIG=/abs/path/to/config.toml` (explicit, beats everything)
2. `LITHOS_LOOM_ENVIRONMENT=<env>` selects `config.<env>.toml` from `./` then `$XDG_CONFIG_HOME/lithos-loom/`
3. Plain `config.toml` from the same locations

`python-dotenv` loads `.env` from the current working directory at startup, primarily for `LITHOS_URL`.

### 3.1 Full TOML Reference

```toml
# ── Required ──────────────────────────────────────────────────────────

[orchestrator]
agent_id      = "lithos-orchestrator-<host>"  # claim attribution; must be unique per host
lithos_url    = "http://localhost:8765"        # Lithos MCP-over-SSE endpoint
work_dir      = "/tmp/lithos-loom"             # per-task staging tree
max_concurrency        = 4                     # parsed but NOT YET enforced (#85); no runtime cap today — a single route runs serially, multiple routes do not contend
log_level              = "info"                # debug | info | warning | error
retain_failed_workdirs = true                  # keep failed work-dirs for triage

# ── Projects (host-local automation registry) ─────────────────────────
#
# Projects exist in Lithos when a project-context doc lives at
# `knowledge/projects/<slug>/`. This TOML registers host-local automation
# config for projects this host should act on. `repo` is the only
# required field. `claude_config` and `codex_config` are parsed and
# stored but not yet consumed by any shipped plugin body.

[projects.<slug>]
repo          = "/abs/path/to/repo"
claude_config = "/home/you/.claude-lithos"     # optional, parsed but unused today
codex_config  = "/home/you/.codex-lithos"      # optional, parsed but unused today

# ── Routes (claim-bound subscribers) ──────────────────────────────────
#
# Each [[routes]] stanza is a claim-bound subscriber that listens to
# lithos.task.created and lithos.task.released (only). A task matches
# when every tag in match.tags is present on the task. The runner claims
# matching tasks, invokes `command` as a subprocess, and reads only
# `status` from the resulting result.json to decide whether to complete
# or release (see §5). Other result.json fields are schema-validated
# but not yet applied. Tag edits on an existing task arrive as
# task.updated, which the runner does NOT subscribe to.
#
# Substitution tokens in `command`:
#   {{task_json}}    — path to the task envelope JSON (read-only)
#   {{work_dir}}     — per-task staging dir under orchestrator.work_dir
#   {{result_file}}  — path the plugin must atomically write
#   {{repo}}         — [projects.<slug>].repo for the task's metadata.project
#                      (one route serves all projects; unresolvable → finding)

[[routes]]
name = "story-implement"
command = "uv run python -m lithos_loom.plugins.story_implement --task-json {{task_json}} --work-dir {{work_dir}} --result-file {{result_file}}"
max_runtime_seconds = 7200
human_blocking = false  # if true, surfaced in Obsidian projection once claimed

[routes.match]
tags = ["trigger:story-implement"]  # task must carry ALL listed tags

# story-develop: {{repo}} resolves per task from [projects.<slug>].repo, so
# one route serves every project. Reviewer config comes from the
# project-context doc's develop_* metadata (§5.5).
[[routes]]
name = "story-develop"
command = "uv run python -m lithos_loom.plugins.story_develop --task-json {{task_json}} --work-dir {{work_dir}} --result-file {{result_file}} --repo {{repo}}"
max_runtime_seconds = 28800

[routes.match]
tags = ["trigger:story-develop"]

# ── Subscriptions (fire-and-forget side effects) ──────────────────────
#
# Each [[subscriptions]] stanza is a fire-and-forget subscriber that
# consumes one or more event types, runs an `action` registered as a
# Python entry-point handler, retries on failure with exponential or
# linear backoff, and posts a [Friction] finding on persistent failure
# (default; set to "ignore" to suppress).

[[subscriptions]]
name = "obsidian-tasks"
on = [
  "lithos.task.created",
  "lithos.task.updated",
  "lithos.task.claimed",
  "lithos.task.released",
  "lithos.task.completed",
  "lithos.task.cancelled",
]
match.tags = []                  # optional: structural superset filter
where      = ""                  # optional: Python expression with `task` and `event` in scope
action     = "obsidian-projection"
on_persistent_failure = "friction"  # or "ignore"
[subscriptions.retry]
attempts = 5
backoff  = "exponential"          # or "linear"
initial_delay_seconds = 0.5
max_delay_seconds     = 30.0

# ── Obsidian sync (vault-host only) ───────────────────────────────────
#
# Presence of this block is the spawn gate for the obsidian-sync child.
# Omit on hosts without a vault.

[obsidian_sync]
vault_path        = "/home/you/Obsidian/Vault"   # absolute
tasks_file        = "_lithos/tasks.md"           # relative to vault_path
projects_dir      = "_lithos/projects"           # relative to vault_path
resolved_ttl_days = 7                            # see §6.3 task-archive interaction
include_blocked   = true                         # project tasks with metadata.depends_on
exclude_tags      = ["debug:trace"]              # suppress projection for these tags

# ── GitHub issue watcher (per-host gate) ──────────────────────────────
#
# Presence of this block AND `enabled = true` is the spawn gate for the
# github-watcher child. Only one host should have this enabled at a time
# (no Lithos-coordinated election; pick one host manually). The watcher
# uses `gh auth token` at startup to resolve a bearer token, so the host
# must have `gh` on PATH with the operator already logged in.
#
# Per-project enablement lives in metadata on the project-context doc;
# manage via `lithos-loom project add-github-repo <slug> <owner/name>` and
# `lithos-loom project enable-github <slug>` (§4).

[github_watcher]
enabled               = false                                  # spawn gate
poll_interval_seconds = 60                                     # incremental polls
coord_doc_path        = "projects/_lithos-loom-internal/github-watcher-state.md"
# Lithos doc the watcher uses to persist per-repo updated_at cursors.
# Must be a relative Lithos doc path (no leading `/`, no `..`).
resolved_replay_days  = 7
# How far back the embedded LithosEventStream replays resolved task
# events at bootstrap. A Lithos task that closes (or gets renamed) while
# the watcher is down is mirrored to GH on restart via the replay; the
# push handler is idempotent (refetches GH before PATCH) so a too-large
# window only costs harmless re-checks. Set to 0 to disable replay (the
# push handler then only fires for events that arrive live).
reconcile_interval_minutes = 60
# Cadence of the periodic Lithos→GH reconciliation sweep. Catches drift
# left over from outages longer than the in-memory retry budget — every
# interval the child scans Lithos for open + recently-resolved tasks
# carrying metadata.github_issue_url and replays each through the push
# handler. Set to 0 to disable the sweep.
```

### 3.2 Validation

`lithos-loom validate-config` parses, typechecks, and lists projects / routes / subscriptions. `validate-config --dry-run` additionally polls Lithos and prints which routes / subscriptions would fire for each currently-open task plus any orphans (tasks no route matches) and dead config (routes / subscriptions no task currently matches). Both forms exit non-zero on invalid TOML.

`lithos-loom doctor` verifies the configured `vault_path` exists, `_lithos/` is creatable, and a probe write+read round-trip works. It also reads `lithos_list(path_prefix='projects/')` and warns about TOML `[projects.<slug>]` entries with no corresponding Lithos project-context doc.

---

## 4. CLI Reference

All commands accept `--config / -c <path>` to override discovery. JSON-emitting commands accept `--format / -f json|text`.

### 4.1 `lithos-loom run`

Starts the daemon: supervisor + per-domain children. Foregrounded process; SIGINT / SIGTERM trigger graceful shutdown — the supervisor signals children to stop, in-flight plugin subprocesses are cancelled, and the supervisor waits up to a timeout before SIGKILLing any child that didn't exit. Cancelled plugins that don't write a result file trigger the contract-violation release path; claims may also be left to age out via Lithos's claim TTL.

```
lithos-loom run [-c config.toml]
```

Exit codes: `0` clean exit, non-zero on child crash before shutdown or SIGKILL after timeout.

### 4.2 `lithos-loom validate-config`

```
lithos-loom validate-config [-c config.toml] [--dry-run]
```

- Plain form: parse, validate, print `OK:` summary (agent_id, lithos_url, projects, routes, subscriptions).
- `--dry-run`: also fetch open tasks from Lithos and print routing / subscription dry-runs. Useful before introducing new routes.

### 4.3 `lithos-loom doctor`

```
lithos-loom doctor [-c config.toml]
```

Probes vault writability + the Lithos project surface. Each check prints PASS/FAIL with an actionable message. Non-zero exit if any check fails.

### 4.4 `lithos-loom config`

```
lithos-loom config --show [-c config.toml]
```

Prints the merged effective config. Useful for verifying config discovery picked the right file.

### 4.5 `lithos-loom task create`

```
lithos-loom task create --project <slug> --title <text>
                        [--brief <text>] [--scheduled YYYY-MM-DD]
                        [--priority highest|high|medium|low|lowest]
                        [--tags a,b,c]
                        [--target-file <path> | --no-insert]
                        [-c config.toml]
```

Creates a Lithos task and emits its projected line. Used by the capture-task Templater macro.

Output modes (mutually exclusive):

- **Default**: print the projected `- [ ]` line to stdout. Useful for redirect/pipe.
- **`--target-file PATH`**: append the line to PATH (creates parent dirs). Used by "create task and write the line into next week's daily note" flows.
- **`--no-insert`**: print just the task_id to stdout; the projected line is discarded. Used by the capture macro (which inserts a wikilink instead).

Exit codes: `0` success, `1` Lithos call failed, `2` validation failure (unknown project, unknown priority, mutually-exclusive output flags).

### 4.6 `lithos-loom project list`

```
lithos-loom project list [--source lithos|toml] [-f text|json] [-c config.toml]
```

- `--source lithos` (default): merges Lithos's project-context-doc list with the local TOML overlay. Three columns: slug, status (`active`/`archived` from the canonical doc), repo path.
- `--source toml`: lists only the TOML's `[projects.<slug>]` slugs.
- `-f json`: emits a JSON array of slug strings (what the capture macro consumes).

Canonical-doc picker: for each slug, prefers `projects/<slug>/<slug>-project-context.md`; falls back to lex-min path when no canonical doc exists.

### 4.7 `lithos-loom project create`

```
lithos-loom project create --title <text>
                           [--slug <slug>] [--tags a,b]
                           [--body <text> | --body-file <path>]
                           [-f text|json] [-c config.toml]
```

Creates a new Lithos project-context doc at `projects/<slug>/<slug>-project-context.md`. Slug defaults to slugified `--title` when not given. `project-context` tag is always added (plus any operator-supplied tags, deduped).

Output: vault path of the projected file (text) or `{id, slug, vault_path}` (json).

Exit codes: `0` success, `1` slug collision or Lithos call failure, `2` invalid slug.

### 4.8 `lithos-loom project import`

```
lithos-loom project import <source> [--slug <slug>] [--tags a,b]
                                    [--tasks-only] [--no-tasks]
                                    [--force-tasks] [--yes]
                                    [--dry-run] [-f text|json]
                                    [-c config.toml]
```

Imports an existing local Markdown file as a Lithos project, extracting `- [ ]` task lines as real Lithos tasks. Two modes:

- **Greenfield (default)**: creates the project doc + tasks. Refuses if slug exists; error message points at `--tasks-only` as the alternative.
- **`--tasks-only` + `--slug`**: skips doc creation; just adds tasks to an existing project.

Task extraction is on by default. `--no-tasks` skips it. `--force-tasks` cancels all open tasks for the slug before importing (gated by interactive y/N unless `--yes`). `--dry-run` prints the full plan without Lithos writes; output is framed with `NO CHANGES MADE` markers at start and end.

Slug derivation (when `--slug` not given): `--title` frontmatter → file stem with a leading `project-` prefix stripped. The strip is flagged in dry-run output.

Task extraction parses:
- Tags matching `#[A-Za-z0-9_/-]+` (all-digit tokens like `#123` excluded).
- Priority emoji `🔺⏫🔼🔽⏬` mapped to `metadata.priority`.
- Cross-project `#project/<other-slug>` tags refuse the import (exit 2).
- Indented children become `metadata.depends_on` from parent → children; siblings are `metadata.parallelizable = true` by default; `[sequential]` token on parent flips children to a chain.

Exit codes: `0` success, `1` Lithos call failure / slug collision / missing project / partial-import failure, `2` input validation failure.

Full reference: `docs/cli/project-import.md`.

### 4.9 `lithos-loom project regenerate-done`

```
lithos-loom project regenerate-done --slug <slug>
                                    [--dry-run] [--yes]
                                    [-f text|json] [-c config.toml]
```

Rebuilds `<vault>/<projects_dir>/<slug>/<slug>-done.md` from Lithos by writing every resolved (completed + cancelled) task for the slug as a Tasks-plugin line. Replaces the file outright (no merge). Sorted ascending by `resolved_at`, ties broken by task id. Confirmation prompt fires when the file already exists; `--yes` bypasses.

Differs from the live `task-archive` subscription: the archive subscription only records tasks the operator surfaced in `tasks.md`; `regenerate-done` writes all resolved tasks (a complete-history superset).

Full reference: `docs/cli/project-regenerate-done.md`.

### 4.10 `lithos-loom project add-github-repo` / `remove-github-repo`

```
lithos-loom project add-github-repo    <slug> <owner/name> [-c config.toml]
lithos-loom project remove-github-repo <slug> <owner/name> [-c config.toml]
```

Map / unmap a GitHub repo for the issue watcher by editing the `github_repos` metadata list on the canonical project-context doc. A project may map several repos (call `add-github-repo` once per repo); each is polled independently. `add` validates `owner/name` against GitHub's rules at CLI time — a malformed value exits 2 before any Lithos write — and is idempotent if the repo is already present. `remove` is idempotent if the repo is absent; removing the last repo is allowed (the project is unmapped) and warns if watching is still enabled.

The watcher does not begin polling until `enable-github <slug>` sets `github_watch_enabled = true`.

### 4.11 `lithos-loom project enable-github`

```
lithos-loom project enable-github <slug> [-c config.toml]
```

Sets `github_watch_enabled = true` on the project-context doc, enabling polling. Requires a non-empty `github_repos` list (exit 2 if empty, with the actionable error pointing at `add-github-repo`).

### 4.12 `lithos-loom project disable-github`

```
lithos-loom project disable-github <slug> [-c config.toml]
```

Sets `github_watch_enabled = false`. The `github_repos` list is preserved so re-enabling later doesn't need `add-github-repo`. Disabling stops new polls for the project at most one poll interval later (in-flight events for that slug still drain).

### 4.13 `lithos-loom project migrate-github-tags`

```
lithos-loom project migrate-github-tags [--dry-run] [-c config.toml]
```

One-shot migration from the legacy tag-based scheme (`github-repo:` / `github-watch` / `github-exclude-*` tags) to the metadata keys above. Scans every project-context doc and, for any still carrying github tags, writes the derived metadata and strips the tags in one CAS write per doc (multiple legacy `github-repo:*` tags collapse into the `github_repos` list). Idempotent; `--dry-run` previews without writing. Exit 1 if any doc fails its CAS retries.

### 4.13 `lithos-loom obsidian-sync show`

```
lithos-loom obsidian-sync show [-f text|json] [-c config.toml]
```

Prints the resolved `[obsidian_sync]` block. Used by the capture-task macro to discover the configured `tasks_file` path at runtime, so vaults that customise it get the wikilink target right without editing the macro.

---

## 5. Plugin Contract

Plugins are subprocesses invoked by a route-runner. They receive a small CLI surface and write an atomic `result.json`.

### 5.1 Invocation

```
<command> --task-json <path> --work-dir <path> --result-file <path>
```

- `--task-json`: read-only JSON file. Today its contents are `{"task": <event-payload>}` — the bus event's payload (a Lithos task envelope) wrapped under a single `task` key. The resolved project entry from the local TOML is **not** included in the file; a plugin that needs the project's on-disk repo path uses the `{{repo}}` command token (below) or loads the TOML itself.

  **`{{repo}}` substitution.** Beyond the three path tokens, a route `command` may carry a `{{repo}}` token. The runner resolves it from `[projects.<slug>].repo` keyed by the claimed task's `metadata.project`, before the plugin forks — so one route serves every registered project, and the repo a plugin acts on is derived from the task's own project rather than hard-coded per route. A `{{repo}}` route whose task has no `metadata.project`, or whose slug isn't in `[projects.*]` on this host, is released with a `[BlockerFailed]` finding (`route misconfigured: …`) and never run. Routes without the token don't require a project.
- `--work-dir`: per-task staging directory at `<orchestrator.work_dir>/<task_id>/`. The plugin owns the tree; the runner reads only the result file.
- `--result-file`: path the plugin must write atomically (temp file + fsync + rename). Partial files must never be observable.

Substitution tokens (`{{task_json}}`, `{{work_dir}}`, `{{result_file}}`) in the route's `command` are filled in by the runner before fork.

### 5.2 Result Schema

The full schema is at `docs/result-schema.json` (JSON Schema Draft 2020-12). Required fields: `schema_version` (const 1), `task_id`, `status`, `exit_code`.

```json
{
  "schema_version": 1,
  "task_id": "uuid",
  "status": "succeeded",
  "exit_code": 0,
  "started_at": "2026-05-29T10:00:00Z",
  "finished_at": "2026-05-29T10:05:00Z",
  "worktree": "/abs/path or null",
  "artifacts": { "key": "rel/path or /abs/path" },
  "commits": ["40-char-sha"],
  "spawned_tasks": ["task_id"],
  "metadata_updates": { "pr_url": "https://..." },
  "error": null
}
```

For a failed run, replace `status` with `"failed"` (or `"interrupted"`) and set `error` to an object with the required keys `category` (one of `config`, `environment`, `input`, `agent`, `git`, `github`, `lithos`, `usage_limited`, `internal`) and `message`, plus the optional boolean `retriable`. No other `error` keys are accepted.

An `interrupted` result may additionally carry a `resume` object marking the interruption as retryable:

```json
{
  "resume": {
    "resume_after": "2026-06-12T15:00:00+00:00",
    "run_id": "abc12345",
    "coder_session": "uuid",
    "reviewer_sessions": { "code-quality": "uuid" }
  }
}
```

`resume_after` (required within the block) is the earliest instant a re-run is expected to succeed — the provider's parsed reset time, or a fixed fallback delay when no hint was parseable. The session ids let a future run resume its on-disk transcripts from the retained work dir.

**What the runner does with each field today:**

| Field | Status |
|---|---|
| `schema_version`, `task_id`, `status` | Required by schema; `status` drives the runner's branch (see §2.2). |
| `error.message` | Used as the `[BlockerFailed]` finding text when `status == "failed"`. |
| `resume.resume_after` | Schedules the in-process re-dispatch on `status == "interrupted"` (see §2.2). |
| `exit_code`, `started_at`, `finished_at`, `worktree`, `artifacts`, `commits`, `spawned_tasks`, `metadata_updates`, `error.category`, `error.retriable`, `resume.run_id`, `resume.coder_session`, `resume.reviewer_sessions` | Schema-validated but **currently ignored** by the runner. Plugins may populate them; they have no effect on Lithos today. |

### 5.3 Runner Lifecycle

The route-runner enforces `max_runtime_seconds` (per-route config). On timeout, it sends SIGTERM and waits a grace period; if the plugin hasn't exited, it sends SIGKILL. Result-file absence after exit is treated as a contract violation: the runner posts `[BlockerFailed] route <name>: plugin contract violation: <detail>` and releases the claim.

`retain_failed_workdirs = true` keeps the work directory for triage on failure; on success the work-dir is removed.

### 5.4 Bundled Plugins (scaffolded)

`prd-decompose`, `story-implement`, `story-review-human` are present under `src/lithos_loom/plugins/` as Python modules with prompt files. Their bodies are stubs; they do not yet produce real `result.json` output. The route-runner code path is the load-bearing piece exercised by tests.

### 5.5 story-develop (shipped)

`story-develop` runs the full implement → review → fix → approve loop with containerised agents (one persistent coder session + an N-reviewer panel; per-round commits, objective test gate, usage-limit reactions, optional PR delivery with an autonomous Copilot review round). The full design is `docs/prd/story-develop.md`; the standalone CLI surface is `python -m lithos_loom.plugins.story_develop --help`.

**Daemon mode.** Passing `--task-json` (with `--work-dir` and `--result-file`) switches the plugin to the route-runner contract:

```
uv run python -m lithos_loom.plugins.story_develop \
    --task-json {{task_json}} --work-dir {{work_dir}} --result-file {{result_file}} \
    --repo {{repo}}
```

- `--repo` takes the runner's `{{repo}}` token (§5.1), resolved per task from `[projects.<slug>].repo` keyed by `metadata.project` — so one route serves every registered project. (An absolute path also works if you want a route pinned to one checkout.)
- The task (title, body, `metadata.acceptance_criteria`) comes from `task.json`; `--description` / `--task-id` / `--no-lithos` / `--complete-on-approval` / `--reviewer` / `--develop-config` are rejected in daemon mode.
- **Config lookup.** Reviewer config is resolved from the project-context doc's metadata at `projects/<slug>/<slug>-project-context.md` (slug from `task.metadata.project`; fallback: lexicographically-smallest `project-context`-tagged doc under `projects/<slug>/`). Keys: `develop_reviewers` (pool of `{name, tool, block_threshold?, system_prompt?, fallback_chain?}`), `develop_default_reviewers` (names that run when the task doesn't override), `develop_coder` (`{tool}`), `develop_fallback_chain`, `develop_max_rounds`, `develop_max_cost_usd`. Per-task override: `task.metadata.reviewers` (names from the pool). Every miss — no slug, no doc, no keys, unknown name — degrades to the built-in single `code-quality` reviewer with a `[Friction]` finding; a populated pool without a default selection still runs only the built-in reviewer (pool membership does not auto-run).
- **Status mapping.** `approved` → `succeeded` (the runner completes the task); `interrupted` (usage-limit pause budget exhausted) → `interrupted` with `error.category="usage_limited"` and a `resume` block (the runner schedules a re-dispatch, §2.2); every other stop (`max_rounds`, `stalled`, `disputed`, `cost_exceeded`, `failed`) → `failed`.
- The plugin still owns its Lithos round-trip directly (the `[DevelopResult]` finding + `develop_*` metadata, same as `--task-id` mode); `result.json` carries `status` for the runner, so there is no double-application.

---

## 6. Event Bus Contract

### 6.1 Event Schema

```python
@dataclass(frozen=True)
class Event:
    type: str                   # dotted name, e.g. "lithos.task.created"
    timestamp: datetime         # UTC; when the source published the event
    payload: Mapping[str, Any]  # event-type-specific; see §6.4
```

Events are passed by reference through the in-process bus. Subscribers must not mutate the payload.

### 6.2 Filter Language

Each `[[subscriptions]]` stanza may carry both filters; an event passes when both hold (logical AND):

- **Structural `match.<key>` tables.** A `match.tags = ["X", "Y"]` requires the event payload's task / note to carry every listed tag (superset semantics). Other `match.*` keys check equality on the named payload field.
- **`where = "<python-expression>"`.** The expression is evaluated in a restricted scope exposing `event` (the Event object) and `task` (= `event.payload` for task events). Only safe builtins are available; no imports, no attribute lookups outside the allowed names.

`match` runs first as a cheap structural filter; `where` runs on the survivors.

### 6.3 Dispatch Semantics

- **Concurrent fire-and-forget.** Each subscription has a bounded async queue; events dispatched to a full queue increment a drop counter and emit a WARNING log line. Slow subscribers do not block fast ones.
- **Per-subscription retry.** Failure raises an exception; the runner sleeps `initial_delay` then retries with `exponential` (or `linear`) backoff up to `max_delay`. After `attempts` exhausted, `on_persistent_failure = "friction"` posts a `[Friction]` finding to the related task; `"ignore"` suppresses.
- **Idempotency is the handler's responsibility.** Handlers must be safe under replay (cold-start re-emission, network reconnect with `Last-Event-ID`, daemon restart). Pre-check before mutating Lithos state.

### 6.4 Event Catalog

Payloads are dicts; the exact key set depends on the source. Task-event payloads are the Lithos task envelope as returned by `lithos_task_list` / `task_status` (fields like `id`, `title`, `status`, `tags`, `metadata`, `claims`, and lifecycle timestamps such as `resolved_at`). Note bootstrap events are intentionally minimal (`{id, title, path}`); subscriptions that need the full body or tags re-fetch via `lithos_read`. Subscriptions should treat payloads as opaque dicts and look up specific keys defensively — additional fields may be present and field absence depends on the underlying Lithos response.

| Event type | Source | Payload notes |
|---|---|---|
| `lithos.task.created` | LithosEventStream | Lithos task envelope. |
| `lithos.task.updated` | LithosEventStream | Lithos task envelope (post-edit). |
| `lithos.task.claimed` | LithosEventStream | Lithos task envelope; `claims` lists the active claim. |
| `lithos.task.released` | LithosEventStream | Lithos task envelope after release. |
| `lithos.task.completed` | LithosEventStream | Lithos task envelope; `resolved_at` populated. |
| `lithos.task.cancelled` | LithosEventStream | Lithos task envelope; `resolved_at` populated. |
| `lithos.note.created` | LithosNoteStream | Bootstrap: `{id, title, path}`. Subscriptions that need more re-fetch via `lithos_read`. |
| `lithos.note.updated` | LithosNoteStream | Same shape as `created`. |
| `lithos.note.deleted` | LithosNoteStream | `{id, path}`. |
| `obsidian.task.status_changed` | ObsidianFSWatcher | Carries the prior and new status markers (`[ ]`, `[x]`, `[-]`, `[/]`, `[>]`) and the task id parsed from `🆔 lithos:<id>`. |
| `obsidian.task.priority_changed` | ObsidianFSWatcher | Carries prior and new priority (one of `highest|high|medium|low|lowest|null`). |
| `obsidian.task.due_date_changed` | ObsidianFSWatcher | Carries prior and new `YYYY-MM-DD` date strings (either side may be absent). |
| `obsidian.note.modified` | ObsidianDirWatcher | Carries the doc id parsed from frontmatter, the modified body, and the local `lithos_version`. |
| `github.issue.seen` | GitHubIssueWatcher | `{slug, repo, number, title, body, state, state_reason, labels, author, html_url, updated_at}`. One per issue per poll. The subscription decides create / update / close from the marker + state combo. |

### 6.5 Sources

Sources are async coroutines spawned by their owning child. They consume external input (Lithos SSE, filesystem polls) and publish events.

| Source | Spawned by | Bootstrap | Reconnect |
|---|---|---|---|
| `LithosEventStream` | route-runner + obsidian-sync + github-watcher (independently) | `lithos_task_list(status='open', with_claims=true)` → re-emit `lithos.task.created` per task. | Exponential backoff with `Last-Event-ID` resume. Cursor persisted to `<work_dir>/<child>/sse_cursors.json` so restarts resume from the last drained event. |
| `LithosNoteStream` | obsidian-sync (when `project-context-projection` is configured) + github-watcher | `lithos_list(path_prefix='projects/', tags=['project-context'])` → re-emit `lithos.note.created` per match. | Exponential backoff with `Last-Event-ID` resume. Cursor persisted alongside `LithosEventStream` in the same `sse_cursors.json`. |
| `ObsidianFSWatcher` | obsidian-sync | Polls `<vault>/<tasks_file>` on a 250ms cadence; emits when a line diverges from the last-known state. | n/a (polling). |
| `ObsidianDirWatcher` | obsidian-sync (when `note-push` is configured) | Walks `<vault>/<projects_dir>/**/*.md` on the same cadence; computes body-only hashes. | n/a. Excludes files ending in `-done.md` (the per-project archive). |
| `GitHubIssueWatcher` | github-watcher | Reads `note_list(path_prefix='projects/', metadata_match={'github_watch_enabled': true})` to build the slug → repos watch list (a project may map several repos); loads per-repo `updated_at` cursors from `coord_doc_path`. | n/a (polling). Per-repo 404 drops the repo with a `[Friction]` log; 403 + `X-RateLimit-Remaining: 0` sleeps until `X-RateLimit-Reset`. |

### 6.6 Subscription Action Registry

Subscriptions resolve their `action` field against the `lithos_loom.subscriptions.handlers` Python entry-point group:

| Action | Module | Consumes | Effect |
|---|---|---|---|
| `noop` | `_noop` | any | Logs at DEBUG. Useful for tracing. |
| `obsidian-projection` | `_obsidian_projection` | `lithos.task.*` | Rewrites `<vault>/<tasks_file>`. |
| `obsidian-status-transition` | `_obsidian_status_transition` | `obsidian.task.status_changed` | `[ ]→[x]` calls `lithos_task_complete`; `[ ]→[-]` calls `lithos_task_cancel`; `[x]→[ ]` posts `[ReopenRequested]` finding; `[/]` / `[>]` are no-op (logged). |
| `obsidian-priority-changed` | `_obsidian_priority_changed` | `obsidian.task.priority_changed` | `lithos_task_update(metadata={priority: ...})`. |
| `obsidian-due-date-changed` | `_obsidian_due_date_changed` | `obsidian.task.due_date_changed` | `lithos_task_update(metadata={scheduled_for: ...})`. |
| `project-context-projection` | `_project_context_projection` | `lithos.note.*` | Re-fetches via `lithos_read`, writes `<vault>/<projects_dir>/<slug>/<filename>.md` atomically. |
| `note-push` | `_note_push` | `obsidian.note.modified` | `lithos_write(id, content, expected_version)`; on conflict, runs the conflict resolver. |
| `task-archive` | `_task_archive` | `lithos.task.completed` / `lithos.task.cancelled` | Appends a Tasks-plugin line to `<vault>/<projects_dir>/<slug>/<slug>-done.md` (O_APPEND); marks the task as archived so the projection evicts it on next flush. |
| `github-issue-sync` | `_github_issue_sync` | `github.issue.seen` | Auto-wired by the github-watcher child (not declared in `[[subscriptions]]`). Resolves the `<!-- lithos:<id> -->` marker, then creates / closes / no-ops + GH → Lithos drift (title / body / labels) per §2.2. Reopen on a terminal task posts `[ReopenRequested]` once (de-duped via `metadata.github_state_snapshot`). |
| `github-issue-push` | `_github_issue_push` | `lithos.task.created` / `lithos.task.completed` / `lithos.task.cancelled` / `lithos.task.updated` | Auto-wired by the github-watcher child. Mirrors Lithos terminal status to a GH close with the matching `state_reason`; mirrors title renames from Lithos → GH on `task.updated` and (for bootstrap-replayed open tasks) `task.created`. Idempotent re-fetch dodges redundant PATCHes when the GH → Lithos path already converged. Consumer retries transient GH failures with exponential backoff capped at 60s, up to 8 attempts before dropping `[Friction]`. |

Third-party handlers can be registered via Python entry points. Each handler receives an `Event` and a `SubscriptionContext` carrying a shared `LithosClient`, a scoped `logging.Logger`, and the orchestrator's `agent_id`.

---

## 7. Obsidian Projection

### 7.1 File Layout

```
<vault_path>/
├── <tasks_file>                              # default: _lithos/tasks.md
├── <projects_dir>/                           # default: _lithos/projects/
│   ├── <slug>/
│   │   ├── <slug>-project-context.md         # canonical project doc (per Lithos KB convention)
│   │   ├── <other-file>.md                   # any additional project-context-tagged doc
│   │   └── <slug>-done.md                    # task-archive's append-only history (vault-only)
│   └── _unassigned/
│       └── _unassigned-done.md               # archive bucket for tasks with missing metadata.project
└── _lithos/conflicts/                        # note-push conflict snapshots
```

All writes use a dot-prefixed temp file (`.<filename>.tmp.<rand>`) plus `os.replace` for atomicity. The dot prefix matters: Obsidian Sync (and Dropbox-style observers) skip dotfiles, avoiding a publish noise.

### 7.2 Tasks-Plugin Line Shape

Open-task line:

```markdown
- [ ] <title> 🆔 lithos:<id> [#project/<slug>] [#lithos/<route-name>] [⛔ lithos:<dep_id>...] [🔺⏫🔼🔽⏬] [📅 YYYY-MM-DD]
```

Resolved-task line (completed / cancelled):

```markdown
- [x] <title> 🆔 lithos:<id> [#project/<slug>] ✅ YYYY-MM-DD
- [-] <title> 🆔 lithos:<id> [#project/<slug>] ❌ YYYY-MM-DD
```

The renderer always emits fields in this exact order. Priority, deps, and due-date markers are dropped on resolved lines; the resolved-date marker is always last so the Tasks plugin's `sort by done date` / `done after` filters parse correctly. Operator-side tags from `task.tags` are NOT rendered today.

| Token | Meaning | Source | Direction |
|---|---|---|---|
| `[ ]` / `[x]` / `[-]` | Status: open / completed / cancelled | `task.status` | Bidirectional. `[/]` and `[>]` are detected on read, no-op on write. |
| `🆔 lithos:<id>` | Task ID | `task.id` | One-way (identity; never edited by operator). |
| `#project/<slug>` | Project tag | `task.metadata.project` | One-way. |
| `#lithos/<route-name>` | Active human-blocking claim's route | route lookup based on the active claim | One-way; surfaces while a human-blocking route holds the claim. |
| `⛔ lithos:<dep_id>` | One marker per `metadata.depends_on` entry | `task.metadata.depends_on[]` | One-way (Lithos canonical). |
| `🔺⏫🔼🔽⏬` | Priority (highest / high / medium / low / lowest) | `task.metadata.priority` | Bidirectional. Absent emoji = no priority. |
| `📅 YYYY-MM-DD` | Due date | `metadata.scheduled_for` if set; else `today` for human-blocking tasks; else absent | Bidirectional via `metadata.scheduled_for`. |
| `✅ YYYY-MM-DD` | Completed date | `task.resolved_at` | One-way; only rendered for `[x]` lines within TTL. |
| `❌ YYYY-MM-DD` | Cancelled date | `task.resolved_at` | One-way; only rendered for `[-]` lines within TTL. |

### 7.3 Projection Filter

A task is projected when `is_human_actionable(task, routes)` returns true:

- The task is `open`, AND
- Either (a) no `[[routes]]` matches the task's tags, OR (b) a route matches AND that route has `human_blocking = true` AND the route currently holds the claim.

Dependency-blocked tasks still project (with the `⛔` marker); the Tasks plugin's own queries decide whether to surface or hide them.

Tasks with terminal status (`completed` / `cancelled`) project with `[x]` / `[-]` and the corresponding `✅` / `❌` date marker, lingering until either (a) the `task-archive` subscription evicts them on the next flush after archiving, or (b) `resolved_ttl_days` elapses since `resolved_at`.

### 7.4 Project-Context Projection

Each `project-context`-tagged note under `projects/` projects to one file at `<vault>/<projects_dir>/<slug>/<filename>.md`, where:

- `<slug>` = the directory name under Lithos's `knowledge/projects/<slug>/`.
- `<filename>` = the slug of the doc's `title` (Lithos slugifies title → filename).

Frontmatter envelope:

```yaml
---
lithos_id: <uuid>
lithos_version: <int>
lithos_updated_at: <ISO 8601>   # omitted when the note has no updated_at
slug: <directory-name>          # omitted when the note has no slug
status: <whatever Lithos returned>   # omitted when null; common values: active, archived, quarantined
tags:                           # omitted when empty
  - project-context
  - ...
---
# <title>

<body>
```

Key order is stable (`lithos_id` → `lithos_version` → `lithos_updated_at` → `slug` → `status` → `tags`). Optional rows are omitted entirely rather than rendered as `null` or `[]`. `status` is whatever Lithos returned for the note — Loom passes it through verbatim. The body below the frontmatter is the Lithos doc body, prefixed with `# <title>`. Frontmatter is daemon-managed; operator edits to frontmatter fields are not pushed back. Body edits are pushed via `note-push` (see §7.5).

Filename migration: if Lithos changes the doc's title (changing the slug), the projection writes the new path first, then unlinks the old path. Order matters — a failed new write leaves both copies on disk rather than losing the content.

### 7.5 Bidirectional Note Push

`ObsidianDirWatcher` polls projected files. When the body-only hash diverges from the projection's last write, it emits `obsidian.note.modified` with the operator's body and the current local `lithos_version`. `note-push`:

1. Fetches canonical via `lithos_read(id)` for current title / tags / status.
2. Calls `lithos_write(id, content=body, expected_version=local_version)`.
3. On `status=updated`: re-fetches via `lithos_read` to refresh the local frontmatter (`lithos_version`, `lithos_updated_at`).
4. On `status=version_conflict`: invokes the conflict resolver — moves the operator's body to `<vault>/_lithos/conflicts/<slug>.<file>.<ts>.md`, writes canonical to the original path, logs `[Friction]` WARNING.
5. On `status=duplicate`: no-op (Lithos detected the body is identical to canonical).

**Frontmatter-only edits are silently absorbed.** The watcher hashes the body only; adding a custom YAML field (e.g. a Dataview field) does not trigger a push. Custom fields persist until the next projection rewrite, at which point the renderer reconstructs frontmatter from scratch and the custom field is lost. This is by design — frontmatter is daemon-managed.

**Cold-start divergence.** If the daemon was down while the operator edited a projected file, the bootstrap projection detects the local-vs-canonical body diff and routes through the same conflict resolver (operator's body preserved in `_lithos/conflicts/`, canonical pulled to the original path).

### 7.6 Per-Project Task Archive

When `task-archive` is configured, the `obsidian-projection` handler also marks tasks as "surfaced" in an in-memory map whenever it writes a task line. On `lithos.task.completed` / `lithos.task.cancelled`, the `task-archive` handler:

1. Skips tasks that were never surfaced (background / route-claimed-only work).
2. Resolves the target file from `task.metadata.project`; falls back to `_unassigned-done.md` if missing or unknown.
3. Renders one Tasks-plugin line (terminal-status drops priority + due-date markers; `✅` or `❌` carries `task.resolved_at`).
4. Dedups against existing lines in the file (lazy-read on first event per project).
5. O_APPEND writes the line.
6. Marks the task as archived so the projection evicts it from `tasks_file` on next flush.

The done file is **vault-only and append-only** — the dir-watcher excludes the `-done.md` suffix so operator edits are inert. Deleting a done file can be recovered with `project regenerate-done` (which rebuilds from Lithos, all-resolved-tasks superset).

### 7.7 Filter Knobs

- **`include_blocked`** (default `true`): when `false`, tasks with non-empty `metadata.depends_on` are not projected.
- **`exclude_tags`** (default `[]`): tasks carrying any listed tag are not projected. Useful for suppressing automation noise (e.g. `["influx:run", "influx:backfill"]`).
- **`resolved_ttl_days`** (default `7`): how long resolved tasks linger in `tasks_file` when `task-archive` is NOT configured, OR (when `task-archive` IS configured) the bootstrap-replay window the archiver looks back over on restart.

---

## 8. Finding Prefixes

Loom posts findings with stable prefixes so operators (and `lithos-lens`) can grep machine-parseably. The prefixes emitted today:

| Prefix | Posted by | Meaning |
|---|---|---|
| `[Friction]` | any subscription | Persistent failure of a side effect (retry exhausted) OR a notable operator-visible event (e.g. note-push conflict). |
| `[ReopenRequested]` | `obsidian-status-transition` and `github-issue-sync` | An operator unticked a completed task (Obsidian) OR reopened a closed GH issue linked to a terminal Lithos task. Lithos has no reopen primitive, so this signals the intent. |
| `[BlockerFailed]` | route-runner | Plugin failed, timed out, violated the contract, or returned an unknown status. The claim was released. |

---

## 9. Errors and Exit Codes

CLI commands use a unified exit code convention:

| Code | Meaning |
|---|---|
| `0` | Success (or clean user abort at a `--yes`-gateable prompt). |
| `1` | Operational failure — Lithos call failed, config load failed, slug collision, missing project, partial-import failure, network unreachable. |
| `2` | Input validation failure — invalid flag combination, unknown project, unknown priority, malformed task lines, cross-project tag, empty parent, `lithos_id` / `--slug` mismatch, unreadable source file. |

`lithos-loom run` exits `0` on clean shutdown, non-zero on child crash or SIGKILL after timeout.

### 9.1 Common Validation Failures

- **`unknown project '<slug>'`** (exit 2, `task create`): the `--project` value isn't in the union of (a) slugs from `lithos_list(path_prefix='projects/', tags=['project-context'])` and (b) the TOML `[projects.<slug>]` registry. The TOML side lets a host run capture against a slug that hasn't yet been promoted to a project-context doc in Lithos.
- **`unknown priority '<value>'`** (exit 2, `task create`): `--priority` must be one of `highest|high|medium|low|lowest`.
- **`--target-file and --no-insert are mutually exclusive`** (exit 2, `task create`).
- **`slug '<X>' already exists at doc <id>`** (exit 1, `project create`): refuses to overwrite. Use `project import --tasks-only --slug <X>` if you wanted to add tasks instead.
- **`no project at slug '<X>'`** (exit 1, `project import --tasks-only`): includes near-miss suggestions when typo distance ≤ 2.
- **`lithos_id resolves to project Y; --slug=X; refusing`** (exit 2, `project import --tasks-only`): the source file's frontmatter `lithos_id` points at a different project than `--slug`.
- **`obsidian_sync.vault_path must be a non-empty path string`** (exit 1, validate-config): the `[obsidian_sync]` block is malformed.
- **`tasks_file must be relative to vault_path`** (exit 1, validate-config): absolute paths in `tasks_file` are rejected.

### 9.2 Runtime Failures

- **`plugin <pid> did not honour SIGTERM within 5.0s; sent SIGKILL`** (route-runner WARNING): the plugin exceeded `max_runtime_seconds` and didn't shut down. The claim is released and `[BlockerFailed]` is posted.
- **`plugin contract violation: did not write <path>`** (route-runner WARNING): the plugin exited but no `result.json` exists. `[BlockerFailed]` posted; claim released.
- **`note-push conflict for doc=<id>`** (note-push `[Friction]`): operator and Lithos both edited the doc; the operator's body is preserved at `_lithos/conflicts/<slug>.<file>.<ts>.md`, canonical was pulled to the original path.
- **`obsidian-projection: skipped <slug>: no project-context doc in Lithos`** (doctor): TOML registers a slug Lithos doesn't know. Either create the doc in Lithos or remove the TOML stanza.

---

## 10. Lithos Prerequisites

Loom requires a Lithos server exposing the MCP-over-SSE surface plus these primitives:

| Surface | Used for |
|---|---|
| `lithos_task_list(status='open', with_claims=true)` | Source bootstrap. |
| `lithos_task_status`, `_create`, `_complete`, `_cancel`, `_update`, `_claim`, `_release` | Task lifecycle. |
| `lithos_task_create(metadata=...)` | Single-shot create with metadata (post `agent-lore/lithos#295`). |
| `lithos_finding_post` | `[Friction]` / `[ReopenRequested]` / `[BlockerFailed]` breadcrumbs. |
| `lithos_write(id=..., expected_version=...)` | Note push with optimistic locking; `version_conflict` envelope drives the conflict resolver. |
| `lithos_read`, `lithos_list(path_prefix=...)`, `lithos_delete` | Project-context projection + CLI surface. |
| `task.metadata` field on tasks | All `metadata.*` references throughout (priority, scheduled_for, project, depends_on, parallelizable, etc.). |
| `task.updated` event (minimal `{task_id}` payload) | Cache-invalidation signal; `LithosEventStream` force-refreshes via `task_list(status='open')` to pick up the new field values. Other task events are served from cache where possible. |
| `note.created` / `note.updated` / `note.deleted` events on `GET /events` SSE | Project-context projection. |

Slug = directory name under `knowledge/projects/<slug>/`. Lithos enforces uniqueness with a `slug_collision` envelope; Loom relies on this rather than a frontmatter field.

---

## 11. Multi-Host Deployment

The vault host (typically a workstation with Obsidian) runs the full daemon — the supervisor spawns both route-runner and obsidian-sync children. Other hosts (additional workstations, headless servers) run with `[obsidian_sync]` omitted; the supervisor spawns only the route-runner child.

There is no inter-host coordination. Each host:

- Registers as `lithos-orchestrator-<host>` via `orchestrator.agent_id`.
- Reads its own TOML config (different `[projects.<slug>]` registry, different routes).
- Claims tasks competitively via `lithos_task_claim` (Lithos guarantees collision safety).

Per-project automation (the `[projects.<slug>].repo` field) is host-specific. If host A doesn't have the repo checked out, it can't claim tasks for that project. Project existence is a Lithos fact (the project-context doc); project automation is a host fact (the TOML entry).

Obsidian Sync (the app) handles delivering the vault to the operator's secondary devices (laptop, phone). Loom doesn't see those devices.

---

## 12. Not Implemented

The following are absent from the current surface. Some are explicit non-goals; others are queued in `docs/prd/`. Listed here so readers don't go looking.

- **Plugin bodies for `prd-decompose`, `story-implement`, `story-review-human`.** Scaffolding under `src/lithos_loom/plugins/`; no real logic.
- **Application of `result.json` fields beyond `status`.** `metadata_updates`, `artifacts`, `commits`, `spawned_tasks`, `exit_code`, `error.retriable` are schema-validated but not used by the runner today (§5.2).
- **`orchestrator.max_concurrency` enforcement.** Parsed and stored but never read at runtime — there is no global cap on concurrent plugin runs. A single route runs its tasks serially; multiple routes run concurrently without contending. Tracked in [#85](https://github.com/agent-lore/lithos-loom/issues/85).
- **Resolved project entry in `task.json`.** Plugins receive `{"task": <payload>}` only.
- **Startup reclaim of stale claims.** Claims age out via Lithos's own TTL; the route-runner does not actively release them on startup.
- **Hot-reload of TOML config.** Operator restarts the daemon.
- **Persistent event log.** Restart relies on source re-authority + subscriber idempotency.
- **Containerised daemon.** Loom runs as a host process; Lithos and adjacent services may run in docker.
- **Other planned work** (`prd-generate`, agent-driven reviews, brain, `merge-stories`, A2A endpoint, GitHub issue watcher, multi-host PRD-affinity, docker sandbox, cost tracking). See `docs/prd/` for PRDs.

---

## Appendix A: Worked Example — Capture a Task from Obsidian

1. Operator highlights "Review staging deploy" in any note, fires the capture-task hotkey.
2. The Templater macro shells out to `lithos-loom project list --format json` to populate the project dropdown, and `lithos-loom obsidian-sync show --format json` to learn the configured `tasks_file` path.
3. Modal opens; operator selects project `lithos-loom`, optionally fills priority and tags, submits.
4. Macro shells out to `lithos-loom task create --project lithos-loom --title "Review staging deploy" --no-insert`; CLI prints the new task_id.
5. Macro inserts a wikilink at cursor: `[[_lithos/tasks.md|Review staging deploy]] 🆔 lithos:<id>`.
6. Meanwhile: Lithos broadcasts `task.created` via SSE → the daemon's `LithosEventStream` receives it → `obsidian-projection` re-renders `<vault>/_lithos/tasks.md` with the new line.
7. Total elapsed: ~250–500ms from submit to projected line landing.

## Appendix B: Worked Example — Bidirectional Project-Context Edit

1. Operator opens `<vault>/_lithos/projects/lithos-loom/lithos-loom-project-context.md` in Obsidian and edits the body.
2. Saves. `ObsidianDirWatcher` polls every 250ms; on next tick, the body hash diverges from `sync_state.note_body_hashes[doc_id]`.
3. Watcher emits `obsidian.note.modified` with the operator's body and the local `lithos_version`.
4. `note-push` calls `lithos_write(id, content=body, expected_version=local_version)`.
5. Lithos returns `status=updated` with the bumped version.
6. `note-push` calls `lithos_read(id)` to refresh `lithos_version` and `lithos_updated_at` in the local frontmatter.
7. The dir-watcher detects the post-write file change but matches it against its last-known mtime + content hash → suppresses as a self-write.

If a separate agent had pushed a body change between steps 1 and 4, step 5 would return `status=version_conflict`. The resolver moves the operator's body to `_lithos/conflicts/lithos-loom.lithos-loom-project-context.<ts>.md`, writes canonical to the original path, and posts a `[Friction]` WARNING. The operator can diff the two files to recover their edit.
