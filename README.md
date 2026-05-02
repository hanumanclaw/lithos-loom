# lithos-loom

Workflow orchestration daemon that turns [Lithos](https://github.com/agent-lore/lithos) tasks into executable work via bundled subprocess plugins.

Loom polls a Lithos knowledge base for open tasks, matches them against tag-based routes in a TOML config, claims them collision-safely, and dispatches plugins (subprocesses) that produce artifacts back into Lithos. Plugins shipped in the MVP build the full pipeline: decompose a [Pocock-shaped PRD](https://aihero.dev/) into story tasks, implement each story via a coding agent, and watch the resulting GitHub PR for human merge.

It is the orchestration layer that connects Lithos to coding agents — replacing the [Ralph++](https://github.com/snarktank/ralph) approach with a fine-grained, fault-tolerant pipeline whose state lives in Lithos and whose hot state lives on the host filesystem.

## Status

Skeleton — the package is scaffolded, the design and PRDs are in [`docs/`](docs/), and the implementation has not landed yet. Tracking work via the user stories in [`docs/prd/mvp.md`](docs/prd/mvp.md).

## Project documents

| Doc | Purpose |
|-----|---------|
| [`docs/PLAN.md`](docs/PLAN.md) | Locked design decisions, plugin list, plugin contract, repo layout, build order, ambitious roadmap |
| [`docs/prd/mvp.md`](docs/prd/mvp.md) | MVP PRD — 35 user stories, ~4 days of focused work |
| [`docs/prd/full.md`](docs/prd/full.md) | Full system PRD — 75 user stories spanning roadmap items A1–A10 |
| [`docs/result-schema.json`](docs/result-schema.json) | Versioned JSON Schema for the plugin `result.json` contract (US-3, US-33) |

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/) (recommended)
- A reachable Lithos server at `LITHOS_URL` (HTTP transport)
- Lithos with `task.metadata` support (`agent-lore/lithos#215` or later)
- `claude` and/or `codex` CLI authenticated against your account, depending on which agents your routes invoke
- `gh` CLI authenticated, for plugins that open GitHub PRs (`story-implement`, `story-review-human`)
- `git` with worktree support

## Install

```bash
uv sync
```

This installs the package in editable mode plus the dev dependencies. `lithos-loom` becomes available on the venv's PATH.

For end-user install once published:

```bash
uv tool install lithos-loom
```

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
```

### 5. Run the daemon

```bash
uv run lithos-loom run
```

Loom runs as a foreground process. For background operation, use `tmux`, `nohup`, or eventually a `systemd --user` unit (deferred per `docs/PLAN.md`).

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

Lithos and Influx run as docker services because they're services — long-lived, stable protocols, no host filesystem coupling. Loom is different: it creates git worktrees you can `cd` into, invokes `claude` / `codex` / `gh` CLIs that authenticate against per-user dotfiles, and spawns plugin subprocesses that need the same access. Containerizing Loom would require bind-mounting `~/.claude/`, `~/.codex/`, `~/.config/gh/`, every project repo's parent dir, and `/var/run/docker.sock`, which defeats containerization. Loom runs as a host process; a `systemd --user` unit is a deferred polish item (see `docs/prd/full.md` US-62).

Individual `story-implement` runs *can* be sandboxed in docker (deferred A10 enhancement) for untrusted-code projects, but the orchestrator itself stays on the host.

## Subcommands

| Command | Purpose |
|---------|---------|
| `lithos-loom run` | Start the daemon (poll loop). |
| `lithos-loom run --dry-run` | Preview matched tasks and rendered commands; no claims, no writes. |
| `lithos-loom doctor` | Verify Lithos connectivity and `task.metadata` support (US-35). |
| `lithos-loom validate-config` | Typecheck the TOML, list interpolation variables. |
| `lithos-loom config --show` | Print the merged effective config (US-4). |

## Project layout

```
lithos-loom/
├── pyproject.toml
├── Makefile                       # install / fmt / lint / typecheck / test / check
├── .python-version                # 3.12
├── .env.example                   # template for shell-side LITHOS_* vars
├── .github/workflows/ci.yml
├── examples/
│   └── lithos-loom.toml           # example TOML config
├── src/
│   └── lithos_loom/
│       ├── main.py                # Typer dispatcher (entry point)
│       ├── config.py              # TOML loader (US-4)
│       ├── daemon.py              # poll/claim loop (US-5, US-7, US-29)
│       ├── lithos_client.py       # async HTTP client over Lithos MCP (US-2)
│       ├── route.py               # tag matching + dependency resolution (US-5, US-6, US-9)
│       ├── plugin_runner.py       # subprocess + result.json schema (US-3, US-31, US-33, US-34)
│       ├── errors.py              # exception hierarchy
│       ├── runner/                # salvaged from Ralph++
│       │   ├── worktree.py        # per-task git worktree (US-11)
│       │   ├── agents.py          # claude/codex subprocess + stream-json (US-12)
│       │   └── git.py             # base SHA, commits-since (US-13)
│       └── plugins/               # bundled subprocess plugins
│           ├── prd_decompose/     # US-12, US-13
│           ├── story_implement/   # US-10, US-14-17
│           └── story_review_human/  # US-11, US-18-20
├── tests/
│   ├── conftest.py                # clean LITHOS_* env per test, loom_config_env fixture
│   ├── test_main.py               # CLI smoke
│   ├── test_config.py             # TOML loader behaviour
│   └── test_plugin_runner.py      # atomic-write contract
└── docs/
    ├── PLAN.md
    ├── result-schema.json
    └── prd/
        ├── mvp.md
        └── full.md
```

## Development

```bash
uv sync          # create the venv and install deps
make check       # ruff + pyright + pytest
```

## Configuration model

| Layer | What it sets | When you change it |
|-------|--------------|--------------------|
| Defaults baked in | `poll_interval_seconds`, `max_concurrency`, `log_level`, etc | Almost never. |
| TOML config | `orchestrator.*`, project registry, route table | Per-machine, per-environment. Hot-reload deferred to A6. |
| `.env` (CWD) or shell rc | `LITHOS_URL`, `LITHOS_LOOM_CONFIG`, `LITHOS_LOOM_ENVIRONMENT` | Per-shell session. |
| CLI flags (`--config`, `--dry-run`) | One-off overrides | Per invocation. |

The TOML schema is documented inline in [`examples/lithos-loom.toml`](examples/lithos-loom.toml) and validated by `lithos_loom.config`. See `docs/prd/mvp.md` US-4 for the full FR-level spec.

## License

MIT — see [LICENSE](LICENSE).
