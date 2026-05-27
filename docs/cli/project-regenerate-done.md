# `lithos-loom project regenerate-done` — reference

Rebuild a project's task-archive "done" file (`<vault>/_lithos/projects/<slug>/<slug>-done.md`) from Lithos by writing **every** resolved (completed + cancelled) task for the slug as a Tasks-plugin line.

```
lithos-loom project regenerate-done --slug <slug> [flags]
```

This complements the live `task-archive` subscription (Slice 6 — see [`docs/prd/task-archive.md`](../prd/task-archive.md)). The subscription appends resolved tasks incrementally as they happen; this command rebuilds the whole file on demand.

---

## TL;DR

```bash
# Preview the rebuilt file — no writes
lithos-loom project regenerate-done --slug lithos-loom --dry-run

# Rebuild it (prompts before overwriting an existing file)
lithos-loom project regenerate-done --slug lithos-loom

# Rebuild without the prompt
lithos-loom project regenerate-done --slug lithos-loom --yes
```

---

## Why this exists, and what it writes

The live archiver only records tasks the operator **surfaced** in `_lithos/tasks.md`, and only from the moment the daemon started archiving. So two gaps appear:

- Resolved-task history that predates the archiver never landed in a done file.
- A deleted or damaged done file can't be rebuilt from the running daemon, because "was this surfaced" is an **ephemeral, in-memory signal that cannot be reconstructed** after the fact.

This command therefore writes **all** resolved tasks for the slug — a complete-history snapshot, a superset of what the live archiver would have captured. That's a deliberate trade: you get a full history at the cost of including tasks that were never operator-visible (background / automation work). If you want only the surfaced set, don't regenerate — let the live archiver maintain the file.

For each resolved Lithos task with `metadata.project == <slug>`, one line is written:

```
- [x] <title> 🆔 lithos:<id> #project/<slug> ✅ <resolved-date>     # completed
- [-] <title> 🆔 lithos:<id> #project/<slug> ❌ <resolved-date>     # cancelled
```

Lines are sorted **ascending by resolution date** (oldest first, the way an append-only log would have grown), ties broken by task id. The file is bare Tasks-plugin lines — no header, no frontmatter.

The data comes from Lithos with **no time window**, so the rebuild is as complete as Lithos's retained history (Lithos has no documented resolved-task TTL).

---

## Overwrite semantics

`regenerate-done` **replaces** the file outright — it does not merge. Any current contents are discarded, including:

- the daemon's surfaced-only set (replaced by the all-resolved set), and
- any manual edits you made to the done file.

That's the point of "regenerate." The guard rails below make the overwrite deliberate.

If the rebuild finds **zero** resolved tasks for the slug and a done file already exists, confirming the prompt **clears** it to an empty file (it is not deleted — Obsidian Sync will propagate the now-empty note). If no file exists and there are zero tasks, the command is a no-op (nothing is created).

---

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--slug` / `-s` | (required) | Project slug. Must match `^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$`. |
| `--dry-run` | off | Print the line count + the lines that would be written, then exit. No writes, no prompt. |
| `--yes` / `-y` | off | Skip the overwrite confirmation prompt. |
| `--format` / `-f` | `text` | `text` prints the written path (or a human message); `json` prints a single-line object on every 0-exit path — `{slug, path, action, count, written}`, where `action` is one of `written` / `dry-run` / `noop` / `aborted`, `count` is the number of resolved-task lines, and `written` says whether the file was changed. |
| `--config` / `-c` | discovered | Explicit TOML config path. |

The confirmation prompt (`Overwrite … (N line(s))? [y/N]`) only fires when the done file **already exists** and `--yes` was not passed; a first-time write has nothing to clobber and proceeds silently.

### Scope notes

- **Single slug only.** There is no `--all`; run it per project.
- **`_unassigned` is out of scope.** The slug pattern rejects the leading underscore, so `--slug _unassigned` exits 2. The unassigned bucket can only be maintained by the live archiver.

---

## Exit codes

| Code | When |
|---|---|
| 0 | File written, or dry-run, or an empty no-op (no resolved tasks and no existing file), or the prompt was declined. |
| 1 | Config load failure, no `[obsidian_sync]` section, or Lithos unreachable / returned an error. |
| 2 | Bad `--slug` (fails the slug pattern) or unknown `--format`. |

---

## Running while the daemon is live

Safe, with one narrow caveat. The live archiver keeps appending **new** completions correctly — its in-memory per-slug dedup set only holds ids it archived, and a task resolves once, so regenerated historical ids are never re-appended.

The one real race: a completion that lands **between** this command's read from Lithos and its atomic file replace is dropped from the rebuilt file. Mitigation: re-run the command, or run it when the project is quiet. A daemon restart re-syncs the archiver's dedup set from the regenerated file. Because the op is manual and re-runnable, there's no hard lock.

The write itself is atomic (dot-prefixed temp + `os.replace`, the same #52-safe path the projection uses), and the dir-watcher excludes `-done.md`, so regenerating never triggers a `note-push` or reopen-request.

---

## Worked example

```bash
$ lithos-loom project regenerate-done --slug lithos-loom --dry-run -c config.dev.toml
NO CHANGES MADE — re-run without --dry-run to apply

Would regenerate /home/you/vault/_lithos/projects/lithos-loom/lithos-loom-done.md for project 'lithos-loom'
  resolved tasks found: 3

  - [x] Wire bulk import 🆔 lithos:abc #project/lithos-loom ✅ 2026-05-10
  - [-] Drop the old approach 🆔 lithos:def #project/lithos-loom ❌ 2026-05-15
  - [x] Ship task-archive 🆔 lithos:ghi #project/lithos-loom ✅ 2026-05-26

NO CHANGES MADE — re-run without --dry-run to apply

$ lithos-loom project regenerate-done --slug lithos-loom -c config.dev.toml
Overwrite lithos-loom-done.md (currently 1 line(s))? [y/N]: y
/home/you/vault/_lithos/projects/lithos-loom/lithos-loom-done.md
```
