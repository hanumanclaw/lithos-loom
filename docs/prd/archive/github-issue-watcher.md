---
title: Lithos Loom — GitHub Issue Watcher
milestone: Track 1 (Slices 7.1 + 7.2)
status: draft
target_version: 0.3.0
references:
  - docs/SPECIFICATION.md (event bus, projection rules, finding prefixes this PRD extends)
  - docs/prd/archive/integration.md (Obsidian bridge PRD — shipped; D7 default-off scoping)
  - docs/prd/archive/task-archive.md (per-project archive shape this reuses — shipped)
  - docs/prd/full.md (where GH was originally scoped as A7)
labels: [needs-triage, lithos-loom, github, integration]
---

# Lithos Loom — GitHub Issue Watcher

## Problem Statement

Project work is bifurcated across two surfaces today:

- **Lithos** is the canonical store for tasks the operator surfaces deliberately (via the capture macro, MCP writes, Track 2 plugin outputs).
- **GitHub** receives external collaboration: contributor reports, automated alerts (dependabot, security scans), CI failures, downstream-team requests.

Issues filed on GitHub never reach Lithos unless the operator manually copies them in. Result: the operator's daily Lithos view (`_lithos/tasks.md` in Obsidian) doesn't include externally-reported work, so prioritisation happens in two places. Closure too: when an issue is fixed and closed on GH, the operator has to remember to also close any Lithos task they manually created for it — or accept drift between the two surfaces.

The asymmetric direction matters: **every GH issue should be tracked in Lithos** (Lithos is the operator's prioritisation surface, and external work needs to show up there), but **not every Lithos task should generate a GH issue** (most Lithos tasks are internal — capture-macro thoughts, Track 2 plugin sub-tasks, autonomous coding work). Auto-promoting every Lithos task to GH would dump noise into a public surface and conflate "operator's own todos" with "things contributors need to see."

## Solution

A new `github-issue-watcher` source + `github-issue-sync` subscription that polls watched GH repos, creates Lithos tasks for new issues, maintains a back-link via an HTML-comment marker in the issue body, and mirrors closure state between the two surfaces. Repo identity lives in Lithos project-context metadata (canonical across all hosts); the watcher subprocess gates on a per-host TOML knob so only one host actually runs the polling loop.

Two slices, mirroring how Slice 4 (one-way pull) preceded Slice 5 (bidirectional) for project-context docs:

- **Slice 7.1 — Inbound mirror (MVP).** Polling + bootstrap reconcile + new-issue import + GH→Lithos close mirror + project metadata storage + CLI.
- **Slice 7.2 — Bidirectional close + ongoing sync.** Lithos→GH close mirror, ongoing title/body/label sync, reopen-request handling.

### Flow per new GH issue

```
operator (or external contributor) opens issue at github.com/owner/repo/issues/N
  └─> github-issue-watcher polls (default 60s)
      └─> for each new/changed issue since last cursor:
          ├─> check for existing linkage: `<!-- lithos:<task_id> -->` in body
          │     OR Lithos task with `metadata.github_issue_url` matching
          ├─> if linked → reconcile (close state, title) per below
          └─> if not linked AND issue is open:
              ├─> resolve project: find project-context doc with
              │     `metadata.github_repo == "owner/repo"`
              │     - if no match → log [Friction], skip
              ├─> filter: apply exclude_labels + exclude_authors
              │     from the project's frontmatter; if filtered → skip
              ├─> create Lithos task:
              │     title    = issue.title
              │     description = issue.body
              │     tags     = issue.labels (snapshotted in
              │                metadata.github_labels for drift tracking)
              │     metadata = {
              │       project: <slug>,
              │       github_issue_url: <url>,
              │       github_issue_number: N,
              │       github_labels: [<labels>],   # for drift tracking
              │     }
              └─> update issue body: append/replace
                  `<!-- lithos:<task_id> -->` line
```

### Flow on GH close

```
GH issue closed (close_reason = "completed" | "not_planned")
  └─> watcher poll picks up state change
      └─> look up linked Lithos task via marker
      └─> close_reason == "completed"   → ctx.lithos.task_complete(...)
          close_reason == "not_planned" → ctx.lithos.task_cancel(...)
      └─> task-archive (Slice 6) catches the lithos.task.{completed,cancelled}
          event and appends to per-project done file; global projection
          evicts the task on next flush (per task-archive D32)
```

### Flow on Lithos close (Slice 7.2)

```
operator ticks `[x]` in _lithos/tasks.md → obsidian-status-transition →
lithos_task_complete → lithos.task.completed event
  └─> github-issue-sync subscription consumes the event
      └─> if task has metadata.github_issue_url:
          ├─> task.status == completed → close GH issue as completed
          └─> task.status == cancelled → close GH issue as not_planned
```

## Locked Design Decisions

| # | Area | Decision |
|---|------|----------|
| D44 | Source mechanism | Polling via `gh` CLI / GitHub REST + GraphQL APIs. Per-repo cursor (most-recent `updated_at`) so each poll is incremental, not a full repo walk. Default 60s interval. Webhooks deferred to a follow-up PRD if/when soak surfaces latency as friction |
| D45 | Repo config storage + write path | Per-project storage in Lithos `note.metadata.github_repo = "owner/name"` + `github_issues_enabled = true` on the project-context doc. Projection extends D25's frontmatter shape to include these fields for vault visibility (read-only — operator edits don't push back, mirroring D28). New CLI `lithos-loom project set-github-repo <slug> <owner/name>` calls `lithos_write` with a metadata update. Canonical across all hosts; no per-host TOML mapping |
| D46 | Linkage marker | HTML comment `<!-- lithos:<task_id> -->` appended to the issue body. Invisible in GitHub's rendering. Parser is tolerant of placement (append or prepend) and case (`<!-- LITHOS: ... -->` also matches) but writer always writes the canonical lowercase-at-end shape. If the operator deletes the marker, next poll detects the unlinked issue + matching `metadata.github_issue_url` Lithos task → re-writes the marker rather than creating a duplicate |
| D47 | Closure mapping | Asymmetric and bidirectional. GH closed-as-`completed` ↔ Lithos `completed`; GH closed-as-`not_planned` ↔ Lithos `cancelled`. Both directions preserved by reading the GH `state_reason` field and Lithos `task.status` |
| D48 | Issue filter | Default: import all open issues from a watched repo. Project-context frontmatter carries optional `github_issue_exclude_labels = ["automated", "renovate"]` and `github_issue_exclude_authors = ["dependabot[bot]", "renovate[bot]"]`. Issues matching either list are skipped at import time and never reconciled |
| D49 | Unmapped repo | An issue from a repo with no matching project-context doc (no doc has `metadata.github_repo == "owner/repo"`): skip + post a `[Friction]` log line naming the repo. No silent creation; no `_unassigned` fallback. The watcher's repo list is itself derived from project-context metadata, so this case only fires if there's a race (project doc deleted between cursor write and next poll) |
| D50 | Host gating | TOML `[github_watcher] enabled = true` per-host. Only one host at a time should have this enabled (typically the same host that runs `obsidian-sync`). The supervisor's spawn gate is just this flag; no Lithos-coordinated election. Operators with multiple hosts pick one to be the watcher manually |
| D51 | Title/body drift sync (Slice 7.2) | Title is **bidirectional** — operator can rename on either side; the other surface mirrors on next reconcile. Body is **one-way GH→Lithos** (mirror to `task.description`). Loom's writes to the GH body are limited to maintaining the linkage marker; Loom does not push Lithos `task.description` changes back to the GH body |
| D52 | Labels → tags (Slice 7.2) | One-way GH→Lithos. On each reconcile, snapshot the GH labels into `metadata.github_labels` and compute the diff vs the previous snapshot; replace the corresponding tag entries on the Lithos task with the new GH labels. Operator-added Lithos tags (not in any `github_labels` snapshot) coexist and persist across reconciles. Lithos-side tag changes do NOT push to GH labels |
| D53 | PRs excluded | The watcher filters PRs at the API level (search filter `type:issue`). PRs have their own review-state lifecycle that's orthogonal to issues and isn't a clean fit for Lithos's task lifecycle. Track 2's coding plugins create PRs but the operator doesn't need to triage them as tasks — they're the OUTPUT of tasks, not new tasks |
| D54 | Reopen of closed GH issue (Slice 7.2) | Mirror D17: post a `[ReopenRequested]` finding on the (now-completed-in-Lithos) task. Same workaround Slice 2 uses for Obsidian unticks. Awaiting upstream `agent-lore/lithos#243` for proper task-reopen |
| D55 | Last-poll cursor persistence | Per-repo `updated_at` cursor stored as `metadata.github_last_poll[<repo>] = "<iso-datetime>"` on a daemon-owned coordination doc (`projects/_lithos-loom-internal/github-watcher-state.md`). Survives daemon restarts; avoids re-walking the full open-issue list on cold start. The coord doc lives under `projects/` so the existing project-context-projection projects it into the vault for visibility (read-only — operator never edits) |

## Relationship to Existing Track 1 Architecture

This feature **extends** Slices 4 + 5 + 6 with one new source, one new subscription pair, and one CLI subcommand. Lithos-side dependencies are all already in place:

- **Reuses** `lithos_task_create` / `task_complete` / `task_cancel` / `task_update` / `finding_post` (already wired in Slice 2).
- **Reuses** `lithos_write` for the project-context metadata updates (Slice 4 added the surface).
- **Reuses** the project-context-projection (Slice 4) — the new metadata fields surface in the projected frontmatter automatically once the renderer reads them.
- **Reuses** the task-archive subscription (Slice 6) — closed GH issues become Lithos terminal-status tasks, which the archiver then writes to the per-project done file. No new archive logic needed.
- **Reuses** the `gh` CLI / GitHub API surface the capture macro already requires the operator to have installed for the daemon's PATH check.

No Lithos upstream blockers. Specifically, `lithos_task_reopen` (#243) is NOT a blocker — we mirror D17 for the reopen case.

## User Stories

### Slice 7.1 — Inbound mirror (MVP)

55. As a maintainer, I want a `GitHubIssueWatcher` source that polls watched repos at a configured interval (default 60s) using the GitHub REST API with per-repo `since` cursors, so that incremental reconciliation is cheap and the rate-limit budget covers 50+ repos.

56. As an operator, I want the watcher to bootstrap on daemon start by walking every open issue in each watched repo, checking for a `<!-- lithos:<task_id> -->` marker or a Lithos task with matching `metadata.github_issue_url`, and creating a Lithos task + writing the marker for any unlinked issue, so that my existing-but-unlinked issues all become Lithos tasks without manual intervention.

57. As an operator, I want new GH issues that appear after the watcher started to also be reconciled — same path as bootstrap, just driven by the per-poll cursor advance — so that the daily flow keeps Lithos in sync without further action.

58. As an operator, I want each imported Lithos task to carry `metadata.github_issue_url`, `metadata.github_issue_number`, `metadata.project = <slug>`, and the GH labels as `tags` (snapshot in `metadata.github_labels`), so that the task is queryable, traceable, and auto-projects via the existing obsidian-projection subscription.

59. As an operator, I want imported tasks to have their `title` = GH issue title and `description` = GH issue body at creation time, so that I can triage them in Obsidian without clicking through to GitHub for context.

60. As an operator, I want a closed GH issue to drive Lithos closure: `close_reason == "completed"` → `task_complete`; `close_reason == "not_planned"` → `task_cancel`, so that the two surfaces stay aligned without my having to remember the second close.

61. As an operator, I want a new CLI subcommand `lithos-loom project set-github-repo <slug> <owner/name>` that calls `lithos_write` with a metadata update on the project-context doc, so that I have an explicit, scripted way to enable GH watching on a project without editing TOML or driving MCP directly.

62. As an operator, I want a complementary `lithos-loom project enable-github <slug>` / `disable-github <slug>` pair that flips `metadata.github_issues_enabled`, so that I can toggle watching on/off per-project without unsetting the repo mapping.

63. As an operator, I want project-context frontmatter to include `github_repo` and `github_issues_enabled` (read-only — operator edits don't push back, mirroring D28), so that the configuration is visible from inside Obsidian when I open the projected file.

64. As an operator, I want optional `metadata.github_issue_exclude_labels` and `github_issue_exclude_authors` lists on the project-context doc, so that I can suppress noise from automated issue creators (dependabot, renovate) without disabling the watcher entirely for the project.

65. As an operator, I want issues from a repo that no project-context doc references via `github_repo` skipped with a `[Friction]` log line naming the repo, so that misconfiguration surfaces explicitly and I'm not surprised by silent ignores.

66. As a maintainer, I want the watcher subprocess gated by a TOML `[github_watcher] enabled = true` knob on whichever Loom host should run the watcher (typically the same host that runs `obsidian-sync`), so that multi-host deployments don't race to create duplicate Lithos tasks for the same GH issue.

67. As a maintainer, I want the watcher to persist per-repo `updated_at` cursors as Lithos metadata on a daemon-owned coordination doc (`projects/_lithos-loom-internal/github-watcher-state.md`), so that daemon restart doesn't re-walk the entire open-issue list and the cursor survives across hosts if the operator moves the watcher.

68. As a maintainer, I want the watcher to filter PRs at the API level (`type:issue`), so that PR review-state events don't leak into the issue reconciliation path.

69. As a maintainer, I want the watcher's marker parser to be tolerant of placement (top/bottom of body) and case, but to write the canonical `<!-- lithos:<task_id> -->` shape at the end on update, so that operator-edited body content survives reconciliation but the marker drifts back to canonical form over time.

70. As a maintainer, I want the watcher to handle GitHub's 5000-reqs/hr rate limit gracefully — on 403 with `X-RateLimit-Remaining: 0`, back off until the reset timestamp, log the wait, and resume — so that a busy poll cycle doesn't fail the watcher subprocess.

### Slice 7.2 — Bidirectional close + ongoing sync (follow-up)

71. As an operator, I want Lithos task completion (via Obsidian tick, MCP call, or any other surface) to close the linked GH issue with the matching `state_reason` (completed/not_planned), so that closing on either surface propagates to the other.

72. As an operator, I want GH issue title changes to mirror to the Lithos task title (and vice versa) on the next reconcile, so that renames on either surface don't desynchronise the two views.

73. As an operator, I want GH issue body changes to mirror to the Lithos task description on the next reconcile (one-way GH→Lithos), so that updated context from contributors is visible in Lithos without my clicking through.

74. As an operator, I want GH label changes to mirror to the Lithos task tags on each reconcile, with operator-added Lithos tags preserved alongside GH-derived tags via the `metadata.github_labels` snapshot diff, so that label edits propagate without clobbering operator intent.

75. As an operator, I want a reopened GH issue (closed → open transition on a previously-completed Lithos task) to post a `[ReopenRequested]` finding on the Lithos task per D54, so that I have a signal to revisit the task until upstream `lithos_task_reopen` lands.

## Implementation Decisions

### Modules to build

- **`lithos_loom.sources.github_issue_watcher`** (new) — async polling source. Constructed with the project-list (queried from Lithos at startup + on `lithos.note.updated` for project-context docs). Per-repo cursor management. Emits `github.issue.{created,updated,closed,reopened}` events onto the bus.

- **`lithos_loom.subscriptions._github_issue_sync`** (new) — stateful handler factory. Consumes the watcher's events. Creates Lithos tasks on `created`, mirrors closure on `closed`, posts `[ReopenRequested]` on `reopened`. Slice 7.2 adds the reverse direction (consumes `lithos.task.{completed,cancelled,updated}` to drive GH).

- **`lithos_loom.children.github_watcher`** (new) — subprocess child mirroring the shape of `obsidian_sync`. Gated by `[github_watcher] enabled = true`. Spawns the source + subscription.

- **`lithos_loom.config.GitHubWatcherConfig`** (new) — TOML schema for the `[github_watcher]` block: `enabled`, `poll_interval_seconds`, `coord_doc_path` (default `projects/_lithos-loom-internal/github-watcher-state.md`).

- **`lithos_loom.cli.project`** (extend) — new `set-github-repo` / `enable-github` / `disable-github` subcommands. Reuses the existing `_create_project_async` plumbing for the metadata updates.

- **`lithos_loom.render_project_context`** (extend) — D25 frontmatter shape adds `github_repo`, `github_issues_enabled`, and the two `github_issue_exclude_*` lists. Read-only on the operator side (D28).

- **`lithos_loom.github_client`** (new) — thin async wrapper over the GitHub API. Either `httpx` + raw REST, OR shell out to `gh` CLI for each call. Probably REST direct so we get proper async + cursor handling; `gh` is fallback if auth gets tricky.

### Coordination doc shape

```yaml
---
lithos_id: <uuid>
lithos_version: <n>
slug: _lithos-loom-internal
status: active
tags:
  - lithos-loom-internal
  - github-watcher-state
github_last_poll:
  lithos/lithos-loom: "2026-05-25T14:30:00Z"
  lithos/lithos: "2026-05-25T14:30:00Z"
---
# Lithos Loom — GitHub Watcher State

Daemon-owned coordination doc. Do not edit by hand — the watcher
overwrites the `github_last_poll` map on every successful poll.
```

The doc is created on first watcher poll if missing; updated via `lithos_write(id=..., expected_version=..., metadata={...})` after each successful per-repo cursor advance.

### GitHub API surface

For Slice 7.1, the operations needed:

- `GET /repos/{owner}/{repo}/issues?state=open&sort=updated&direction=asc&since={cursor}` — incremental issue list. Filter `pull_request` field out for D53.
- `GET /repos/{owner}/{repo}/issues/{n}` — fetch a single issue (for the close-state reconciliation path).
- `PATCH /repos/{owner}/{repo}/issues/{n}` — update body (linkage marker write); close (Slice 7.2).
- `GET /rate_limit` — diagnostic for the 70th-percentile retry strategy.

Total: 4 endpoints, all REST. Within `httpx` capability; no need for the heavier `PyGithub` library.

### Failure modes + retries

- **Rate limit hit (403 + `X-RateLimit-Remaining: 0`)**: back off until `X-RateLimit-Reset`, log the wait at INFO, resume.
- **Repo not found (404)**: log `[Friction]`, drop that repo from the cursor map, continue with the others.
- **Auth failure (401)**: log a startup error, exit the watcher subprocess (the operator needs to fix auth before anything works). The supervisor can choose to respawn or not per its existing policy.
- **Linkage-marker race** (operator and Loom both editing body simultaneously): GitHub's `PATCH /issues/{n}` is full-body replacement, no optimistic locking. On marker write, fetch the latest body, append/replace the marker, write back. A losing race (operator's edit lost) is possible but rare; document the limitation. A retry on `409` is the right behaviour but the GitHub API doesn't expose this cleanly.

## Open Questions (Deferred)

1. **Assignee mapping.** GH issues have an `assignees` field. Lithos doesn't have a first-class assignee model (everything is `agent`). Mapping GH usernames to Lithos agents requires an identity table operators don't have today. Defer; document `metadata.github_assignees` as a snapshot for queries.

2. **Comments / discussion sync.** GH issue comments are rich (threaded, edited, reacted-to). Mirror to Lithos findings? Probably not — findings are agent-driven breadcrumbs, not a comment thread. Operators can click through to GitHub for the discussion. Defer.

3. **Milestone / project board sync.** Out of scope. Most operators don't use GH milestones; those who do can manage them separately. Defer.

4. **Multiple repos per project.** Current design is 1:1 — one Lithos project doc → one `github_repo`. Some projects span multiple repos (monorepo with satellite repos). Defer; expand to `github_repos = ["owner/repo1", "owner/repo2"]` list when soak surfaces the need.

5. **Webhooks instead of polling.** Adds low-latency feedback for high-traffic repos. Requires public endpoint or smee.io relay. Defer; soak first to confirm 60s polling is sufficient.

6. **Issue templates.** GH issue templates produce structured bodies (e.g. "## Description / ## Steps to reproduce"). Parsing these into Lithos task structure (separate description / repro / etc fields) is appealing but Lithos task model doesn't have those fields. Defer.

## Verification

### After Slice 7.1 (inbound MVP)

1. **Setup.** Enable a project: `lithos-loom project set-github-repo lithos-loom agent-lore/lithos-loom`, `lithos-loom project enable-github lithos-loom`. Set `[github_watcher] enabled = true` in slice-7-test config. Restart daemon.

2. **Bootstrap reconcile.** On startup, watcher walks every open issue in `agent-lore/lithos-loom`. For each: confirm a Lithos task created OR (if marker already present) confirm task lookup succeeds. Within a few minutes (depending on issue count), all open issues become Lithos tasks. Check Obsidian's `_lithos/tasks.md` — they should appear with `#project/lithos-loom` tag, GH labels as additional tags, and link via `[GH #N]` (or similar — title prefix TBD by renderer).

3. **Linkage marker.** Open any newly-reconciled issue on github.com. View body in edit mode (or raw via API). Confirm `<!-- lithos:<task_id> -->` line is present at end.

4. **Re-bootstrap idempotent.** Stop daemon, restart. Confirm bootstrap doesn't create duplicate tasks (cursor + marker prevent re-creation). Check daemon log for `github-watcher: skipped already-linked issue #N`.

5. **New issue.** Manually open a new issue on github.com. Within ~60s, Lithos task appears + marker written + Obsidian projection picks it up.

6. **GH close completed.** Close one of the imported issues with reason "Completed" on github.com. Within ~60s, the Lithos task transitions to `completed`. Slice 6's task-archive picks up the event and appends to `<slug>-done.md`. Global projection evicts the line.

7. **GH close not-planned.** Close another with reason "Not planned". Confirm Lithos task transitions to `cancelled`. Archive line uses `[-]` marker.

8. **Unmapped repo `[Friction]`.** Create an issue in a repo that no project-context doc references. Confirm: NO Lithos task created, `[Friction]` log line names the repo. (Reproduce by removing `metadata.github_repo` from a project-context doc and waiting for next poll.)

9. **Exclude filters.** Add a label `automated` to one issue + configure `metadata.github_issue_exclude_labels = ["automated"]` on the project doc. Confirm next reconcile skips that issue (no Lithos task created; if already created, NOT closed automatically — exclude is only at import time).

10. **Rate limit grace.** Optional, harder to reproduce: cap the watcher's API budget locally (e.g., point at a stub that returns 403). Confirm log line + retry-after-reset behaviour.

### After Slice 7.2 (bidirectional + ongoing sync)

11. **Lithos complete → GH close.** Tick a projected GH-imported task in Obsidian. Within ~250ms (obsidian-status-transition fires immediately) + next watcher poll (~60s), the GH issue transitions to closed-as-completed.

12. **Lithos cancel → GH close.** Same flow with `[ ]→[-]`. GH issue closes as not-planned.

13. **Title drift.** Rename the GH issue title. Confirm Lithos task title updates on next poll. Reverse: rename Lithos task title via MCP. Confirm GH issue title updates on next reconcile.

14. **Body drift.** Edit GH issue body (preserve the marker). Confirm Lithos task description updates on next poll.

15. **Label drift.** Add and remove labels on GH. Confirm Lithos task tags reflect the new label set; operator-added Lithos tags persist.

16. **Reopen.** Reopen a closed Lithos-completed task on GH. Confirm `[ReopenRequested]` finding posted to the Lithos task (Lithos task itself stays `completed` until upstream reopen lands).

## Risks

- **GitHub rate limit on large operator graphs.** 5000 reqs/hr ÷ 60s polls = 83 polls/min. With 50 watched repos that's manageable, but operators with 100+ repos hit the ceiling. Mitigations: configurable poll interval (raise to 300s for big graphs), GraphQL batching (one query covers many repos), webhook opt-in for highest-traffic repos (deferred follow-up).

- **GH body-update race vs operator edit.** GitHub's `PATCH /issues/{n}` is full-body replacement with no optimistic locking. If operator and Loom both update the body at the same instant, the loser's edit is dropped. Mitigation: Loom only touches the body to maintain the marker (one line); read-modify-write is fast enough that the race window is small. Document the limitation in the operator README.

- **Operator deletes the linkage marker by accident.** Next poll detects an unlinked issue with matching `metadata.github_issue_url` and re-writes the marker rather than creating a duplicate. Defensive against operator edits.

- **Coordination doc grows unbounded.** `github_last_poll` map carries one entry per watched repo. With 100+ repos, the YAML frontmatter on the coord doc gets long but stays well under typical Lithos doc size limits. Revisit only if soak surfaces a doc-size issue.

- **Watcher subprocess single-host bottleneck.** Per D50, one host runs the watcher. If that host is down, no GH reconciliation happens (issues queue up but get caught at next watcher start via cursor). Acceptable for v1; multi-host coordination is deferred.

- **PRD scope ambition.** Slice 7 is materially larger than Slices 3-6 — it adds a new source, a new subscription pair, a new subprocess child, a new CLI surface, a new config block, AND a new coordination doc shape. The 7.1 / 7.2 split helps but the MVP is still ~1 sprint of focused work. Worth confirming the operator wants this before Track 2 vs deferring further.
