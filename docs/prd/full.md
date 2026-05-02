---
title: Lithos Loom — Full Automated Workflow System
milestone: M1–M5
status: draft
target_version: 1.0.0
references:
  - docs/PLAN.md (locked decisions, ambitious roadmap A1-A10)
  - docs/prd/mvp.md (the MVP this builds upon)
  - /home/dns/projects/lithos/code/lithos/docs/SPECIFICATION.md (Lithos task + knowledge surface)
  - Lithos KB projects/lithos-loom/lithos-loom-architecture-design.md
  - Lithos KB projects/lithos-loom/lithos-loom-requirements.md
  - Lithos KB projects/lithos-loom/lithos-loom-future-enhancements.md
  - Lithos KB projects/lithos-loom/lithos-coding-mcp-requirements.md
  - Lithos KB projects/lithos-loom/conveyer-notes.md
labels: [needs-triage, lithos-loom, orchestrator, full-system]
---

# Lithos Loom — Full Automated Workflow System

## Problem Statement

The MVP (`docs/prd/mvp.md`) proves the orchestration loop works against one PRD on one workstation, but it leaves most of the actual workflow manual: I write PRDs by hand outside Loom, I review every PR by hand, I merge integration branches by hand, I notice failures by hand, the agent invocations have no awareness of the wider Lithos knowledge base, the system has no memory of why prior runs went wrong, and the whole thing only runs on one machine.

The work I want Loom to do for me long-term is bigger than that:

- **Front-of-pipeline gap.** I want to type a feature description and have Loom produce a Pocock-shaped PRD that I review, refine, and approve before decomposition runs. Right now Loom only consumes PRDs.
- **Story review at scale.** Some projects warrant agent-driven reviews; others always need a human. The MVP assumes always-human. With dozens of stories per PRD, always-human becomes the bottleneck and I want a per-project policy: always-human, always-agent, every-N-stories spot check, or brain-decided.
- **No knowledge feedback loop.** When `story-implement` runs, Claude sees the PRD body and the project's AGENTS.md but cannot reach into the rest of Lithos for related ADRs, prior decisions, or contradictions. Implementations therefore can't accumulate compounding context.
- **No self-improvement.** Conveyor's strongest single idea is the `[Friction]` finding loop — workers note what's painful, and the system aggregates those into improvement tasks. Loom currently logs nothing about its own pain.
- **No multi-host story.** I have two capable workstations. The MVP assumes one. I want to run Loom on both, with PRD-affinity so a PRD's children land on the host that started it, but new PRDs balance across hosts.
- **PR latency.** `gh pr view` polling means a 30–60s window between merging a PR and the next story unblocking. For a 28-story PRD that adds up.
- **No safe-mode for experimental code.** Some implementations should run inside a Docker sandbox, not directly against the host. Ralph++ has this; Loom does not.
- **Brittle failure handling.** Today a failed story leaves a dependency chain dead and requires manual triage. There's no "decide what to do given the current state" agent.
- **No external triggers.** Agent Zero (samsara) and Hanuman (mac mini) cannot poke Loom directly — they have to create a Lithos task and wait for poll. For interactive use, that's too slow.

The result: the MVP is a proof, not a daily-workflow tool. To replace Ralph++ as my default coding orchestration, Loom needs all of the above.

## Solution

Layer the ambitious roadmap items A1–A10 from PLAN.md onto the MVP. Each is independently shippable; this PRD enumerates what "done" means for each layer and how they fit together.

The end state:

- I type a feature description into a Lithos task tagged `trigger:prd-generate`. Loom produces a Pocock-shaped PRD doc, reviews it (agent + me), and on my approval decomposes it into story tasks.
- Each story runs through `story-implement` against a per-PRD integration branch. Claude inside `story-implement` has Lithos-aware tools via `lithos-coding-mcp` — it pulls related ADRs as context and writes new ADRs back as it implements.
- Story review runs per the project's `review_policy`. Agent reviews emit findings; on rejection, `story-fix` patches in place; on persistent rejection, `decide-next` (a brain plugin) decides whether to escalate to me, batch-fix, or back out.
- `merge-stories` runs `make ci` on the integration branch and opens the final PR to `main` with a synthesised changelog from per-story commit messages.
- Every plugin records `[Friction]` findings when it hits something painful. A weekly `loom-improve` task aggregates them into improvement tasks for me to triage — a self-improvement backlog.
- An A2A endpoint lets Agent Zero / Hanuman trigger Loom immediately for known task IDs, ask for status, and cancel runs.
- Two workstations both run Loom against the same Lithos. PRD-affinity ensures children of one PRD stay on the host that started it (avoiding worktree fragmentation); new PRDs balance across hosts.
- A GitHub webhook receiver replaces `gh pr view` polling, dropping review-to-unblock latency to seconds.
- Optionally, `story-implement` runs inside Ralph++'s docker sandbox lifted into Loom for untrusted-code work.

The MVP's three-plugin core (`prd-decompose`, `story-implement`, `story-review-human`) and the `result.json` plugin contract remain unchanged. Everything below is additive — old configs continue to work, old plugins remain valid.

## User Stories

Ordered by ambitious-roadmap layer, build sequence within each layer.

### A1 — Plugin SDK + bash-runner (week 1)

1. As a plugin author, I want a `lithos_loom.plugin_api` Python library that exposes the abstract `Plugin` base class, helpers for emitting findings, reading task metadata, opening worktrees, launching coding agents, and writing `result.json` atomically, so that I can write a new plugin in a single short script without re-deriving the contract.
2. As a plugin author, I want plugins to be installable as separate uv packages discovered via Python entry points, so that I can ship a plugin from a separate repo without forking Loom.
3. As an operator, I want a built-in `bash-runner` plugin that takes a shell command from the route TOML and wires its stdout/stderr/exit code into a `result.json`, so that I can express a one-off plugin entirely from config without writing Python.
4. As an operator, I want `bash-runner` to support optional `outputs` declarations in the route — globbing files relative to the work-dir and uploading them to Lithos with configured tags and access scopes — so that simple file-producing plugins (e.g. data extraction scripts) need no code at all.
5. As an operator, I want `lithos-loom validate-config` to typecheck the TOML against the route schema, surface unknown plugins, and list which interpolation variables a route uses, so that I find errors before running.
5a. As an operator, I want `lithos-loom --dry-run` to poll for open tasks, evaluate route matching, render interpolated commands, and print a summary without claiming, executing, or writing anything to Lithos, so that I can validate a new route configuration against live task state before risking real runs.
5b. As an operator, I want routes to support an optional `[routes.match.conditions]` block that gates matching on metadata predicates (e.g. `task.meta.prd_lines_lt = 500`), so that a single tag can fan out to different plugins based on task content without writing a brain plugin.
5c. As a plugin, I want to optionally write a JSONL event stream at `{work_dir}/{task.id}/events.jsonl` with timestamped, ordered events (`step.started`, `step.finished`, `agent.turn`, `commit.detected`, `pr.opened`), so that lithos-lens (or any other observer) can subscribe to live progress instead of waiting for the final `result.json`.
5d. As an operator, I want plugins to accept an optional `--idempotency-key` from the daemon (defaulting to `task.id`) and short-circuit with the prior `result.json` if a run with that key has already completed in the work-dir, so that duplicate triggers (manual re-run, A2A `run task` race with poller) do not produce duplicate side effects.

### A2 — `prd-generate` + `prd-review-agent` + `prd-review-human` (weeks 1–2)

6. As the operator, I want a `prd-generate` plugin that takes a free-text feature description from a Lithos task and produces a Pocock-shaped PRD knowledge doc, so that I can start a project from a one-paragraph description rather than writing the PRD by hand.
7. As the operator, I want `prd-generate` to use Pocock's `to-prd` skill prompt structure adapted for Loom (problem / solution / numbered user stories / implementation decisions / testing decisions / out of scope / further notes), so that the PRDs Loom generates are immediately consumable by `prd-decompose`.
8. As the operator, I want `prd-generate` to write the PRD with `tags: [prd, project:<x>, draft]` and emit a follow-up task tagged `trigger:prd-review-agent`, so that the next stage in the pipeline starts automatically.
9. As the operator, I want a `prd-review-agent` plugin that runs Codex (or another Claude instance) against a draft PRD and posts structured findings (one finding per identified issue, prefixed `[PRDReview]`), so that obvious problems get caught before I see the PRD.
10. As the operator, I want `prd-review-agent` to mark its findings as `recommendation: revise | approve` and, on `revise`, retag the task `trigger:prd-fix` so a follow-up plugin can apply the fixes, so that minor PRD problems heal automatically.
11. As the operator, I want a `prd-review-human` plugin that posts a Lithos finding `[ReviewPending] PRD ready for human review: <doc-link>` and waits for a tag transition (`approved` or `rejected`) on the PRD doc itself, so that I can review the PRD in Obsidian or the Lithos CLI and signal approval without opening a web UI.
12. As the operator, I want `prd-review-human` to optionally open a temporary GitHub PR previewing the PRD as a Markdown file in a docs repo, so that I can review using the same diff tooling I use for code (configurable per project).
13. As the operator, I want approving a PRD to retag it with `trigger:prd-decompose` and clear the `draft` tag, so that decomposition kicks off automatically.

### A3 — `story-review-agent` + `story-fix` + per-project `review_policy` (week 2)

14. As the operator, I want each project entry in the Loom config to declare `review_policy = "always-human" | "always-agent" | "every-n" | "brain-decide"`, so that I can keep tight control over critical projects and let Loom run more autonomously on lower-stakes ones.
15. As the operator, I want a `story-review-agent` plugin that runs Codex (or another Claude) against a story branch's diff with a review prompt, posts structured findings, and emits `recommendation: approve | revise | reject`, so that agent-reviewed stories get a fast first pass.
16. As the operator, I want `story-review-agent`'s `approve` recommendation to retag the task `trigger:story-review-human-fast` (a lightweight human-merge plugin) or, in fully-autonomous projects, directly auto-merge the PR, so that approved stories don't queue waiting for me.
17. As the operator, I want `story-review-agent`'s `revise` recommendation to retag the task `trigger:story-fix` and pass the structured findings as plugin input, so that the fixer plugin knows exactly what to address.
18. As the operator, I want a `story-fix` plugin that creates a fix-up commit on the story branch by running Claude with the original story brief + the agent reviewer's findings, so that minor review issues heal automatically without backing out.
19. As the operator, I want `story-fix` to retry up to a configurable `max_fix_attempts` (default 3) before retagging the task as `trigger:story-needs-human`, so that loops don't run forever.
20. As the operator, I want `every-n` review policy to insert a human-review checkpoint after every Nth story for that PRD, so that I retain spot-check oversight without reviewing every diff.
21. As the operator, I want `brain-decide` to invoke the `decide-next` plugin (see A4) at story-review time to decide policy on a per-story basis given current PRD state, so that the system can shift gears mid-PRD if quality drops.

### A9 — `lithos-coding-mcp` + Lithos-aware Claude config dir (week 2, pulled forward)

22. As a coding agent (Claude or Codex) running inside `story-implement`, I want a small MCP tool surface tuned for the coding workflow — `get_implementation_context(feature)`, `get_architecture_decisions(topic)`, `write_adr(title, decision, rationale)`, `log_finding(summary)`, `report_contradiction(doc_id, description)` — so that I can pull related context on demand and contribute back to the KB without learning the full 28-tool Lithos surface.
23. As the operator, I want `lithos-coding-mcp` published as a separate uvx-installable package, so that I can use it from any Claude/Codex session, not just inside Loom.
24. As the operator, I want a `~/.claude-lithos/` config dir that includes the `lithos-coding-mcp` server registration and a `CLAUDE-LITHOS.md` skill file describing when and how to use the tools, so that injecting `--claude-config ~/.claude-lithos` into a `story-implement` invocation switches Claude into Lithos-aware mode.
25. As the operator, I want `story-implement` to inject `LITHOS_TASK_ID`, `LITHOS_AGENT_ID=claude-code`, `LITHOS_PRD_ID`, and `LITHOS_URL` env vars when invoking Claude, so that `lithos-coding-mcp` can attribute the agent's writes correctly without the agent needing to know the IDs.
26. As the operator, I want project repo `CLAUDE.md` / `AGENTS.md` files to remain free of any Lithos-specific content, with all Lithos integration coming from `~/.claude-lithos/`, so that the integration is invisible to the project repos themselves (exception: projects in the Lithos ecosystem itself).
27. As a coding agent, I want `write_adr` to set provenance correctly (path `architecture/`, `note_type: concept`, `derived_from_ids: [LITHOS_PRD_ID]`, `source_task: LITHOS_TASK_ID`, `tags: [adr, project:<x>]`), so that ADRs land in the KB with their lineage intact.
27a. As the operator, I want `lithos-coding-mcp` to ship an `AGENTS-LITHOS.md` skill file alongside `CLAUDE-LITHOS.md` for Codex sessions, so that both coding agents have equivalent guidance on when and how to use the five tools.
27b. As the operator, I want both skill files to encode the conveyor context-budget rule ("after a sub-skill or tool call returns, the very next token must be a tool call, not narration") and other prompt hygiene patterns, so that agent runs are predictable and cheap to debug.
27c. As the operator, I want unrecognised `LITHOS_AGENT_ID` values (anything other than `claude-code` / `codex`) to be auto-registered with type `unknown` and the raw value as display name, so that custom or experimental coding agents work without server-side allowlists.
27d. As the operator running ad-hoc coding sessions (outside Loom), I want a `lithos-coding-mcp launch <agent>` subcommand (e.g. `lithos-coding-mcp launch claude my-task`) that handles env-var injection, agent ID hardcoding (via internal dispatch table), task lookup by UUID or title fragment, optional task creation via `--new`, and pre-launch agent registration, so that I can run KB-aware coding sessions without remembering which env var controls which thing or hand-creating tasks first. The package ships a single binary with subcommands rather than separate per-agent launchers; users add their own shell aliases (`alias cdl='lithos-coding-mcp launch claude'`) for ergonomic short forms. Specified in detail in `lithos-coding-mcp-requirements.md` FR-7.

### A4 — `decide-next` brain plugin (weeks 2–3)

28. As the operator, I want a `decide-next` plugin that reads the full state of a PRD's children (all tasks + recent findings + diff sizes) and a configurable decision prompt, calls Claude with structured output, and emits one of the actions: `escalate_to_human`, `retry_failed`, `batch_fix(scope)`, `merge_now`, `cancel_remaining`, so that non-trivial workflow decisions can be delegated to a model.
29. As the operator, I want `decide-next` to be invocable both as a route handler (tagged tasks) and as a sub-call from other plugins (e.g. `story-fix` after max attempts), so that the brain is reusable across the workflow.
30. As the operator, I want the `decide-next` decision prompt to be customisable per project, so that a research project gets different escalation defaults from a production project.
31. As the operator, I want every `decide-next` invocation to write a `[BrainDecision]` finding with the prompt, the structured output, and the chosen action, so that I have an audit trail of agentic decisions.

### A5 — Crash recovery + `loom-improve` (week 3)

32. As the operator, I want each plugin to write filesystem progress checkpoints in `{work_dir}/{task.id}/progress.json` at meaningful steps (worktree-created, agent-started, agent-finished, commits-detected, pr-opened), so that on crash the next run has a checkpoint to recover from.
33. As the operator, I want plugins to read any prior `progress.json` on startup and to read the most recent `[Recovery]` finding for the task, so that resumed runs can branch behaviour rather than re-doing completed work.
34. As the daemon, I want orphan-claim cleanup at startup to additionally write a `[Recovery] interrupted at <last checkpoint>` finding for each cleaned-up claim, so that the next worker has clear breadcrumbs about where the previous run left off.
35. As the operator, I want every plugin to be able to emit `[Friction] <description>` findings when it hits something painful (unclear story brief, ambiguous PRD section, repeated test failure), so that pain points are captured as data.
36. As the operator, I want a recurring `loom-improve` task (created on a configurable schedule) that aggregates `[Friction]` findings since last run, classifies them by theme using Claude, and creates Lithos tasks for the top themes tagged `improvement`, so that pain feeds back into the system as work.
37. As the operator, I want `loom-improve` outputs to be reviewable in lithos-lens (improvement tasks tagged distinctly), so that I can triage and prioritise process improvements alongside feature work.
37a. As the operator, I want `story-implement` (and any plugin that runs a coding agent) to install an exit hook that auto-commits any uncommitted changes in the worktree if the agent process dies mid-turn, with commit message `loom: salvage WIP from <task_id>`, so that a crash or timeout does not lose work that was in progress.
37b. As the operator, I want the daemon's startup orphan-claim cleanup to additionally check for worktrees holding salvage commits and post an `[Recovery] WIP salvaged at <commit>` finding pointing at the SHA, so that the next worker (or I) can decide whether to continue from the salvage or start clean.

### A8 — `merge-stories` (week 3)

38. As the operator, I want a `merge-stories` plugin invoked as the terminal task on a PRD that takes the current integration branch state and runs the project's `make ci` (or configured equivalent), so that I have automated confirmation the integrated stories play together before I see the final PR.
39. As the operator, I want `merge-stories` to fail-fast on red CI and spawn one `trigger:story-fix` task per failing test (mapped via Claude's structured-output classification), so that systemic failures get triaged automatically.
40. As the operator, I want `merge-stories` on green CI to open the final PR to `main` with a synthesised changelog assembled from per-story commit messages and PR descriptions, so that I have one PR to review for the whole PRD instead of N individual ones.
41. As the operator, I want the final PR to be tagged with the PRD's project tag and link back to the parent PRD doc + parent decompose task in the description, so that lithos-lens can surface the in-flight PRD's status from the PR list.

### A6 — A2A endpoint (week 3)

42. As Agent Zero (or Hanuman), I want to call Loom's A2A endpoint with `run task <task_id>` to immediately claim and execute a specific task without waiting for the poll interval, so that interactive workflows don't have a 30s startup delay.
43. As Agent Zero, I want to call `status` to receive a list of currently-running tasks with their states, elapsed time, and current plugin names, so that I can answer "what's the orchestrator doing right now?".
44. As Agent Zero, I want to call `cancel <task_id>` to send SIGTERM to a running plugin and release the claim, so that I can stop a misbehaving run without ssh-ing to the workstation.
45. As Agent Zero, I want to call `reload config` to pick up TOML changes without restarting the daemon, so that I can iterate on routes mid-session.
46. As Agent Zero, I want to call `list routes` to see the current routing table (route names, match tags, output schemas), so that I can compose new tasks correctly.
47. As the operator, I want the A2A endpoint to be FastA2A-compatible and bind to a configurable port (default 9100), so that existing A2A clients work without protocol surgery.

### A7 — Multi-host, PRD-affinity, GitHub webhooks (week 4)

48. As the operator, I want Loom on each workstation to bind to a host-identified agent ID (e.g. `lithos-orchestrator-samsara`), so that claim attribution is unambiguous when two Loom instances coexist.
49. As the operator, I want each Loom instance to have its own project registry with paths only resolvable on that host, so that each host claims only tasks for projects it actually has checked out.
50. As the operator, I want PRD-affinity: when `prd-decompose` runs on host A, every child task gets `metadata.host_affinity = "samsara"` so only host A claims them, until the integration branch is merged to `main`, so that worktrees stay on the host that owns them.
51. As the operator, I want host-affinity to be releasable post-merge (the integration branch is on GitHub, any host can clone fresh worktrees off `main`), so that a project's next PRD can run on the other host.
52. As the operator, I want a `lithos-loom webhook` mode that runs an HTTP receiver listening for GitHub `pull_request` events, validates the GitHub webhook signature, and posts an internal event that `story-review-human` and `merge-stories` can subscribe to, so that PR state changes are reflected in Loom within seconds rather than the polling interval.
53. As the operator, I want the webhook receiver to fall back to polling cleanly when no webhook event has arrived in a configurable timeout, so that a misconfigured webhook doesn't strand a task.
53a. As the operator, I want each Loom instance to subscribe to Lithos's `GET /events` SSE stream filtered to `task.created` / `task.updated` / `task.completed`, react to relevant events immediately by re-evaluating route matching, and fall back to polling when SSE is unavailable, so that interactive task creation (e.g. via A2A from Agent Zero) does not wait for the next poll interval.
53b. As the operator, I want SSE event handling to be idempotent against the existing poll loop (events trigger the same matcher; double-evaluation is harmless because claim is collision-safe), so that having both signals active is safe and can be toggled without code changes.

### A10 — Docker sandbox option (later)

54. As the operator, I want `story-implement` to accept a `--sandbox` flag that runs Claude inside Ralph++'s docker sandbox (lifted into `lithos_loom/runner/sandbox.py`) instead of directly on the host, so that I can run untrusted-code stories safely.
55. As the operator, I want sandbox-mode runs to mount only the worktree directory and the Claude config dir into the container, so that the agent cannot read or modify anything else on the host.
56. As the operator, I want sandbox mode to be enabled per-project via a `sandbox = true` flag in the project config rather than per-task, so that the trust boundary is set at the project level and inherited by all stories.
57. As the operator, I want sandbox mode to record `[Sandboxed]` in the agent invocation finding, so that I can tell at a glance which runs were sandboxed when reviewing history.

### Cross-cutting

58. As the operator, I want token / cost / turn-count metrics from each Claude invocation to be parsed from stream-json output and posted as a `[Cost]` finding on the task, so that I can see what each task cost and identify expensive patterns.
58a. As the operator, I want `story-implement` (and any other implementation-shaped plugin) to post a `[Plan]` finding before invoking the coding agent, summarising what the plugin understands the task to be (story brief excerpt, integration branch, base SHA, target acceptance criteria), so that I can sanity-check the plugin's framing before any code runs.
58b. As the operator, I want `story-implement` to post a `[Drift]` finding after the coding agent exits, comparing what was actually built (commit messages, files touched, lines of test added) against the story brief's acceptance criteria via a brief structured Claude call, so that under- and over-delivery are surfaced without me reading the diff.
58c. As the operator, I want `[Drift]` findings to be queryable as a class so that periodic `loom-improve` aggregations can spot systemic over-/under-delivery patterns, so that the system can suggest prompt-template improvements over time.
59. As the operator, I want a `lithos-loom dashboard` CLI command that prints a summary of in-flight tasks per host, recent findings, and aggregate cost over the last 24h, so that I can do a daily glance check from the terminal.
60. As the operator, I want a `lithos-loom replay <task_id>` command that re-runs a task with the same plugin and inputs, useful after `story-fix` lands or after a flake, so that I can re-trigger work without recreating tasks.
61. As the operator, I want all plugin invocations to emit OpenTelemetry traces matching the Lithos telemetry config, so that the workflow shows up in the same observability stack as Lithos itself.
62. As the operator, I want a `systemd --user` unit shipped with Loom (e.g. `contrib/lithos-loom.service`) plus install / uninstall instructions, so that I can graduate Loom from manual `uv run` invocation to a managed background service once I trust the daemon's stability. The unit must restart on failure, forward logs to journald (which the existing OTel stack picks up), and respect SIGTERM for graceful shutdown.

## Implementation Decisions

**New deep modules** (in addition to the MVP's modules, which remain unchanged):

- **Plugin SDK** (`lithos_loom.plugin_api`) — abstract `Plugin` base class, `emit_finding`, `read_task_metadata`, `with_worktree`, `with_agent`, `write_result`. Deep module: stable interface that 3rd-party plugins depend on; breakage of this surface is a major version bump.
- **Webhook receiver** — HTTP server that accepts GitHub events, validates signatures, normalises to internal events, fans out to subscribers. Deep module: simple subscribe/publish interface; isolates GitHub-specific shape from review plugins.
- **A2A endpoint** — FastA2A-compatible RPC surface that translates A2A calls into daemon operations. Deep module: protocol shim, encapsulates the FastA2A wire format.
- **Decision prompt runner** (used by `decide-next`) — given a structured-output schema and a context payload, runs a Claude turn and returns validated structured output. Reusable by `prd-review-agent`, `story-review-agent`, `merge-stories` failure classifier, `loom-improve` aggregator. Deep module: encapsulates the LLM-with-validation pattern, retry-on-schema-mismatch, prompt logging.
- **Host-affinity resolver** — given a task and the local host's project registry, decides whether this Loom should claim. Pure function. Deep module: tested exhaustively, used at every poll cycle.
- **Sandbox runner** (`lithos_loom.runner.sandbox`) — lifted from Ralph++'s docker sandbox code, exposes `run_in_sandbox(image, mounts, command)`. Deep module that other plugins compose with.
- **Stream-json metrics parser** — given a stream-json log file, returns cost/turn/tool-call counts. Deep module, pure function.
- **JSONL plugin event writer** — append-only writer for `events.jsonl` with monotonic sequence numbers and UTC timestamps. Deep module, pure function.
- **SSE event subscriber** — long-running async client over Lithos `GET /events` with reconnect, backoff, and dedup against the in-memory ring buffer. Deep module, isolates SSE protocol from the matcher.
- **Worktree salvage helper** — exit-hook-installable function that detects uncommitted changes on plugin termination and creates a salvage commit. Deep module composed by every plugin that runs an agent.
- **Drift summariser** — given a story brief + the diff a plugin produced, calls Claude with a small structured-output prompt and returns `{built: [...], not_built: [...], extra: [...]}`. Deep module, reusable across `story-implement`, `story-fix`, `merge-stories`.

**Config schema additions:**

- `[orchestrator]` gains `mode = "polling" | "webhook"`, `webhook_port`, `a2a_port`
- `[projects.<name>]` gains `review_policy`, `sandbox`, `claude_config`, `host_affinity` (override)
- `[[routes]]` gains `next_route` (chaining), optional `[routes.match.conditions]` block (metadata-based gating), optional `decide_via_brain = true`, optional `max_runtime_seconds` (overrides MVP default), optional `idempotency_key` template
- New `[loom_improve]` section with `schedule_cron`, `friction_lookback_hours`, `max_themes`

**Task metadata additions (orchestration-only):**

- `host_affinity` — set by `prd-decompose` (A7), respected by all Loom instances
- `review_policy_override` — per-task override of the project default
- `friction_count` — incremented on each `[Friction]` finding for ranking
- `cost_total_usd` — sum of `[Cost]` findings on the task

**lithos-coding-mcp surface** (already designed in the KB note `lithos-coding-mcp-requirements.md`):

- Five tools, env-var-configured, separate uvx-installable package
- Skill files (`CLAUDE-LITHOS.md`, `AGENTS-LITHOS.md`) shipped with the package
- `~/.claude-lithos/` and `~/.codex-lithos/` config dirs are operator-managed copies that include both the MCP registration and the skill files

**Routing and chaining decisions:**

- Tag-based handoffs remain the default (BR3 from PLAN.md)
- `next_route` field in route config provides explicit chaining for sequential workflows (PRD-generate → PRD-review-agent → PRD-review-human → PRD-decompose), so the chain isn't fragile to tag rename
- `conditions` field allows simple metadata-based branching (e.g. `task.meta.prd_lines > 500 → trigger:prd-split`) without invoking the brain
- Brain (`decide-next`) is invoked only when configured via `decide_via_brain = true` on a route, or when `review_policy = "brain-decide"`

**Concurrency posture (full):**

- `orchestrator.max_concurrency` per host (default 4)
- Project-level limit `max_concurrent_tasks` (default 2 per project per host)
- Brain plugin (`decide-next`) is single-instance per PRD by default — never run two brain decisions on the same PRD concurrently

**Migration story from MVP:**

- The MVP's three plugins continue to work without modification; they get reclassified as built-in plugins shipped with the SDK
- `story-implement` gets opt-in `lithos-coding-mcp` integration via the `claude_config` project setting (defaults to project's existing claude config; setting it to `~/.claude-lithos/` enables Lithos-aware mode)
- The MVP's `story-review-human` is unchanged; A2/A3 add new plugins alongside it

## Testing Decisions

**Test philosophy:** unchanged from MVP — test external behaviour, not implementation details. Avoid over-mocking the LLM: where possible, use recorded LLM responses (vcrpy-style) to make tests deterministic but realistic.

**Modules with mandatory unit test coverage:**

- Plugin SDK — all helper functions tested with a synthetic plugin that exercises the full lifecycle (claim → emit findings → write result)
- Webhook receiver — signature validation (valid, invalid, malformed), event parsing for `pull_request` open/closed/merged, fan-out to subscribers
- A2A endpoint — each command (`run task`, `status`, `cancel`, `reload config`, `list routes`) with a stub daemon
- Decision prompt runner — happy structured output, schema mismatch retry, persistent failure handling, prompt-logging on every call
- Host-affinity resolver — exhaustive table-driven tests for: matching host, non-matching host, no affinity set, project not in local registry
- Sandbox runner — happy path (using a fixture image), file-mount restrictions enforced, network-disabled by default, container cleanup on plugin exit
- Stream-json metrics parser — fixture log files for happy path, partial logs, malformed entries

**Modules with integration test coverage:**

- `prd-generate` end-to-end against a fixture feature description with a recorded Claude response
- `prd-review-agent` against a known-bad PRD doc, asserting structured findings and `recommendation: revise`
- `story-review-agent` against a known-bad diff, asserting structured findings and `recommendation: revise`
- `story-fix` against a story branch + reviewer findings, asserting fix-up commit lands
- `merge-stories` against a fixture integration branch with green CI, asserting final PR opens; against red CI, asserting `story-fix` tasks spawn
- `decide-next` against fixture PRD states (3 failed children, 2 succeeded; all succeeded; mixed parallel branch), asserting the right action comes out
- `loom-improve` against a fixture batch of `[Friction]` findings, asserting aggregated improvement tasks are created
- A2A endpoint against a running daemon with a stub plugin, asserting commands work end-to-end
- Webhook receiver wired to `story-review-human`: simulated GitHub webhook → task completes within 5s
- Multi-host: two Loom instances pointed at the same Lithos, PRD-affinity correctly partitions claims (via fixture host names)

**Modules with manual acceptance test coverage** (not automated):

- The full feature-description-to-merged-PR loop end-to-end against a real project with a real Claude/Codex account
- The two-workstation deployment (samsara + mac mini) running concurrent PRDs

**Prior art:**

- MVP's pytest layout for plugins extends naturally to the SDK
- Lithos's telemetry tests for the OpenTelemetry trace emission patterns
- Ralph++'s sandbox tests for docker integration

**Recorded-response strategy:** every plugin that calls an LLM ships at least one recorded fixture under `tests/fixtures/<plugin>/<scenario>.jsonl` so tests run without live API calls. Re-recording is gated behind `LITHOS_LOOM_REFRESH_FIXTURES=1`.

## Out of Scope

The following remain out of scope even for the full system, deferred to later major versions or rejected outright:

- Web UI / dashboard (CLI dashboard from US-59 only)
- Cloud deployment (single-user, local-first remains the model)
- Distributed coordination beyond two workstations (no Kubernetes, no service mesh)
- Real-time collaboration (live cursor / co-editing)
- Replacing the underlying coding agents (Claude / Codex) — Loom orchestrates them; it does not implement them
- Cross-PRD dependency tracking (one PRD's children depending on another PRD's output) — possible via metadata but not surfaced as a first-class feature
- Cost optimisation routing (e.g. route easy stories to Haiku, hard stories to Opus) — defer to project config; no automatic model routing in v1
- GitHub issue creation / closing (deferred from MVP and remains deferred — webhook is the only direction)
- Non-GitHub forges (GitLab, Forgejo, sr.ht) — possible via `bash-runner` plugins but not first-class
- Human-readable PRD review UI inside Lithos itself — `prd-review-human` uses Obsidian or temporary GitHub PR instead
- Multi-tenant operation — single-trust-domain assumed throughout, mirroring Lithos's own non-goal §1.2
- Continuous integration of the orchestrator itself across hosts (e.g. shared work queue with locking) — claim-based coordination via Lithos remains sufficient

## Further Notes

- **Build order in PLAN.md is the authoritative sequencing.** A1 → A2 → A3 → A9 (pulled forward) → A4 → A5 → A8 → A6 → A7 → A10. Each layer is independently shippable; the order minimises context-switches and brings the highest-leverage items earliest.
- **Why A9 (`lithos-coding-mcp`) is pulled forward.** Without it, story prompts must include the entire PRD body. As PRD size grows this becomes the dominant cost. With it, Claude pulls relevant slices on demand and writes ADRs back, so the KB compounds as work happens. Once you've built A2 (`prd-generate` + `prd-review`) you'll feel the prompt-size pain immediately, so A9 should ship in week 2.
- **The conveyor patterns explicitly adopted.** Stateless plugins; filesystem progress checkpoints; prefixed findings as machine-parseable breadcrumbs (`[Plan]`, `[Drift]`, `[Recovery]`, `[Friction]`, `[ReviewPending]`, `[Cost]`, `[BrainDecision]`); friction-loop self-improvement; stream-json captured to disk.
- **The conveyor patterns explicitly NOT adopted.** Jira-as-queue (Lithos is the queue); tmux-per-worker (subprocess + work-dir is enough; tmux can be added per-plugin if useful for debugging); cron-only scheduling (Loom's daemon polls; cron-style scheduling is a `loom-improve` schedule helper, not the primary trigger).
- **Replaces Ralph++ entirely.** By the time A10 lands, Loom can do everything Ralph++ does (PRD generate, PRD review, story implement, post-review) plus everything Ralph++ cannot (knowledge integration, multi-host, brain-driven decisions, self-improvement). At that point Ralph++ can be archived; the salvaged code remains in Loom's `runner/` as the only legacy.
- **A2A endpoint is the integration point for Agent Zero / Hanuman.** This is what closes the loop from strategic agents (which decide what work matters) to operational agents (Loom, which does the work). Without A2A, those agents have to create Lithos tasks and wait — which is fine for batch but frustrating interactively.
- **Webhook receiver implies a small public ingress on each host.** Acceptable for the home-lab two-workstation setup using ngrok, Cloudflare Tunnel, or similar. Documented in deployment notes; not a security concern given the single-trust-domain model.
- **The "daily workflow" target.** End-state goal is that I sit down in the morning, look at lithos-lens, see in-flight PRDs across both hosts and recent findings, optionally trigger a few `prd-generate` tasks for new ideas, approve/reject reviews that are waiting, and let the system run. Loom's `[Friction]` findings tell me what's painful; `loom-improve` turns those into tasks I can triage. The system gets better over time without me touching its code.
- **What signals "done" for this PRD.** Not a single moment — each user story is independently shippable. The most useful waypoint is end of A9 (week 2), at which point the system is dramatically more useful than the MVP. Full system completion is end of A10 (~5 weeks), at which point Ralph++ can be archived.
