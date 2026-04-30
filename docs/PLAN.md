# Lithos Loom — Build Plan

Status: draft
Date: 2026-04-30
Owner: Dave Snowdon
Related repos: [lithos](https://github.com/agent-lore/lithos), [lithos-lens](../lithos-lens), [ralph-plus-plus](../../agents/ralph-dev/ralph-plus-plus) (source of salvageable bits)

> See also:
> - [docs/prd/mvp.md](prd/mvp.md) — PRD for the proof-of-concept
> - [docs/prd/full.md](prd/full.md) — PRD for the full automated workflow system
> - Project context (in Lithos KB): `projects/lithos-loom/lithos-loom-project-context.md`
> - Architecture design (in Lithos KB): `projects/lithos-loom/lithos-loom-architecture-design.md`
> - Conveyor analysis (in Lithos KB): `projects/lithos-loom/conveyer-notes.md`

## Goal

Turn Lithos tasks into a fine-grained, fault-tolerant pipeline for building software with LLM-driven coding agents. Replace Ralph++ as the orchestration approach: instead of one monolithic Ralph++ run per PRD (which becomes unreviewable past ~10 stories), break work into single-purpose Lithos tasks (PRD generation, PRD review, decompose, per-story implement, per-story review, integration merge) that Loom dispatches to small bundled plugins.

This is the orchestration layer that connects Lithos (knowledge + task store) to the work, with deliberately conveyor-like properties: stateless plugins, file-based hot state, durable Lithos findings as breadcrumbs, GitHub PRs as the human-review surface.

## Locked design decisions

| Area | Decision |
|------|----------|
| Granularity | Replace Ralph++; salvage worktree creation, agent subprocess launching, commit detection, stream-json log capture |
| Story task shape | One Claude/Codex invocation per story task — no inner review loop. Review is a separate task. |
| Worktrees | Per task (W2). Worktree may be reused for the immediately-following review task. |
| Brain | Tag-routed by default (BR3); LLM-backed `decide-next` plugin invocable for non-trivial state |
| Human-in-the-loop | PRD review mandatory; story review configurable per project (always-human / always-agent / spot-check / brain-decide) |
| Concurrency | Across projects and within a project where dependencies permit |
| Task dependencies | `task.metadata.depends_on: [task_id]` and `task.metadata.parallelizable: bool`. Default strict-sequential. Lithos `edges.db` is doc-only and cannot model task→task edges without a Lithos spec change. |
| Crash recovery | Filesystem hot state in `{work_dir}/{task.id}/progress.json`; entry/exit recorded as Lithos findings. On restart, post `[Recovery]` finding with last checkpoint and re-run from there (conveyor-C). |
| Plugin contract | Subprocess + atomic `result.json` (P1) plus shipped Python helper library (P3). `bash-runner` plugin lets the TOML config define ad-hoc plugins inline. uv-managed Python. |
| Topology | One Loom instance per workstation. Project-affinity in MVP (T3). PRD-affinity later. |
| Story storage | Each story is its own Lithos doc (S2): `note_type: task_record`, `derived_from_ids: [prd_id]`. |
| GitHub | `watch-pr` polling in MVP; webhook receiver later. PRs target a per-PRD integration branch (`loom/<prd-slug>`). |
| Sandbox | Direct Claude invocation in MVP. Docker sandbox is a deferred A10 enhancement. |
| Agent identity | Loom registers as `lithos-orchestrator-<host>`; coding sub-agents are `claude-code` / `codex` per the existing agent identity model. |

## Bundled plugins

| Plugin | MVP? | Role |
|--------|------|------|
| `prd-decompose` | ✓ | Read a PRD doc, run a Claude turn with a Pocock-`to-issues`-style prompt, emit one Lithos story doc per story plus one task per story (with `depends_on` for strict-sequential), create the per-PRD integration branch |
| `story-implement` | ✓ | Per-task worktree off the integration branch, Claude run with PRD+story context, commit detection, `gh pr create` against the integration branch |
| `story-review-human` | ✓ | Poll `gh pr view --json state,mergedAt`; on `MERGED` complete the task and unblock dependents; on `CLOSED` without merge fail the task |
| `prd-generate` | ✗ | Pocock-style PRD generation from feature description |
| `prd-review-agent` | ✗ | Codex/Claude reviews a PRD, posts findings |
| `prd-review-human` | ✗ | Opens a Lithos review template doc and waits for a human-tag/finding signal |
| `story-review-agent` | ✗ | Codex reviews the diff for a story PR; pass → ready for human merge; fail → spawn `story-fix` |
| `story-fix` | ✗ | Apply review feedback to a story branch |
| `merge-stories` | ✗ | Run `make ci` on the integration branch, open final PR to `main` with synthesised changelog |
| `decide-next` | ✗ | LLM-backed brain that reads PRD + child task state and decides next steps (escalate, retry, batch-fix) |
| `watch-pr` | ✗ | Generic GitHub PR state poller (or webhook receiver), used by review plugins |
| `bash-runner` | ✗ | Generic plugin that executes a config-defined shell command, for ad-hoc routes |
| `loom-improve` | ✗ | Aggregates `[Friction]` findings into improvement tasks (conveyor-style introspection loop) |

## Plugin contract

All plugins are invoked as subprocesses with three flags:

```
plugin --task-json <path> --work-dir <path> --result-file <path>
```

- `--task-json` — JSON dump of `lithos_task_status(task_id)` plus the resolved project entry from local Loom config.
- `--work-dir` — Per-task staging directory (`{loom.work_dir}/{task.id}/`). Plugins own the tree; Loom only reads the result file.
- `--result-file` — Where the plugin writes the JSON outcome atomically (write-temp + fsync + rename).

Result schema mirrors the Ralph++ unattended-mode contract:

```json
{
  "schema_version": 1,
  "task_id": "<lithos task id>",
  "status": "succeeded|failed|interrupted",
  "exit_code": 0,
  "started_at": "...", "finished_at": "...",
  "worktree": "/abs/path/or/null",
  "artifacts": { "<key>": "/abs/path or relative-to-worktree" },
  "commits": ["sha1", "sha2"],
  "spawned_tasks": ["task_id1", "task_id2"],
  "metadata_updates": { "<key>": "<value>" },
  "error": null
}
```

`metadata_updates` lets a plugin patch the task it ran (e.g. `prd-decompose` writes `integration_branch`; `story-implement` writes `pr_url`). Loom applies these via `lithos_task_update`. `spawned_tasks` lets `prd-decompose` declare the children it created so Loom can track the DAG.

## Topology

```
┌────────────────── workstation (samsara or mac mini) ──────────────────┐
│                                                                       │
│  ┌─────────────────┐                                                  │
│  │   lithos-loom   │ ── poll lithos_task_list(status='open') ─┐       │
│  │     daemon      │                                          │       │
│  │                 │                                          ▼       │
│  │  - claim        │                          ┌───────────────────┐   │
│  │  - run plugin   │ ── subprocess + JSON ──► │ Lithos (HTTP MCP) │   │
│  │  - upload arts  │                          └───────────────────┘   │
│  │  - update task  │                                          ▲       │
│  └────────┬────────┘                                          │       │
│           │ subprocess                                        │       │
│  ┌────────▼────────┐                                          │       │
│  │  plugin (uv-run │ ── claude / codex subprocess ──┐         │       │
│  │  Python entry)  │                                ▼         │       │
│  │                 │                  ┌──────────────────┐    │       │
│  │  uses runner/   │                  │ git worktree     │    │       │
│  │  helpers        │                  │ + claude/codex   │    │       │
│  └─────────────────┘                  │ + gh pr ops      │    │       │
│                                       └──────────────────┘    │       │
└───────────────────────────────────────────────────────────────│───────┘
                                                                │
                  github.com  ◄──── PRs target loom/<prd-slug> ─┘
                                    integration branches
```

## Knowledge model in Lithos

| Lithos object | Used as |
|---------------|---------|
| Knowledge doc, `note_type: concept`, tag `prd` | The PRD (canonical Markdown) |
| Knowledge doc, `note_type: task_record`, `derived_from_ids: [prd_id]` | A single story brief, written by `prd-decompose` |
| Knowledge doc, `note_type: concept`, tag `adr` | ADR written by coding agents during implementation (via `lithos-coding-mcp` once available) |
| Knowledge doc, `note_type: observation`, tag `run-log`, `access_scope: task`, `ttl_hours: 168` | Stream-json run logs, expire after 7 days unless promoted |
| Lithos task, tag `trigger:<route>` | Unit of work for Loom |
| `task.metadata.project` | Join key for resolving project repo / Claude config dir |
| `task.metadata.depends_on` | List of task IDs that must be `completed` before this task runs |
| `task.metadata.parallelizable` | If `true`, Loom may run this task concurrently with other parallelizable siblings |
| `task.metadata.prd_doc_id` | The parent PRD's doc ID |
| `task.metadata.story_doc_id` | The story brief's doc ID |
| `task.metadata.integration_branch` | The per-PRD integration branch (`loom/<prd-slug>`) |
| `task.metadata.pr_url` | The story's GitHub PR URL |
| Lithos finding, summary prefixed `[Plan]` / `[Drift]` / `[Recovery]` / `[Friction]` / `[ReviewPending]` / `[ReviewMerged]` / `[ReviewRejected]` | Conveyor-style breadcrumbs queryable per task |

## MVP scope (this week)

Critical path: take `lithos-lens/docs/prd/milestone-1-operator-view.md`, decompose it, implement stories sequentially via Claude, land each behind a GitHub PR, with the trail visible in lithos-lens.

**Cut from MVP:** PRD generation, PRD review, agent story review, merge-stories, brain `decide-next`, A2A endpoint, SSE event subscription, multi-host, Docker sandbox, hot-reload, crash recovery beyond fail-and-restart, configurable spot-check policy, GitHub webhooks, bash-runner, `lithos-coding-mcp` integration.

### Build order (4 days of focused work)

1. **Repo skeleton + result.json contract** — half day. `pyproject.toml` (uv), `lithos_loom/` package, MCP HTTP client, `result.json` schema validator, claim/poll/release loop, TOML config loader, project-affinity resolution.
2. **Salvage from Ralph++** — half day. Extract `worktree.py`, `agents.py` (claude/codex subprocess + stream-json capture), `git.py` (base SHA + commit list since base) into `lithos_loom/runner/`.
3. **`story-implement` plugin** — one day (start here, gives end-to-end signal first). Worktree off integration branch, prompt template (PRD body + story brief + project AGENTS.md), Claude run, commit detection, `gh pr create`.
4. **`story-review-human` plugin** — half day. `gh pr view` poller; on `MERGED` post `[ReviewMerged]` finding and complete; on `CLOSED` post `[ReviewRejected]` and fail.
5. **`prd-decompose` plugin** — one to one-and-a-half days. Adapt Pocock `to-issues` SKILL.md, prompt Claude with the PRD plus a structured-output schema (`{stories: [{title, brief, acceptance_criteria, deps, files_hint}]}`), write story docs, create tasks with `metadata.depends_on` for strict-sequential, create the integration branch.
6. **End-to-end run** on lithos-lens milestone-1 — half day. Expect breakage. Fix.

**Done bar:** one story merged via the loop, the next dependent story auto-unblocks and runs end-to-end without intervention.

## Repo layout

```
lithos-loom/
├── pyproject.toml                    # uv-managed
├── lithos_loom/
│   ├── daemon.py                     # poll loop, claim/release
│   ├── config.py                     # TOML loader
│   ├── lithos_client.py              # MCP HTTP client
│   ├── route.py                      # tag matching, dep resolution
│   ├── plugin_runner.py              # subprocess + result.json
│   ├── runner/                       # Salvaged from Ralph++
│   │   ├── worktree.py
│   │   ├── agents.py                 # claude/codex subprocess + stream-json
│   │   └── git.py
│   └── plugins/                      # Bundled plugins
│       ├── prd_decompose/
│       │   ├── __main__.py
│       │   └── prompt.md             # adapted from Pocock to-issues
│       ├── story_implement/
│       │   ├── __main__.py
│       │   └── prompt.md
│       └── story_review_human/
│           └── __main__.py
├── docs/
│   ├── PLAN.md                       # this document
│   ├── plugin-contract.md            # result.json schema
│   ├── routing.md                    # tag → route + dep resolution semantics
│   ├── mvp-walkthrough.md            # lithos-lens milestone-1 worked example
│   └── prd/
│       ├── mvp.md                    # PRD for MVP
│       └── full.md                   # PRD for full system
└── examples/
    └── lithos-loom.toml              # example config
```

## Ambitious roadmap (weeks)

Each item is independently shippable. Pick order by what hurts most.

| ID | Item | Week | Dependency |
|----|------|------|------------|
| A1 | Plugin SDK (`lithos_loom.plugin_api`) + `bash-runner` built-in | 1 | MVP |
| A2 | `prd-generate` + `prd-review-agent` + `prd-review-human` | 1–2 | MVP |
| A3 | `story-review-agent` + `story-fix` + per-project `review_policy` | 2 | MVP |
| A9 | `lithos-coding-mcp` + Lithos-aware Claude config dir | 2 (pulled forward) | MVP |
| A4 | `decide-next` brain plugin | 2–3 | A3 |
| A5 | Crash recovery (conveyor-C) + introspection loop (`loom-improve`) | 3 | MVP |
| A8 | `merge-stories` plugin | 3 | MVP |
| A6 | A2A endpoint for Agent Zero / Hanuman | 3 | MVP |
| A7 | Multi-host, PRD-affinity, GitHub webhook receiver | 4 | A6 |
| A10 | Docker sandbox option for `story-implement` | later | A1 |

## Open questions for follow-up rounds

- **Custom prompts per project.** Should story prompts be overridable per project (e.g. add the project's coding-style ruleset)? Likely yes; expose via project config block.
- **Cost / token budget tracking.** The conveyor dashboard parses stream-json for cost. Should Loom expose a per-task / per-PRD cost summary as a finding? Probably yes for ambitious; out of scope for MVP.
- **Story doc content templating.** Format of the rich story brief — bullet-list of acceptance criteria, problem-excerpt, file-path hints, links to upstream ADRs. Worth converging on a standard schema before A9 lands so coding agents pulling story docs via `lithos-coding-mcp` get a predictable shape.
- **Failure escalation policy.** Today: fail the task, leave dependents blocked. Better: spawn a `[NeedsHuman]` task and surface in lithos-lens "Needs attention" section.
