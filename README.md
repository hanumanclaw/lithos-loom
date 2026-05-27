# lithos-loom

Workflow orchestration daemon for [Lithos](https://github.com/agent-lore/lithos) tasks. Ships two integration tracks:

- **Track 1 — Obsidian ↔ Lithos bridge.** Projects Lithos open tasks into an Obsidian-Tasks-compatible inbox, pushes Obsidian-side status / priority edits back to Lithos, and offers a CLI + Templater macro for capturing new tasks from Obsidian. Slices 1-3 of [`docs/prd/integration.md`](docs/prd/integration.md) are shipped; Slices 4-5 (project-context bidirectional sync) are queued.
- **Track 2 — Coding pipeline.** A `sources → bus → subscribers` daemon that polls Lithos for open tasks, claims them collision-safely, and dispatches subprocess plugins (`prd-decompose`, `story-implement`, `story-review-human`) that produce artifacts back into Lithos. Plugin scaffolding is in place; the plugin bodies are queued behind Track 1 daily-use validation.

It is the orchestration layer that connects Lithos to coding agents — replacing the [Ralph++](https://github.com/snarktank/ralph) approach with a fine-grained, fault-tolerant pipeline whose state lives in Lithos and whose hot state lives on the host filesystem.

## Status

- **Architecture:** `sources → bus → subscribers` with a supervisor + per-domain child processes (see [`docs/prd/integration.md`](docs/prd/integration.md) D3, D11). 528+ tests; ruff + pyright clean.
- **Track 1 — Obsidian bridge:**
  - Slice 1 (US7–15, read-only projection) — **shipped**
  - Slice 2 (US16–23, bidirectional status / priority push) — **shipped**
  - Slice 3 (US24–27, capture macro + CLI) — **shipped**
  - Slices 4–5 (project-context pull / push) — not started
- **Track 2 — Coding pipeline plugins:** scaffolded under `src/lithos_loom/plugins/` but bodies not yet built.

## Project documents

| Doc | Purpose |
|-----|---------|
| [`AGENTS.md`](AGENTS.md) | Non-obvious project facts + the mandatory pre-merge check |
| [`docs/PLAN.md`](docs/PLAN.md) | Locked design decisions, plugin contract, build order, roadmap A1–A10 |
| [`docs/prd/integration.md`](docs/prd/integration.md) | **Track 1 PRD** — Obsidian bridge, 38 user stories across 5 slices |
| [`docs/prd/mvp.md`](docs/prd/mvp.md) | **Track 2 MVP PRD** — original coding pipeline, ~35 user stories |
| [`docs/prd/full.md`](docs/prd/full.md) | Full system PRD — 75 user stories spanning A1–A10 |
| [`docs/macros/README.md`](docs/macros/README.md) | Slice 3 Obsidian capture macro — install instructions + behaviour notes. The macro source itself lives at [`docs/macros/capture-task.md`](docs/macros/capture-task.md) (copy-it-verbatim into your vault's Templater Template Folder). |
| [`docs/cli/project-import.md`](docs/cli/project-import.md) | **`lithos-loom project import` reference** — every flag, decision, exit code, and worked example for adopting existing Obsidian project docs into Lithos (greenfield doc + tasks, `--tasks-only`, `--force-tasks`, `--dry-run`). |
| [`docs/cli/project-regenerate-done.md`](docs/cli/project-regenerate-done.md) | **`lithos-loom project regenerate-done` reference** — rebuild a project's `<slug>-done.md` task archive from Lithos (all resolved tasks), the all-resolved-vs-surfaced semantic, flags, exit codes, and the live-daemon caveat. |
| [`docs/result-schema.json`](docs/result-schema.json) | Versioned JSON Schema for the plugin `result.json` contract |

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/) (recommended)
- A reachable Lithos server (MCP-over-SSE transport at `<LITHOS_URL>/sse`). Compatible with Lithos `#295` or later — the capture-macro CLI calls `lithos_task_create(metadata=...)` from `#295`, and the Obsidian-sync handlers depend on `#283` (task.updated event), `#286`/`#288` (resolved_at), `#290` (per-key metadata merge), and `#294` (full task envelope on status + new `task_get`).
- `claude` and/or `codex` CLI authenticated (Track 2 plugins only — not needed for Track 1)
- `gh` CLI authenticated (Track 2 plugins only — `story-implement`, `story-review-human`)
- `git` with worktree support
- **For the Obsidian bridge (Track 1):** Obsidian Desktop with [Templater](https://github.com/SilentVoid13/Templater) for the capture macro, and the [Tasks](https://publish.obsidian.md/tasks/Introduction) plugin for the daily-view queries that read projected lines.

## Install

```bash
uv sync
```

This installs the package in editable mode plus the dev dependencies. `lithos-loom` becomes available on the venv's PATH.

For end-user install once published:

```bash
uv tool install lithos-loom
```

### CLI on PATH for the capture macro

The Slice 3 Templater macro shells out to `lithos-loom task create` via Node's `child_process`. Obsidian Desktop inherits its PATH from the **desktop launcher session** (launchd on macOS, the systemd user session on Linux), **not** from your shell rc. Confirm Obsidian can find the binary:

```bash
# In Obsidian: Developer Console (Cmd-Opt-I / Ctrl-Shift-I) → Console tab:
require("child_process").execSync("which lithos-loom").toString()
```

If that errors, either:

- Install via `uv tool install lithos-loom` (puts the binary in `~/.local/bin/`, typically in the launcher PATH on Linux; on macOS use `launchctl setenv PATH ...` or symlink into `/usr/local/bin/`).
- Or hardcode the absolute path in the macro's `execFileSync` call (see [`docs/macros/README.md`](docs/macros/README.md)).

## Quick start

### 1. Bring up a Lithos server

Loom needs a running Lithos to talk to. Start it however you normally do (docker, host process, etc).

### 2. Drop a config in place

```bash
mkdir -p ~/.config/lithos-loom
cp examples/lithos-loom.toml ~/.config/lithos-loom/config.toml
$EDITOR ~/.config/lithos-loom/config.toml  # adjust paths to your machine
```

### 3. Set `LITHOS_URL` (and optionally pick an environment)

```bash
cp .env.example .env
$EDITOR .env  # set LITHOS_URL
```

`python-dotenv` loads `.env` from the current working directory automatically when Loom starts. You can also `export LITHOS_URL=...` in your shell rc.

### 4. Validate the config

```bash
uv run lithos-loom validate-config
uv run lithos-loom doctor
```

### 5. Run the daemon

```bash
uv run lithos-loom run
```

Loom runs as a foreground process. For background operation, use `tmux`, `nohup`, or eventually a `systemd --user` unit (deferred per `docs/PLAN.md`).

### 6. (Track 1) Install the Obsidian capture macro

If you want to create Lithos tasks from inside Obsidian, follow the install instructions in [`docs/macros/README.md`](docs/macros/README.md). Summary: copy `docs/macros/capture-task.md` verbatim into your vault's Templater Template Folder, register it via Templater, then bind your hotkey to **`Templater: Insert capture-task`** (not the auto-generated "Create" variant — that one produces an empty new note). Verify the CLI-on-PATH check above first.

## Per-environment configuration

Loom supports multiple parallel configurations on the same workstation via `LITHOS_LOOM_ENVIRONMENT`. Useful when you want a `dev` Loom pointing at a local Lithos and a `prod` Loom pointing at your real KB, or when you want a sandbox config for testing new routes.

Search order:

1. `LITHOS_LOOM_CONFIG=/abs/path/to/cfg.toml` — explicit path, beats everything
2. If `LITHOS_LOOM_ENVIRONMENT=dev` is set, look for `config.dev.toml` in:
   - `./config.dev.toml` (CWD — useful for project-local overrides)
   - `~/.config/lithos-loom/config.dev.toml`
3. Otherwise, look for `config.toml` in the same locations

```bash
# Run against dev config
LITHOS_LOOM_ENVIRONMENT=dev uv run lithos-loom run

# Run against prod config
LITHOS_LOOM_ENVIRONMENT=prod uv run lithos-loom run

# Or pin an explicit path
uv run lithos-loom run --config /tmp/experimental.toml
```

## Why Loom doesn't run in docker

Lithos and Influx run as docker services because they're services — long-lived, stable protocols, no host filesystem coupling. Loom is different: it creates git worktrees you can `cd` into, invokes `claude` / `codex` / `gh` CLIs that authenticate against per-user dotfiles, and spawns plugin subprocesses that need the same access. The Slice 3 capture macro additionally requires the CLI to be on Obsidian Desktop's PATH. Containerizing Loom would require bind-mounting `~/.claude/`, `~/.codex/`, `~/.config/gh/`, every project repo's parent dir, the vault directory, and `/var/run/docker.sock`, which defeats containerization. Loom runs as a host process; a `systemd --user` unit is a deferred polish item (see `docs/prd/full.md` US-62).

Individual `story-implement` runs *can* be sandboxed in docker (deferred A10 enhancement) for untrusted-code projects, but the orchestrator itself stays on the host.

## Subcommands

| Command | Purpose |
|---------|---------|
| `lithos-loom run` | Start the daemon: supervisor + per-domain children (route-runner, obsidian-sync). |
| `lithos-loom doctor` | Verify the vault is writable and Lithos has `task.metadata` (US15 + US-35). |
| `lithos-loom validate-config` | Typecheck the TOML, list configured projects / routes / subscriptions. |
| `lithos-loom validate-config --dry-run` | Also poll Lithos and print which routes / subscriptions would fire for each open task (US6). |
| `lithos-loom config --show` | Print the merged effective config (US-4). |
| `lithos-loom task create --project X --title Y [--brief Z] [--scheduled DATE] [--priority P] [--tags A,B] [--target-file PATH \| --no-insert]` | Create a Lithos task and emit its projected line (Slice 3 US24-27). Used by the Templater capture macro. |
| `lithos-loom project list [--format text\|json] [--source lithos\|toml]` | List projects with Lithos-canonical status + TOML-local overlay (Slice 4 US31 / D30). |
| `lithos-loom project create --title T [--slug S] [--tags A,B] [--body \| --body-file PATH] [--format text\|json]` | Create a new Lithos project-context doc (Slice 5 US36). Used by the `create-project` Templater macro. |
| `lithos-loom project import <source> [--slug S] [--tags A,B] [--tasks-only] [--no-tasks] [--force-tasks] [--yes] [--dry-run] [--format text\|json]` | Import an existing local Markdown file as a Lithos project, extracting `- [ ]` lines as real Lithos tasks. **Full reference:** [`docs/cli/project-import.md`](docs/cli/project-import.md). |
| `lithos-loom project regenerate-done --slug S [--dry-run] [--yes] [--format text\|json]` | Rebuild a project's `<slug>-done.md` task archive from Lithos (all resolved tasks, sorted by date). **Full reference:** [`docs/cli/project-regenerate-done.md`](docs/cli/project-regenerate-done.md). |
| `lithos-loom obsidian-sync show [--format text\|json]` | Print the resolved `[obsidian_sync]` block — vault_path, tasks_file, filter knobs. Used by the capture macro to discover the configured `tasks_file` path at runtime. |

## Project layout

```
lithos-loom/
├── pyproject.toml
├── Makefile                              # install / fmt / lint / typecheck / test / check
├── .python-version                       # 3.12
├── .env.example                          # template for shell-side LITHOS_* vars
├── .github/workflows/ci.yml
├── AGENTS.md                             # non-obvious project facts + pre-merge check
├── examples/
│   └── lithos-loom.toml                  # example TOML config
├── src/
│   └── lithos_loom/
│       ├── main.py                       # Typer dispatcher (entry point)
│       ├── __main__.py                   # `python -m lithos_loom`
│       ├── config.py                     # TOML loader (US-4)
│       ├── bus.py                        # in-process EventBus
│       ├── supervisor.py                 # spawns + monitors per-domain children
│       ├── doctor.py                     # vault + Lithos health checks (US15)
│       ├── errors.py                     # exception hierarchy
│       ├── lithos_client.py              # async MCP-over-SSE client
│       ├── plugin_runner.py              # subprocess + result.json schema (US-3)
│       ├── sync_state.py                 # projection ↔ fs-watcher coordination (US23)
│       ├── render.py                     # shared projected-line renderer (Slice 3)
│       ├── sources/
│       │   ├── lithos_event_stream.py    # Lithos SSE consumer
│       │   └── obsidian_fs_watcher.py    # vault file watcher (Slice 2)
│       ├── subscriptions/
│       │   ├── _obsidian_projection.py        # writes _lithos/tasks.md (Slice 1)
│       │   ├── _obsidian_status_transition.py # [ ]→[x]/[-], [x]→[ ] handlers (Slice 2)
│       │   ├── _obsidian_priority_changed.py  # priority emoji edit handler (Slice 2)
│       │   ├── _human_actionable.py           # projection filter
│       │   ├── route_runner.py                # claim-bound subscriber (Track 2)
│       │   └── _noop.py
│       ├── children/
│       │   ├── obsidian_sync.py          # obsidian-sync child entry point
│       │   ├── route_runner.py           # route-runner child entry point
│       │   └── _echo.py
│       ├── cli/
│       │   ├── task.py                   # `lithos-loom task create` (Slice 3)
│       │   └── project.py                # `lithos-loom project list` (Slice 3)
│       ├── runner/                       # salvaged from Ralph++ (Track 2)
│       │   ├── worktree.py
│       │   ├── agents.py
│       │   └── git.py
│       └── plugins/                      # bundled subprocess plugins (Track 2 — scaffolded)
│           ├── prd_decompose/
│           ├── story_implement/
│           └── story_review_human/
├── tests/                                # 528+ tests; ruff + pyright clean
└── docs/
    ├── PLAN.md
    ├── result-schema.json
    ├── macros/
    │   └── capture-task.md               # Slice 3 Templater macro source + install
    └── prd/
        ├── integration.md                # Track 1 (Obsidian bridge)
        ├── mvp.md                        # Track 2 MVP
        └── full.md                       # Roadmap A1–A10
```

## Development

```bash
uv sync          # create the venv and install deps
make check       # ruff + ruff format + pyright + pytest
```

`make check` is the mandatory pre-merge gate; all four stages must be green. See [`AGENTS.md`](AGENTS.md) for the non-obvious project facts and rules of engagement.

## Configuration model

| Layer | What it sets | When you change it |
|-------|--------------|--------------------|
| Defaults baked in | `poll_interval_seconds`, `max_concurrency`, `log_level`, etc | Almost never. |
| TOML config | `orchestrator.*`, project registry, route table, subscriptions, `obsidian_sync` | Per-machine, per-environment. Hot-reload deferred to A6. |
| `.env` (CWD) or shell rc | `LITHOS_URL`, `LITHOS_LOOM_CONFIG`, `LITHOS_LOOM_ENVIRONMENT` | Per-shell session. |
| CLI flags (`--config`, `--dry-run`) | One-off overrides | Per invocation. |

The TOML schema is documented inline in [`examples/lithos-loom.toml`](examples/lithos-loom.toml) and validated by `lithos_loom.config`.

## License

MIT — see [LICENSE](LICENSE).
