# `lithos-loom project` GitHub-watcher subcommands

Subcommands that configure the per-project GitHub issue watcher — see
[`docs/SPECIFICATION.md`](../SPECIFICATION.md) §2.2 and §4.10–§4.13. They
manage free-form metadata on the canonical project-context doc
(`projects/<slug>/<slug>-project-context.md`):

- **`github_repos`** — list of `owner/name` strings. A project may map
  several repos; each is polled independently. A non-empty list is what
  makes the watcher consider the project.
- **`github_watch_enabled`** — `true` enables the watcher's polling for
  the project; `false` pauses it without losing the repo list.
- **`github_exclude_labels`** (list) — drop issues carrying any of these
  labels at import time. Applies to every repo the project maps. Label
  names containing spaces are stored verbatim.
- **`github_exclude_authors`** (list) — same shape; drop issues opened by
  these GitHub logins (e.g. `dependabot[bot]`). Edit the exclude lists
  directly via MCP / Lithos; there is no dedicated CLI for them yet.

The host-side watcher subprocess is gated separately by
`[github_watcher].enabled` in TOML; the CLI does not start or stop the
daemon child.

## Synopsis

```
lithos-loom project add-github-repo    <slug> <owner/name> [-c CONFIG]
lithos-loom project remove-github-repo <slug> <owner/name> [-c CONFIG]
lithos-loom project enable-github      <slug>              [-c CONFIG]
lithos-loom project disable-github     <slug>              [-c CONFIG]
lithos-loom project migrate-github-tags [--dry-run]        [-c CONFIG]
```

All mutating subcommands run a read-mutate-write CAS round-trip: the
helper reads the doc's current version, applies the change to the
metadata, and `note_write`s with `expected_version=...`. On
`version_conflict` (a concurrent writer landed between read and write)
it re-reads and re-applies, up to 3 attempts before giving up with
exit 2. Metadata writes are an additive per-key merge, so they never
touch the doc's tags or other metadata keys.

## `add-github-repo` / `remove-github-repo`

`add-github-repo` validates `owner/name` against GitHub's repo-naming
rules at CLI input time (alphanumerics + `-` for the owner; alphanumerics
+ `_ . -` for the name) and fails with exit 2 before any Lithos write if
the form is invalid, then appends it to `github_repos` (idempotent if
already present). Call it once per repo to map several.

`remove-github-repo` drops a repo from the list (idempotent if absent).
Removing the last repo is allowed — the project is then unmapped; if
watching is still enabled with no repos left, the command prints a
warning because nothing will be polled.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | List updated (or already in the requested state — idempotent) |
| 1 | Lithos transport / unexpected `note_write` status |
| 2 | Invalid `owner/name` form · invalid slug · canonical doc missing · CAS exhausted |

## `enable-github`

Sets `github_watch_enabled = true`. Requires at least one repo in
`github_repos` (exit 2 with an actionable error pointing at
`add-github-repo` if the list is empty — you can't watch a repo you
haven't mapped). Idempotent: re-running on an already-enabled project
prints "already enabled" and exits 0 without a write.

The watcher does not begin polling for the project until the next refresh
cycle picks up the change — at most one poll interval after the CLI write
completes (the watcher subscribes to `lithos.note.updated` on its
in-process bus for `projects/` path changes — see
`docs/SPECIFICATION.md` §2.2).

## `disable-github`

Sets `github_watch_enabled = false` while preserving `github_repos`.
Re-enabling later doesn't need `add-github-repo`. Idempotent: re-running
on an already-disabled project prints "already disabled".

In-flight events for the slug still drain — disabling stops new poll
cycles from emitting events for that slug at most one interval later.

## `migrate-github-tags`

One-shot migration from the legacy tag-based scheme
(`github-repo:` / `github-watch` / `github-exclude-*` tags) to the
metadata keys above. Scans every project-context doc and, for any still
carrying github tags, writes the derived metadata and strips the tags in
one CAS write per doc. Multiple legacy `github-repo:*` tags on a doc are
collected into the `github_repos` list. Idempotent (a second run finds no
tags and is a no-op); `--dry-run` previews without writing. Exit 1 if any
doc fails its CAS retries.

## Example session

```
# First time: map the repo(s), then enable watching.
$ lithos-loom project add-github-repo lithos-loom agent-lore/lithos-loom
github repo agent-lore/lithos-loom added to projects/lithos-loom/lithos-loom-project-context.md (repos: agent-lore/lithos-loom)
$ lithos-loom project enable-github lithos-loom
github watching enabled on projects/lithos-loom/lithos-loom-project-context.md

# A project that spans several repos — add each:
$ lithos-loom project add-github-repo kindred-code kindred/web
$ lithos-loom project add-github-repo kindred-code kindred/api
$ lithos-loom project add-github-repo kindred-code kindred/infra

# Drop a repo (others keep being polled):
$ lithos-loom project remove-github-repo kindred-code kindred/infra
github repo kindred/infra removed from projects/kindred-code/kindred-code-project-context.md (repos: kindred/web, kindred/api)

# Pause watching for a project temporarily:
$ lithos-loom project disable-github lithos-loom
github watching disabled on projects/lithos-loom/lithos-loom-project-context.md
```

## See also

- [`docs/adr/0001-github-watch-config-storage.md`](../adr/0001-github-watch-config-storage.md) — why config lives in document metadata.
- [`docs/SPECIFICATION.md`](../SPECIFICATION.md) §2.2 — full data flow for the GitHub issue mirror.
- [`docs/prd/archive/github-issue-watcher.md`](../prd/archive/github-issue-watcher.md) — design decisions D45 (storage), D46 (linkage marker), D47 (closure mapping), D50 (per-host gate), plus Slice 7.2 stories #71–#75 covering bidirectional close + drift sync.
