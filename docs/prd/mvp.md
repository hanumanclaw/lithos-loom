---
title: Lithos Loom — MVP (Proof of Concept)
milestone: M0
status: draft
target_version: 0.1.0
references:
  - docs/SPECIFICATION.md (implemented surface — architecture, plugin contract, event bus)
  - docs/prd/archive/integration.md (Obsidian bridge PRD — shipped)
  - docs/prd/archive/PLAN.md (original build plan — shipped)
  - /home/dns/projects/lithos/code/lithos/docs/SPECIFICATION.md (Lithos task + knowledge surface)
labels: [needs-triage, mvp, lithos-loom, orchestrator, plugin-contract]
---

# Lithos Loom — MVP (Proof of Concept)

> **Status (2026-05-29):** The Obsidian bridge has shipped, and the orchestration spine described in this PRD (supervisor + bus + sources + subscribers + plugin-runner + `result.json` contract) is in place. What remains unbuilt is the plugin **bodies** — `prd-decompose`, `story-implement`, `story-review-human`. Scaffolding exists under `src/lithos_loom/plugins/`; this PRD captures the contract those bodies must satisfy. See [docs/SPECIFICATION.md](../SPECIFICATION.md) §5 (plugin contract) and §2 (architecture) for the implemented surface.

## Problem Statement

I write PRDs by hand using Pocock-style skills (problem statement, solution, numbered user stories) and would like LLM-driven coding agents to implement them story-by-story under my supervision. Today my options are:

- **Ralph++** — runs the full pipeline (PRD review → story list → implement → post-review) inside one monolithic process. Works for small PRDs but becomes unreviewable past ~10 stories. No knowledge of Lithos. Each run starts from scratch.
- **Hand-driving Claude** — I open a worktree, paste the PRD, run Claude, review the diff, repeat. Fine for one story; doesn't scale.

Lithos already has the right primitives — tasks with claims, knowledge docs with provenance, agent registry, findings — but no execution layer. There is no orchestrator that dispatches work, no plugin contract for coding tools, and no way for a story task to spawn its own implementation worker.

The result: my PRDs sit in Lithos as text. The KB grows from conversations, not from work. Every implementation starts from scratch. Reviewing a 28-story PRD as a single PR is impossible.

## Solution

A small Python daemon (`lithos-loom`) that polls Lithos for open tasks, matches them by tag against TOML-configured routes, claims them collision-safely, and dispatches three bundled subprocess plugins:

- **`prd-decompose`** — reads a PRD knowledge doc, runs Claude with a Pocock `to-issues`-shaped structured-output prompt, writes one story doc per story to Lithos (`note_type: task_record`, `derived_from_ids: [prd_id]`), creates the per-PRD integration branch (`loom/<prd-slug>`), and creates one Lithos task per story chained via `metadata.depends_on` for strict-sequential execution.
- **`story-implement`** — claims a story task, creates a per-task git worktree off the integration branch, runs Claude with the PRD body + story brief + the project's existing `AGENTS.md` / `CLAUDE.md`, detects new commits, opens a GitHub PR against the integration branch via `gh pr create`, and re-tags the task for human review.
- **`story-review-human`** — polls `gh pr view --json state,mergedAt`. On `MERGED` completes the task and unblocks the next dependent story; on `CLOSED` without merge fails the task with a `[ReviewRejected]` finding.

All three plugins talk to the daemon through the same contract: a subprocess invoked with `--task-json --work-dir --result-file`, writing an atomic `result.json` whose schema is lifted from Ralph++'s unattended-mode contract. The daemon owns claim/release, dependency resolution, artifact upload back to Lithos, and task metadata patching. Plugins own their own work — worktrees, agent invocation, GitHub interaction.

Single workstation. Project-affinity in TOML config. No A2A, no SSE, no brain, no agent-driven review, no crash recovery, no docker sandbox. The MVP exists to prove the loop works end-to-end against a real PRD: `lithos-lens/docs/prd/milestone-1-operator-view.md`.

## User Stories

Vertical-slice, ordered by build sequence. Each is independently grabbable.

1. As a developer, I want to bootstrap the repo from `~/projects/templates/python` (rename `influx` → `lithos-loom` / `lithos_loom`, set description, register the console entry point, **drop the `docker/` directory** because Loom runs on the host, retain `python-dotenv` for per-host config), so that subsequent stories inherit the standard uv + Python 3.12 + `src/` layout + ruff + pyright + pytest + Makefile + GitHub Actions CI scaffolding without rebuilding it by hand.
2. As the daemon, I want a thin async client over Lithos's HTTP MCP surface covering tasks, knowledge writes/reads, findings, and agent registration, so that every other story interacts with Lithos through one tested module.
3. As a plugin author, I want a single documented JSON schema and an atomic-write helper for `result.json`, so that the daemon can rely on never observing a partial result file.
4. As an operator, I want a TOML config file with sections for orchestrator settings, project registry, and routes, so that machine-specific repo paths and Claude config dirs stay out of Lithos and out of project repos.
5. As an operator, I want the daemon to register itself as a Lithos agent on startup and release any stale claims it previously held, so that orphaned claims from a crashed previous run do not block work.
6. As the daemon, I want to poll `lithos_task_list(status='open')`, match tasks against tag-based routes, and claim matched tasks collision-safely, so that the orchestration loop is provably end-to-end before any real plugin lands.
7. As the daemon, I want claim renewal before TTL expiry for long-running plugins, so that legitimate work is not killed by stale-claim cleanup.
8. As the operator, I want to declare task dependencies via `metadata.depends_on` and `metadata.parallelizable`, so that strictly-sequential PRDs (the default) execute in order and parallelizable siblings are unblocked together.
9. As the daemon, I want to detect cyclic `depends_on` graphs and fail offending tasks with `code: dep_cycle`, so that bad decompositions cannot deadlock the queue.
10. As the daemon, I want to fail a task with a `[BlockerFailed]` finding when any blocker enters `cancelled` or `failed`, so that downstream stories do not silently sit in the queue forever.
11. As a plugin, I want a vetted helper for creating per-task git worktrees with predictable unique branch names off an arbitrary base branch, so that I do not reimplement worktree mechanics from scratch and worktree paths never collide.
12. As a plugin, I want a helper that launches `claude` or `codex` as a subprocess with stream-json output capture and a hard timeout, so that all coding-agent invocations have the same logging and timeout discipline.
13. As a plugin, I want a helper that returns the list of new commits a worktree gained since a base SHA, so that I can decide whether the agent did real work and surface SHAs in `result.json`.
14. As the operator, I want a `story-implement` plugin that takes a story task, creates a worktree off the integration branch, runs Claude with the PRD + story brief + project AGENTS.md, and opens a PR, so that I can verify end-to-end implementation works before the decompose plugin is built.
15. As the operator, I want `story-implement` to mark a task failed with `code: no_progress` when the agent produces zero new commits, so that empty runs do not silently succeed.
16. As the operator, I want `story-implement` to push the worktree branch to `origin` and call `gh pr create --base <integration_branch>`, so that the PR is immediately visible on GitHub for me to review.
17. As the operator, I want `story-implement` to retag the task from `trigger:story-implement` to `trigger:story-review-human` and write the PR URL into `task.metadata.pr_url`, so that the next plugin in the chain picks up the same task without manual intervention.
18. As the operator, I want a `story-review-human` plugin that polls `gh pr view --json state,mergedAt` and completes the task on merge, so that approving a PR in GitHub auto-unblocks the next dependent story.
19. As the operator, I want `story-review-human` to fail a task with `[ReviewRejected]` when the PR is closed without merging, so that I have a clear signal to triage and don't have to re-poll forever.
20. As the operator, I want `story-review-human` to be idempotent across daemon restarts (re-polls the same PR URL), so that a daemon crash mid-review does not lose progress.
21. As the operator, I want a `prd-decompose` plugin that reads a PRD knowledge doc and runs Claude once with a structured-output prompt adapted from Pocock's `to-issues` skill, so that I can hand any Pocock-shaped PRD to Loom and get a runnable pipeline.
22. As the operator, I want `prd-decompose` to write one Lithos knowledge doc per story (`note_type: task_record`, `derived_from_ids: [prd_id]`) with title, brief, acceptance criteria, and file-path hints, so that downstream `story-implement` runs have rich, self-contained context.
23. As the operator, I want `prd-decompose` to create the per-PRD integration branch `loom/<prd-slug>` off `main` in the project repo and record it in the parent task's metadata, so that all child story implementations target the same integration point.
24. As the operator, I want `prd-decompose` to create one Lithos task per story tagged `trigger:story-implement`, with `metadata.depends_on` chained per the LLM-emitted dependency list, default strict-sequential, so that running the daemon executes them in the right order.
25. As the operator, I want `prd-decompose` to retry once on schema-invalid LLM output and fail the task with the raw output preserved as a finding on second failure, so that I can manually recover without re-running the expensive decompose call.
26. As the operator, I want `prd-decompose` against the lithos-lens M1 PRD to produce between 8 and 28 coherent story tasks where the first 3 are runnable by `story-implement` without further human editing, so that the MVP has a real, non-toy test case.
27. As the operator, I want the full pipeline (decompose → implement → human-merge → unblock-next) to run unattended for at least the first dependent story pair of the lithos-lens PRD, so that I have proof the orchestrator works.
28. As the operator, I want every meaningful state transition (task claimed, agent finished, PR opened, review pending, review merged, review rejected, blocker failed) to post a tagged Lithos finding with a stable prefix (e.g. `[Implemented]`, `[ReviewMerged]`), so that I can scan task history in lithos-lens and reconstruct what happened without reading subprocess logs.
29. As the operator, I want the daemon to handle SIGTERM gracefully — finish in-flight plugins, release any newly-orphaned claims, then exit — so that I can restart the daemon during a long PRD run without losing work.
30. As the operator, I want artifact upload from `result.json` to be additive: each artifact uploaded to Lithos has its `derived_from_ids` populated from the route's outputs config supporting interpolation of prior artifact doc IDs, so that provenance chains (story doc → ADR → run-log) are linked correctly.
31. As the operator, I want per-task staging directories under `{loom.work_dir}/{task.id}/` to be auto-cleaned on successful completion and retained on failure (configurable via `orchestrator.retain_failed_workdirs`, default true), so that successful runs don't accumulate disk clutter while failed runs remain debuggable.
32. As the operator, I want every Loom log line emitted in structured JSON with `task_id`, `route`, `agent_id`, `plugin`, `level`, and `event` fields, so that I can pipe logs to any aggregator and grep meaningfully across concurrent runs.
33. As a plugin author, I want a versioned `result.schema.json` shipped at `docs/result-schema.json` and used by the plugin runner for validation, so that schema changes are tracked in git and external plugin authors have a checkable contract.
34. As the operator, I want each route to declare a `max_runtime_seconds` (default 3600) and the daemon to send SIGTERM to a plugin that exceeds it, so that a runaway agent or wedged subprocess cannot consume resources indefinitely.
35. As the operator, I want `lithos-loom doctor` (run on first boot) to verify the connected Lithos server supports the `task.metadata` field by writing a probe task with metadata and asserting it round-trips, so that incompatibility with an old Lithos surfaces immediately rather than failing mid-PRD.

## Implementation Decisions

**Modules to build (deep modules where possible — testable in isolation, simple interfaces, rarely-changing):**

- **Repo bootstrapping** — the scaffolding (pyproject.toml, Makefile, ruff/pyright config, src-layout, tests/conftest.py, Dockerfile, CI workflow) comes from `~/projects/templates/python`. Rename and minimal config edits only; do not re-derive these conventions.
- **Lithos client** — async wrapper over the Lithos HTTP MCP surface. Surfaces `{status: "error", code, message}` envelopes as typed exceptions with the `code` preserved. This is a deep module: simple interface (one function per Lithos tool), encapsulates retries, error normalization, and JSON shape contracts. Will not change as plugins are added.
- **Plugin runner** — invokes a plugin subprocess, validates the resulting `result.json` against the schema, returns a typed `PluginResult`. Owns the subprocess lifecycle (start, send SIGTERM on daemon shutdown, wait, parse). Deep module: invariant interface across all current and future plugins.
- **Result-file IO** — atomic write helper (temp + fsync + rename), schema validator, parser. Deep module: trivial interface, encapsulates the atomicity contract that everything else depends on.
- **Route matcher + dependency resolver** — given a list of open tasks and a route table, returns the next runnable task respecting `depends_on`, `parallelizable`, claim status, and tag intersection. Deep module: pure function over Lithos state, easy to test exhaustively.
- **TOML config** — load + validate + project resolution. Deep module: pure transformation from TOML to typed dataclasses.
- **Worktree helper** — create/remove worktrees with unique branch names off an arbitrary base. Deep module lifted from Ralph++.
- **Agent subprocess runner** — launch `claude` / `codex` with stream-json capture and hard timeout. Deep module lifted from Ralph++.
- **Git helper** — base SHA, commits-since, dirty check. Deep module lifted from Ralph++.

**Plugin layout** — three plugins as separate Python entry points under `lithos_loom.plugins.<name>`. Each plugin is a thin script that uses the runner helpers and writes `result.json`. Plugins are not deep modules — they are orchestration glue.

**Plugin contract:**

- Subprocess invocation: `<plugin> --task-json <path> --work-dir <path> --result-file <path>`
- `task.json` is the full `lithos_task_status(task_id)` payload plus the resolved project entry from local Loom config
- Plugin owns the work-dir tree; daemon only reads `result.json`
- Result schema: `{schema_version, task_id, status, exit_code, started_at, finished_at, worktree, artifacts, commits, spawned_tasks, metadata_updates, error}`
- `metadata_updates` lets a plugin patch the task it ran (e.g. `prd-decompose` writes `integration_branch`; `story-implement` writes `pr_url`); daemon applies via `lithos_task_update`
- `spawned_tasks` lets `prd-decompose` declare children for DAG tracking
- Exit code mapping: `0` succeeded · `1` generic failure (consult `error.retriable`) · `20` bad input/config (don't retry) · `30` interrupted (release + leave open)

**Lithos integration:**

- Loom registers itself as agent `lithos-orchestrator-<host>` (type `lithos-loom`) on startup
- Story docs use `note_type: task_record` and `derived_from_ids: [prd_id]` so `lithos_related` surfaces the lineage
- Findings use stable prefixes — `[Implemented]`, `[ReviewPending]`, `[ReviewMerged]`, `[ReviewRejected]`, `[NoProgress]`, `[BlockerFailed]` — for cheap filterability in lithos-lens
- Run logs (stream-json captured during `story-implement`) are uploaded as `note_type: observation`, `access_scope: task`, `ttl_hours: 168` so they don't pollute the KB long-term

**Task metadata schema (orchestration-only fields, all under `task.metadata`):**

- `project` — join key into Loom's project registry (machine-agnostic)
- `prd_doc_id` — the parent PRD's Lithos doc ID
- `story_doc_id` — the story brief's Lithos doc ID
- `integration_branch` — the per-PRD integration branch name
- `pr_url` — set by `story-implement`, read by `story-review-human`
- `depends_on` — list of task IDs that must be `completed` before this task is runnable
- `parallelizable` — boolean, allows concurrent execution among siblings
- `parent_task_id` — set by `prd-decompose` on each story task, points back at the decompose task

**Decisions deliberately encoded for prd-decompose to inherit:**

- One Claude turn per PRD (not per story) — cheaper, gives a coherent story list
- Structured output schema: `{stories: [{title, brief, acceptance_criteria, deps, files_hint, parallelizable}]}` where `deps` is a 1-based index list into the same `stories` array
- Default `parallelizable: false` — the plugin must be explicitly asked by the LLM to mark stories independent
- Retry once on schema mismatch with the validation error fed back; fail with raw output preserved on second failure

**Concurrency posture (MVP):**

- Multiple tasks may run concurrently if they belong to different projects or are explicitly `parallelizable`
- Within a project, default is one in-flight task at a time (project-affinity)
- Configurable `orchestrator.max_concurrency` (default 4)

**Lithos version pre-requisite:**

- Loom requires a Lithos server with the `task.metadata` field on tasks (added in `agent-lore/lithos#215`). The daemon's `doctor` subcommand (US-35) probes this on first boot and refuses to run against an incompatible Lithos. Any earlier Lithos must be upgraded before Loom can run.

## Testing Decisions

**Test philosophy:** test external behaviour, not implementation details. A test of the route matcher should not assert which dictionary key holds the result; it should assert "given this state, this task is the next runnable one". Tests against the Lithos client should verify "the right MCP tool was called with the right arguments and the response was normalised", not the internal HTTP machinery.

**Modules with mandatory unit test coverage:**

- Lithos client — happy path + each documented error code at least once. Mock HTTP layer.
- Plugin runner — happy result, schema-invalid result rejected, partial result observation impossible (atomic write contract), subprocess timeout, SIGTERM propagation.
- Result-file IO — atomic write under simulated mid-write read; schema validation rejects unknown `status`, missing required keys, type mismatches.
- Route matcher + dependency resolver — exhaustive table-driven tests covering: blocked task skipped, blocked task becomes runnable on blocker completion, parallelizable siblings unblocked together, cyclic deps detected, claim conflict skipped silently, tag intersection matching.
- TOML config — happy parse, missing `[orchestrator]`, missing `[projects.<x>]` resolution, env-var override of base URL, invalid types rejected.
- Worktree helper — creation, removal, removal of dirty worktree refused without `force=True`, unique branch naming under repeated calls.
- Agent subprocess runner — mock `claude` script for happy path / timeout / crash exit code 1 / SIGTERM-during-run.
- Git helper — fixture repo for zero / one / many new commits since base SHA, dirty detection.

**Modules with integration test coverage:**

- Daemon poll/claim/release loop — stub plugin that echoes a fixed `result.json`, run end-to-end through claim → execute → release; assert findings posted, task metadata updated, claim released.
- `story-implement` happy path — hand-written story doc + task in a fixture Lithos, fixture project repo, mocked `gh pr create`; assert worktree created, commits detected, PR URL written to metadata, task retagged.
- `story-review-human` PR poller — mocked `gh pr view` returning each state in sequence; assert `[ReviewMerged]` posted on MERGED and `[ReviewRejected]` on CLOSED without merge.
- `prd-decompose` against the lithos-lens M1 PRD — full Claude call (live, not mocked), assert produced story count is in [8, 28], every story has ≥ 80-word brief and ≥ 2 acceptance criteria, integration branch created, tasks chained via `depends_on`.

**Prior art for tests:**

- Ralph++'s pytest layout under `ralph_pp/tests/` — fixture repo creation, claude-script mocking, atomic-write tests. Lift conventions where they translate.
- Lithos's `tests/` for the MCP HTTP client mocking pattern.

**Deliberate non-coverage:** end-to-end live runs against the real lithos-lens repo and a real Claude account are NOT part of the unit/integration suite (cost + flakiness). The "first dependent story pair" run from US-27 is a manual acceptance test, not an automated one.

## Out of Scope

The following are explicitly deferred to the full PRD (`docs/prd/full.md`) and must not be added to the MVP:

- PRD generation from a feature description (`prd-generate`)
- PRD review by an agent or human (`prd-review-agent`, `prd-review-human`)
- Story review by an agent (`story-review-agent`) and the per-project `review_policy` selector
- Story-fix loop after agent review fails (`story-fix`)
- Brain plugin (`decide-next`) for non-trivial state transitions
- A2A endpoint for triggering tasks from Agent Zero / Hanuman
- SSE event subscription (polling only in MVP)
- Multi-host topology, PRD-affinity, GitHub webhooks
- Crash recovery beyond fail-and-restart (no `[Recovery]` findings, no checkpoint resume)
- Filesystem progress checkpoints during a run
- Introspection / self-improvement loop (`loom-improve`)
- `merge-stories` plugin (the operator merges the integration branch by hand the first time)
- `bash-runner` generic plugin and config-defined ad-hoc plugins
- `lithos-coding-mcp` integration and Lithos-aware Claude config dir (`~/.claude-lithos/`) — Claude in MVP does not see Lithos
- Docker sandbox option for `story-implement`
- Cost / token budget tracking and reporting
- Custom prompt overrides per project
- Hot-reload of TOML config (operator restarts the daemon)
- Configurable per-project review_policy (always-human only in MVP)

## Further Notes

- **The salvaging from Ralph++** is done by lifting source files into `lithos_loom/runner/` and adapting them. The Ralph++ project itself is not retained as a runtime dependency. This is by design: Loom replaces Ralph++ as the user's coding orchestration approach.
- **The result.json schema** is intentionally compatible with the Ralph++ unattended-mode contract documented in the Lithos KB note `ralph-plus-plus-unattended-mode-mvp.md`. This means that, transitionally, if Ralph++ ships `--unattended` before Loom is fully built, a Loom route could invoke Ralph++ as a one-shot plugin without contract changes. This is a useful fallback but is not part of the MVP build path.
- **The Pocock-style PRD shape** is the input contract for `prd-decompose`. Any PRD that conforms to the shape used in `lithos-lens/docs/prd/milestone-1-operator-view.md` (numbered "## User Stories" with `As a <role>, I want <X>, so that <Y>` lines) decomposes reliably. Other PRD shapes may decompose poorly until `prd-decompose` learns more shapes (deferred).
- **Why `gh pr view` polling instead of webhooks** — webhooks require public ingress, port forwarding, and a webhook secret. Polling adds latency (default 60s) but is operationally trivial. Webhook receiver is full-PRD scope.
- **Why no `lithos-coding-mcp` in MVP** — pulling it in would couple the MVP to a separate uvx-installable package and require Claude config dir surgery. The MVP works without Claude knowing Lithos exists; story briefs are passed in the prompt. Adding `lithos-coding-mcp` later is a strict improvement.
- **The 4-day target** assumes focused work and that the lithos-lens M1 PRD's own decomposability is reasonable. If the first decompose run produces obviously-bad story briefs, the MVP slips by ~1 day for prompt iteration on `prd_decompose/prompt.md`.
- **Manual escape hatches** preserved throughout: any failed task can be hand-edited in Lithos, re-tagged, and re-claimed. This MVP is built to fail safely, not to be unfailable.
- **Why Loom runs on the host, not in docker.** Lithos and Influx run as docker services because they are services — long-lived, stable protocols, no host filesystem coupling. Loom is different: it creates git worktrees the operator can `cd` into, invokes `claude` / `codex` / `gh` CLIs that authenticate against per-user dotfiles in `~/`, and spawns plugin subprocesses that need the same access. Containerizing Loom would require bind-mounting `~/.claude/`, `~/.codex/`, `~/.config/gh/`, every project repo's parent dir, and `/var/run/docker.sock` (so it could spawn the A10 sandbox containers) — at which point the container is a thin wrapper around "run on the host" with extra restart machinery. The MVP runs Loom manually via `uv run lithos-loom run` in a terminal or tmux. A systemd `--user` unit is a deferred polish item, not part of the MVP.
