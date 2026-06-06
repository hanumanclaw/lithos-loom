# ADR 0001 — Per-project GitHub-watch config in document metadata

- **Status:** Accepted
- **Date:** 2026-06-05
- **Deciders:** Dave Snowdon

## Context

The GitHub issue watcher needs per-project configuration attached to each
watched project: which GitHub repo(s) the project maps to, whether watching is
currently enabled, and optional import-time exclude filters (labels / authors).
This config belongs to the project, so its home is the canonical
project-context document Loom already maintains in Lithos.

Lithos documents expose a writable, server-queryable free-form metadata field
(the document `extra`), reachable through the MCP surface:

- `lithos_write(metadata=...)` — additive per-key merge into the document's
  `extra` (upstream [agent-lore/lithos#305]).
- `lithos_read` / `lithos_list` return that metadata (the list item carries it
  directly under its `metadata` key).
- `lithos_list(metadata_match=...)` / `lithos_task_list(metadata_match=...)`
  filter by metadata; a scalar query matches a stored scalar **or** membership
  in a stored list (upstream [agent-lore/lithos#306]).

A project may map to **more than one** repo (e.g. a product split across
frontend / backend / infra repositories), so the repo mapping is a list.

## Decision

Store the watcher's per-project config in the project-context doc's free-form
metadata, under these keys:

| Concern | Metadata key | Type |
|---|---|---|
| Repo mappings | `github_repos` | `list[str]` of `owner/name` (one or more) |
| Watching on/off | `github_watch_enabled` | `bool` |
| Exclude labels | `github_exclude_labels` | `list[str]` (import-time; applies to all the project's repos) |
| Exclude authors | `github_exclude_authors` | `list[str]` (import-time; applies to all the project's repos) |

Mutations go through a shared read-mutate-write CAS loop
(`src/lithos_loom/cli/_github_metadata.py`) using `expected_version` optimistic
locking, exposed via:

- `project add-github-repo <slug> <owner/name>` / `remove-github-repo` — manage
  the `github_repos` list.
- `project enable-github <slug>` / `disable-github` — toggle
  `github_watch_enabled` (enable requires a non-empty repo list).

Discovery is a single server-side filtered call —
`note_list(path_prefix="projects/", metadata_match={"github_watch_enabled": True})`
— and each returned list item carries its `metadata`, so the watcher reads the
repo list and exclude filters straight from the enumeration with no per-doc
follow-up read.

Because `github_repos` is a typed list and metadata values are typed JSON,
this storage is:

- **semantically clean** — config does not pollute the document's `tags` (used
  for categorisation / discovery and counted by `lithos_tags`);
- **typed** — booleans are booleans, lists are lists, with no string encoding;
- **multi-valued natively** — a project's several repos are one list, not a
  hand-enforced "exactly one" invariant;
- **space-safe** — exclude values (e.g. GitHub label names containing spaces)
  are stored verbatim.

## Consequences

- The watcher's poll cursors are keyed by repo (`owner/name`), the coord doc
  serialises one cursor line per repo, and the sync / push subscriptions are
  per-issue (each task carries `metadata.github_issue_url`, which encodes its
  own repo). Multi-repo projects therefore required no change to cursor state,
  the coord doc, or downstream sync — only the watch-list shape, the poll-loop
  iteration, and the per-repo cursor-reset logic.
- Adding a sibling repo to a project resets only the new repo's cursor; the
  repos it already tracks keep theirs.
- Requires the upstream Lithos metadata write + filter surface
  ([#305]/[#306]); `lithos-loom doctor` probes for writable document metadata
  so an older Lithos fails fast.

## Migration

The earlier implementation stored this config as structured tags
(`github-repo:owner/name`, `github-watch`, `github-exclude-{label,author}:*`).
`lithos-loom project migrate-github-tags` ports any project-context doc still
carrying those tags onto the metadata keys above and strips the tags, in one
CAS write per doc (`--dry-run` previews; the command is idempotent). The
migration is the only code that references the legacy tag strings; it can be
removed once every live doc has been migrated.

[agent-lore/lithos#305]: https://github.com/agent-lore/lithos/issues/305
[agent-lore/lithos#306]: https://github.com/agent-lore/lithos/issues/306
[#305]: https://github.com/agent-lore/lithos/issues/305
[#306]: https://github.com/agent-lore/lithos/issues/306
