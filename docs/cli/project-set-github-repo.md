# `lithos-loom project set-github-repo` / `enable-github` / `disable-github`

Three sibling subcommands that configure the per-project GitHub issue
watcher — see [`docs/SPECIFICATION.md`](../SPECIFICATION.md) §2.2 and
§4.10–§4.12. They manage two tags on the canonical project-context
doc (`projects/<slug>/<slug>-project-context.md`):

- **`github-repo:<owner>/<name>`** — exactly one per project; carries
  the repo mapping. Replaces an existing tag on re-run so a typo is
  fixable.
- **`github-watch`** — presence enables the watcher's polling for the
  project. Removing it pauses watching without losing the repo mapping.
- **`github-exclude-label:<name>`** (zero or more) — drop matching
  issues at import time. Edit the tag list directly via MCP / Lithos to
  add or remove these; no dedicated CLI command yet.
- **`github-exclude-author:<login>`** (zero or more) — same shape;
  drop issues opened by these GitHub logins (e.g. `dependabot[bot]`).

The host-side watcher subprocess is gated separately by
`[github_watcher].enabled` in TOML; the CLI does not start or stop the
daemon child.

## Synopsis

```
lithos-loom project set-github-repo <slug> <owner/name> [-c CONFIG]
lithos-loom project enable-github   <slug>              [-c CONFIG]
lithos-loom project disable-github  <slug>              [-c CONFIG]
```

## `set-github-repo`

Validates `owner/name` against GitHub's repo-naming rules at CLI input
time (alphanumerics + `-` for the owner; alphanumerics + `_ . -` for the
name) and fails with exit 2 before any Lithos write if the form is
invalid. A typo that hit Lithos and surfaced only when the watcher
hit a 404 on the next poll would be a much slower failure mode.

Read-mutate-write happens under a CAS round-trip: the helper reads the
doc's current version, removes any existing `github-repo:*` tag,
appends the new one, and `note_write`s with `expected_version=...`.
On `version_conflict` (a concurrent writer landed between the read and
the write) it re-reads and re-applies the mutator, up to 3 attempts
before giving up with exit 2.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Tag now set (or already correct — idempotent) |
| 1 | Lithos transport / unexpected `note_write` status |
| 2 | Invalid `owner/name` form · invalid slug · canonical doc missing · CAS exhausted |

## `enable-github`

Adds the `github-watch` tag if absent. Requires that a `github-repo:*`
tag is already present on the doc (exit 2 with an actionable error
pointing at `set-github-repo` if not — you can't watch a repo you
haven't bound). Idempotent: re-running on an already-enabled project
prints "already enabled" and exits 0 without a write.

The watcher does not begin polling for the project until the next
refresh cycle picks up the tag change. Refresh happens at most one
poll interval after the CLI write completes (the watcher subscribes to
`lithos.note.updated` on its in-process bus for `projects/` path
changes — see `docs/SPECIFICATION.md` §2.2).

## `disable-github`

Removes the `github-watch` tag while preserving the `github-repo:*`
tag. Re-enabling later doesn't need `set-github-repo`. Idempotent:
re-running on an already-disabled project prints "already disabled".

In-flight events for the slug still drain — disabling stops new poll
cycles from emitting events for that slug at most one interval later.

## Example session

```
# First time: bind the repo, then enable watching.
$ lithos-loom project set-github-repo lithos-loom agent-lore/lithos-loom
github repo set to agent-lore/lithos-loom on projects/lithos-loom/lithos-loom-project-context.md
$ lithos-loom project enable-github lithos-loom
github watching enabled on projects/lithos-loom/lithos-loom-project-context.md

# Operator made a typo and wants to fix it:
$ lithos-loom project set-github-repo lithos-loom agent-lore/lithos-loon
github repo set to agent-lore/lithos-loon on projects/lithos-loom/lithos-loom-project-context.md

# Pause watching for the project temporarily:
$ lithos-loom project disable-github lithos-loom
github watching disabled on projects/lithos-loom/lithos-loom-project-context.md

# Re-enabling preserves the repo mapping — no need to re-set the repo:
$ lithos-loom project enable-github lithos-loom
github watching enabled on projects/lithos-loom/lithos-loom-project-context.md
```

## See also

- [`docs/SPECIFICATION.md`](../SPECIFICATION.md) §2.2 — full data flow for the GitHub issue mirror.
- [`docs/prd/archive/github-issue-watcher.md`](../prd/archive/github-issue-watcher.md) — design decisions D45 (storage), D46 (linkage marker), D47 (closure mapping), D50 (per-host gate), plus Slice 7.2 stories #71–#75 covering bidirectional close + drift sync.
