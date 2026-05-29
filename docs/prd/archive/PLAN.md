# Lithos Loom — Build Plan

Status: draft
Date: 2026-04-30 (re-prioritised 2026-05-05)
Owner: Dave Snowdon
Related repos: [lithos](https://github.com/agent-lore/lithos), [lithos-lens](../lithos-lens), [ralph-plus-plus](../../agents/ralph-dev/ralph-plus-plus) (source of salvageable bits)

> **Re-prioritisation notice (2026-05-05):** Track 1 (Lithos ↔ Obsidian bridge) ships **before** the original MVP plan (now Track 2). Architectural reframe to `sources → bus → subscribers` is pre-invested during Track 1 with no scope expansion to Track 2. See [docs/prd/integration.md](prd/integration.md) for the Track 1 PRD and the 22 locked design decisions (D1–D22) for the integrated system.

> See also:
> - [docs/prd/integration.md](prd/integration.md) — **Track 1 PRD** (Obsidian bridge; ships first)
> - [docs/prd/mvp.md](prd/mvp.md) — Track 2 PRD (PRD → PR automation; ships second)
> - [docs/prd/full.md](prd/full.md) — Full automated workflow system roadmap (A1–A10)
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
| Deployment | Loom runs as a **host process**, not a docker service. Lithos and Influx are services (well-defined protocols, no shelling-out, no host filesystem coupling); Loom is an orchestrator with deep host integration (git worktrees, `claude`/`codex`/`gh` CLI auth in `~/`, plugin subprocess spawning). MVP runs manually via `uv run lithos-loom run` in terminal or tmux. Systemd `--user` unit is a deferred polish item. The template's `docker/` directory is dropped during bootstrap; `python-dotenv` and `.env`-style config from the template are retained for per-host paths (Lithos URL, work_dir, Claude config dir). |
| Agent identity | Loom registers as `lithos-orchestrator-<host>`; coding sub-agents are `claude-code` / `codex` per the existing agent identity model. |
| Architecture (added 2026-05-05) | `sources → bus → subscribers`. Loom is an event router; the route runner is one kind of subscriber (claim-bound). Subscriptions are fire-and-forget side effects with per-subscription retry. Supervisor pattern: one TOML config, subprocess children, monolithic v1 lifecycle. See [docs/prd/integration.md](prd/integration.md) D3, D4, D11, D12, D13, D16. |
| Sequencing (added 2026-05-05) | Track 1 (Obsidian bridge) ships first with bus architecture pre-investment; Track 2 (this PLAN's plugin MVP) ships second with plugins slotting into the existing bus. Daily-friction value framing replaces the original Loom-first ordering. |
| Slug convention (added 2026-05-05) | Slug = directory name under `knowledge/projects/<slug>/` in Lithos KB. Loom TOML `[projects.<slug>]` matches; Lithos enforces uniqueness via `slug_collision`. No frontmatter slug field, no v1 rename mechanism. |

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

## Lithos prerequisites (verified 2026-05-05)

Verified against `lithos/docs/SPECIFICATION.md` v0.1.5 by inspection. Full audit lives in [docs/prd/integration.md § Lithos Prerequisites](prd/integration.md#lithos-prerequisites-verified-2026-05-05); summary of upstream blockers below.

| Item | Blocking | Status |
|------|----------|--------|
| `task.metadata` field on tasks | All `metadata.*` references throughout this plan (`depends_on`, `parallelizable`, `project`, `prd_doc_id`, `story_doc_id`, `integration_branch`, `pr_url`, `priority`, `scheduled_for`, `host_affinity`, `github_issue_url`, `parent_task_id`) | `agent-lore/lithos#215` |
| `lithos_task_reopen` tool | Clean untick semantics in Track 1 (current workaround: `[ReopenRequested]` finding) | `agent-lore/lithos#243` |

Findings that simplified the design vs. earlier assumptions:
- Slug = filename / directory name; Lithos enforces uniqueness with `slug_collision` envelope. No frontmatter slug field needed.
- `lithos_write` accepts `id` for update + `expected_version` for optimistic locking; `version_conflict` envelope returns `current_version`. This gives Track 1 v0.3 (bidirectional project context) clean conflict semantics.
- Doc events are named `note.created` / `note.updated` / `note.deleted` (not `doc.*`). Available on the `GET /events` SSE endpoint.
- `note_type` enum does not include `project_context`; use `note_type: concept` + tag `project-context`.

## Sequencing notes

Track 1 → Track 2 → full system roadmap (A1–A10) is the macro ordering. Within that, three nuances:

1. **Slice 0 of Track 1 (bus + supervisor scaffolding) is independent of `agent-lore/lithos#215`.** It can begin immediately and produces no user-visible features but pre-invests the architecture. The `#215` `task.metadata` field is required from **slice 1 onwards** (the moment task projection actually needs `metadata.scheduled_for`, `metadata.priority`, etc.). If `#215` lands behind schedule, slice 0 is the right place to be in the meantime.

2. **Track 2 starts after Track 1 has been daily-usable for at least a week, not on slice 5 completion.** The soaking period is deliberate: it surfaces real-world friction in projection filters, line shape, query patterns, and capture macros that synthetic testing will miss. Don't pile Track 2 plugin work on top of an unproven Track 1 surface.

3. **Track gating, not strict serialisation.** Once Track 2 begins, expect that `prd-decompose`-created tasks and `story-implement`-claimed tasks will surface Track 1 polish work in flight (formatting edge cases, filter exclusions, tag conventions for new route names). Handle those as tactical fixes alongside Track 2 progress rather than as a separate phase. Track 1 is "done enough to start Track 2," not "frozen forever."

The full A1–A10 roadmap (see `docs/prd/full.md`) layers on top of Tracks 1+2 in the order documented there: A1 → A2 → A3 → A9 (pulled forward) → A4 → A5 → A8 → A6 → A7 → A10. Each layer remains independently shippable.

## MVP scope (Track 2 — this week after Track 1 lands)

> **Note:** This MVP is now Track 2. Track 1 (Obsidian bridge — see [docs/prd/integration.md](prd/integration.md)) ships first and pre-invests the bus architecture. The Track 2 scope below is unchanged from the original plan; only sequencing changed.

Critical path: take `lithos-lens/docs/prd/milestone-1-operator-view.md`, decompose it, implement stories sequentially via Claude, land each behind a GitHub PR, with the trail visible in lithos-lens.

**Cut from MVP:** PRD generation, PRD review, agent story review, merge-stories, brain `decide-next`, A2A endpoint, SSE event subscription, multi-host, Docker sandbox, hot-reload, crash recovery beyond fail-and-restart, configurable spot-check policy, GitHub webhooks, bash-runner, `lithos-coding-mcp` integration.

### Build order (4 days of focused work)

1. **Repo skeleton from `~/projects/templates/python` + result.json contract** — ~half day (the template handles uv / pyproject / Makefile / ruff / pyright / pytest / Docker / CI; rename `influx` → `lithos_loom`, register the console entry, then build MCP HTTP client, `result.json` schema validator + checked-in `docs/result-schema.json`, claim/poll/release loop, TOML config loader, project-affinity resolution).
2. **Salvage from Ralph++** — half day. Extract `worktree.py`, `agents.py` (claude/codex subprocess + stream-json capture), `git.py` (base SHA + commit list since base) into `lithos_loom/runner/`.
3. **`story-implement` plugin** — one day (start here, gives end-to-end signal first). Worktree off integration branch, prompt template (PRD body + story brief + project AGENTS.md), Claude run, commit detection, `gh pr create`.
4. **`story-review-human` plugin** — half day. `gh pr view` poller; on `MERGED` post `[ReviewMerged]` finding and complete; on `CLOSED` post `[ReviewRejected]` and fail.
5. **`prd-decompose` plugin** — one to one-and-a-half days. Adapt Pocock `to-issues` SKILL.md, prompt Claude with the PRD plus a structured-output schema (`{stories: [{title, brief, acceptance_criteria, deps, files_hint}]}`), write story docs, create tasks with `metadata.depends_on` for strict-sequential, create the integration branch.
6. **End-to-end run** on lithos-lens milestone-1 — half day. Expect breakage. Fix.

**Done bar:** one story merged via the loop, the next dependent story auto-unblocks and runs end-to-end without intervention.

## Repo layout

Bootstrapped from `~/projects/templates/python` (uv + Python 3.12 + src-layout + ruff + pyright + pytest + Makefile + CI). The skeleton handles `pyproject.toml`, `Makefile`, `.github/workflows/ci.yml`, `tests/conftest.py`, `.gitignore`, dotenv-based config, etc. The template's `docker/` directory is **dropped** during bootstrap because Loom runs on the host (see deployment decision above). Only the application tree below is project-specific:

```
lithos-loom/
├── (template scaffolding: pyproject.toml, Makefile, .python-version,
│   .github/workflows/ci.yml, docker/, tests/conftest.py, .gitignore, ...)
├── src/
│   └── lithos_loom/
│       ├── __init__.py
│       ├── __main__.py
│       ├── main.py                   # CLI entry: lithos-loom run / doctor / validate-config
│       ├── daemon.py                 # poll loop, claim/release
│       ├── config.py                 # TOML loader
│       ├── lithos_client.py          # MCP HTTP client
│       ├── route.py                  # tag matching, dep resolution
│       ├── plugin_runner.py          # subprocess + result.json
│       ├── runner/                   # Salvaged from Ralph++
│       │   ├── worktree.py
│       │   ├── agents.py             # claude/codex subprocess + stream-json
│       │   └── git.py
│       └── plugins/                  # Bundled plugins
│           ├── prd_decompose/
│           │   ├── __main__.py
│           │   └── prompt.md         # adapted from Pocock to-issues
│           ├── story_implement/
│           │   ├── __main__.py
│           │   └── prompt.md
│           └── story_review_human/
│               └── __main__.py
├── docs/
│   ├── PLAN.md                       # this document
│   ├── plugin-contract.md            # result.json schema
│   ├── result-schema.json            # versioned JSON Schema (US-33)
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
