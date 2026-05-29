# AGENTS.md — lithos-loom

## What this is

A workflow orchestration daemon for [Lithos](https://github.com/agent-lore/lithos) tasks. Two surfaces:

- **Obsidian bridge.** Projects open Lithos tasks into an Obsidian-Tasks-compatible inbox; pushes Obsidian-side status / priority / due-date edits back to Lithos; bidirectionally syncs project-context docs; Templater macros + CLI for capture and project import.
- **Route-runner.** Subscribes to Lithos's SSE event stream, matches `lithos.task.created` and `lithos.task.released` against TOML-configured routes (a task must carry every tag in the route's `match.tags`), claims tasks collision-safely, and dispatches subprocess plugins. The runner reads `status` from each plugin's `result.json` (other fields like `metadata_updates` and `artifacts` are schema-validated but not yet applied). Scaffolding for `prd-decompose`, `story-implement`, `story-review-human` is present; bodies are stubs. Tag edits on existing tasks (`task.updated`) do not currently re-trigger route matching.

Loom replaces [Ralph++](https://github.com/snarktank/ralph) as the user's coding orchestration approach; useful Ralph++ pieces (worktree creation, agent subprocess runner with stream-json, commit detection) are salvaged into `src/lithos_loom/runner/`.

## Non-obvious things to know

- **Loom runs on the host, not in docker.** Lithos and Influx are services (stable protocols, no host coupling) and run in docker; Loom is an orchestrator with deep host integration (worktrees, `claude`/`codex`/`gh` CLI auth in `~/`, plugin subprocesses, Templater macro requiring the CLI on Obsidian Desktop's PATH) so containerizing it would just bind-mount most of `~/`. Run via `uv run lithos-loom run` in tmux/foreground; systemd `--user` unit is a deferred polish item.
- **Per-environment configs.** `LITHOS_LOOM_ENVIRONMENT=dev` selects `config.dev.toml` from `./` and `$XDG_CONFIG_HOME/lithos-loom/`. Explicit `LITHOS_LOOM_CONFIG=/abs/path.toml` beats everything. `python-dotenv` loads `.env` from CWD.
- **Architecture is `sources → bus → subscribers`.** The supervisor spawns subprocess children per enabled category: `route-runner` when at least one `[[routes]]` stanza exists, and `obsidian-sync` when `[obsidian_sync]` is configured. Each child runs its own in-process `EventBus` instance with no inter-child IPC; both independently consume Lithos SSE.
- **Plugin contract = subprocess + atomic `result.json`.** Plugins are invoked as `<command> --task-json <p> --work-dir <p> --result-file <p>`. Schema is checked in at [`docs/result-schema.json`](docs/result-schema.json); validate plugin output against it. Atomic write uses temp + fsync + rename — partial files must never be observable.
- **Vault writes are dot-prefixed temp files.** Projection / archive / conflict writers use `.<filename>.tmp.<rand>` + `os.replace` for atomicity. The dot prefix matters: Obsidian Sync (and Dropbox-style observers) skip dotfiles, avoiding publish noise.
- **Lithos `task.metadata` is a hard prerequisite.** `lithos-loom doctor` probes for it on first run. Loom also requires `lithos_write(id=..., expected_version=...)` (optimistic locking for note-push), `note.*` SSE events, and `lithos_task_create(metadata=...)` (single-shot create with metadata).
- **Task dependencies live in `task.metadata.depends_on` (not Lithos edges).** Lithos's `edges.db` is doc-only; tasks are SQLite rows with no edge surface. Strict-sequential is the default; `metadata.parallelizable: true` allows concurrent execution among siblings.
- **`obsidian-projection` writes `tasks_file` from scratch on every flush.** Idempotent re-runs are no-ops thanks to atomic-write + content-hash dedup. Frontmatter-only edits to projected project-context docs are silently absorbed; `note-push` hashes the body only.
- **Stable finding prefixes** for machine-parseable breadcrumbs: `[Friction]`, `[ReopenRequested]`, `[BlockerFailed]`. Pick a fresh prefix when introducing a new one rather than overloading these — operator queries grep by prefix.
- **Project files stay clean.** Loom config is machine-local TOML; project repo `AGENTS.md` / `CLAUDE.md` files contain no Lithos / Loom references (except for projects in the Lithos ecosystem itself).

## Specifications

| Doc | Purpose |
|-----|---------|
| [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) | Operator + integrator reference: architecture, configuration, CLI, plugin contract, event bus, projection rules, finding prefixes, errors. Code and tests are the authoritative description of current behaviour — when the spec lags reality, fix the spec. |
| [`docs/result-schema.json`](docs/result-schema.json) | Versioned JSON Schema for the plugin `result.json` contract. |
| [`docs/cli/project-import.md`](docs/cli/project-import.md) | Full reference for `lithos-loom project import`. |
| [`docs/cli/project-regenerate-done.md`](docs/cli/project-regenerate-done.md) | Full reference for `lithos-loom project regenerate-done`. |
| [`docs/macros/README.md`](docs/macros/README.md) | Templater macro install + behaviour notes. |
| [`docs/prd/mvp.md`](docs/prd/mvp.md) | Plugin-body contract for `prd-decompose` / `story-implement` / `story-review-human` (orchestration shipped, bodies queued). |
| [`docs/prd/full.md`](docs/prd/full.md) | Roadmap for the larger automated workflow system. |
| [`docs/prd/archive/`](docs/prd/archive/) | Shipped PRDs preserved as historical context. |

## Pre-merge checks (mandatory)

```bash
make check
```

Runs:

- `ruff check` + `ruff format --check` (style + lint; per-file Typer B008 ignore in `main.py` and plugin `__main__.py` is intentional)
- `pyright` (typecheck — `_optional_path` uses overloads to keep callers' return types non-optional when a non-None default is passed)
- `pytest` (unit + integration; auto-clears `LITHOS_*` env per test via `conftest.py`)

All three must pass. CI runs the same on every PR.

When changing the plugin contract, update `docs/result-schema.json` AND `tests/test_plugin_runner.py`. When changing config schema, update `examples/lithos-loom.toml` AND `tests/test_config.py`. When adding a new plugin, ship it under `src/lithos_loom/plugins/<name>/` with a `__main__.py` entry point and add an example route stanza to `examples/lithos-loom.toml`. When changing any operator-visible surface (CLI flag, projection rule, event name, finding prefix), update `docs/SPECIFICATION.md` in the same diff.
