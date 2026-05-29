# `lithos-loom project import` — reference

Import a local Markdown file as a Lithos project — and (by default) extract its `- [ ]` task lines as real Lithos task entities with dependency edges derived from indentation.

```
lithos-loom project import <source> [flags]
```

This is the canonical doc for the command. The PRD that drove its design is at [`docs/prd/archive/bulk-task-import.md`](../prd/archive/bulk-task-import.md).

---

## TL;DR

```bash
# Greenfield: create project + import open tasks from a vault doc
lithos-loom project import ~/Dropbox/obsidian/dave/projects/organising-myself.md

# Preview what would happen — no Lithos writes
lithos-loom project import path/to/doc.md --dry-run

# Just add tasks to an existing project (no doc create)
lithos-loom project import new-tasks.md --tasks-only --slug existing-project

# Bring an existing project to a clean state and re-import
lithos-loom project import doc.md --tasks-only --slug X --force-tasks --yes

# Doc-only: import the body verbatim, skip task extraction
lithos-loom project import doc.md --no-tasks
```

---

## What gets created

For each `- [ ]` open-task line in the source body, a Lithos task is created with:

| Lithos field | Source |
|---|---|
| `title` | the line text after stripping `- [ ]`, tags, priority emojis, `[sequential]` marker |
| `tags` | the line's `#foo` tags + auto-added `#project/<slug>` |
| `metadata.project` | the importing project's slug |
| `metadata.priority` | mapped from priority emoji if present |
| `metadata.depends_on` | child task ids when this line had indented children below it |
| `metadata.parallelizable` | `true` when this line is a sibling under a non-`[sequential]` parent |

The matched task lines are **stripped from the persisted doc body** — the project-context doc holds narrative only; tasks live as Lithos task entities (single source of truth).

`- [x]`, `- [/]`, `- [-]`, `- [>]` and any other markers stay verbatim in the body — only `- [ ]` open tasks are extracted.

---

## Two modes

### Greenfield (default)

Creates the project doc **and** the tasks. Refuses if a project at the slug already exists (the error message suggests `--tasks-only --slug X` as the alternative).

```bash
lithos-loom project import /path/to/source.md
```

### `--tasks-only`

Skips doc creation; just imports tasks against an existing project. **Requires `--slug`**. Refuses if the project doesn't exist (suggests typo matches when edit-distance ≤ 2) or already has tasks (unless `--force-tasks` is passed).

```bash
lithos-loom project import /path/to/new-tasks.md --tasks-only --slug my-project
```

`--tasks-only` ignores frontmatter `title` and any `tags:` for project routing — a safety check to prevent silent mis-routing. If the source file's frontmatter has a `lithos_id` AND it doesn't resolve to the same project as `--slug`, the import is refused.

### `--no-tasks` (escape hatch)

Skips task extraction entirely; imports only the doc body. Mutually exclusive with `--tasks-only`.

```bash
lithos-loom project import /path/to/doc.md --no-tasks
```

---

## Task extraction rules

### Line filter

Only `- [ ]` at line start (after optional leading whitespace) counts as a task. **Not parsed:**

- `* [ ]` or `+ [ ]` (only `-` is supported, per the canonical Obsidian convention)
- `- [ ]` inside fenced code blocks (` ``` ` and `~~~`)
- `- [ ]` inside blockquotes (`> ...`)
- mid-sentence `- [ ]` in prose

This restriction means the parser won't accidentally pick up example task lines you wrote in code samples or quoted material.

### Tags

Tags matching `#[A-Za-z0-9_/-]+` after a whitespace boundary are extracted from each line. All-digit tags (`#123`) are NOT parsed — they stay as literal text in the task description (so issue references like `#42` are preserved).

`#project/<other-slug>` (different from the importing project) triggers a refusal with the line number — see "Validation" below.

`#project/<importing-slug>` is silently consumed (it's auto-added anyway).

### Priority emojis

Mapped to `metadata.priority`:

| Emoji | Value |
|---|---|
| 🔺 | `highest` |
| ⏫ | `high` |
| 🔼 | `medium` |
| 🔽 | `low` |
| ⏬ | `lowest` |

Multiple priority emojis on one line: highest precedence wins; all are stripped from the description.

### `[sequential]` marker

Append the literal token `[sequential]` to a parent task's description to flip its children from parallel (default) to a sequential chain — `child[i].depends_on = child[i-1]`.

```markdown
- [ ] Implement [sequential]
  - [ ] Step 1
  - [ ] Step 2     ← depends on Step 1
  - [ ] Step 3     ← depends on Step 2
```

The marker is case-sensitive (lowercase `s`), must be a standalone token (won't false-positive on prose like "sequential planning"), and only takes effect on tasks that have indented children.

### Hierarchy from indentation

- **Top-level tasks** (no indent) are flat: no `depends_on` between them. Doc ordering imposes no execution semantics.
- **Indented children** represent composition: parent gets `metadata.depends_on = [child_ids]`; children have NO `depends_on` back to the parent. Parent is marked complete manually after all children are done.
- **Sibling children** are parallelizable by default; `[sequential]` on the parent flips them to a chain.

Mixed indentation (tabs + spaces in the same doc) is supported — anything with a strictly-deeper leading-whitespace count than the previous line is treated as a child. Pure-tab and pure-spaces docs work intuitively; mixed indentation gives you what you wrote.

```markdown
- [ ] Feature A              ← top-level (flat with B)
  - [ ] A.1                  ← parallel child of A
  - [ ] A.2                  ← parallel child of A
- [ ] Feature B [sequential] ← top-level
  - [ ] B.1                  ← first in B's chain
  - [ ] B.2                  ← depends on B.1
```

---

## Slug derivation

Priority order:

1. `--slug` flag (explicit override).
2. Slugified frontmatter `title` (NOT prefix-stripped — frontmatter is explicit operator intent).
3. Slugified file stem, with a leading `project-` prefix stripped first.

The prefix-strip lets you keep filesystem-organisation prefixes:

```bash
# File: ~/Dropbox/.../projects/project-organising-myself.md
# No frontmatter title; no --slug
lithos-loom project import ./project-organising-myself.md
# → slug = "organising-myself" (not "project-organising-myself")
```

The strip applies only to the **default-from-stem** path. Override with `--slug project-foo` if your project is literally named "project-foo" (rare).

`--dry-run` flags when the strip fired:

```
WOULD CREATE project:
  slug=organising-myself (stripped leading 'project-' from stem 'project-organising-myself'; override with --slug)
```

---

## Validation (validate-all-then-abort)

The import refuses upfront — **before any Lithos writes** — when any of these occur. All problems are reported in one pass so you can fix everything in one edit cycle:

| Problem | Exit |
|---|---|
| Cross-project tag (`#project/<other-slug>`) on any line | 2 |
| Empty parent (parent task is just `- [ ]` with indented children below) | 2 |
| Invalid slug (doesn't match `^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$`) | 2 |

Greenfield-only refusals:

| Problem | Exit |
|---|---|
| Source frontmatter has `lithos_id` (re-importing a projected file would duplicate) | 2 |
| Slug already exists in Lithos | 1 (error suggests `--tasks-only --slug X`) |

`--tasks-only`-only refusals:

| Problem | Exit |
|---|---|
| `--slug` not provided (frontmatter is ignored for routing in tasks-only mode) | 2 |
| Project doesn't exist for slug | 1 (error suggests near-miss matches when edit-distance ≤ 2) |
| Source `lithos_id` resolves to a different project than `--slug` | 2 |
| Project already has tasks AND `--force-tasks` not passed | 1 (error suggests `--force-tasks`) |

---

## `--dry-run`

Prints the full plan and exits without any Lithos mutations. The same read-only pre-flight as a real run is performed first, so problems surface before you commit:

- **Greenfield**: a `note_list` against `projects/<slug>/` checks for slug collision. Exits 1 with the same "slug already exists; did you mean `--tasks-only --slug X`?" message a real run would give.
- **`--tasks-only`**: verifies the project exists (with typo-hint on miss), and that any `lithos_id` in frontmatter resolves to the same slug. Exits 1 (project missing) or 2 (lithos_id mismatch) on pre-flight failure.

If the pre-flight passes, the full plan is printed. No `note_write` / `task_create` / `task_cancel` calls are made under any code path.

Output is framed with **`NO CHANGES MADE — re-run without --dry-run to apply`** at the start AND end so it can't be mistaken for a success log.

```
NO CHANGES MADE — re-run without --dry-run to apply

WOULD CREATE project:
  slug=organising-myself
  title=Organising Myself
  tags=['project-context', 'productivity']
  body=<284 chars after stripping 9 task lines>

WOULD CREATE 9 tasks (top-level: flat; nested: depends_on parent):
  1. "Set up GTD inbox" #productivity #project/organising-myself priority=high
  2. "Triage weekly review" #productivity #weekly #project/organising-myself priority=medium
  3. "Implement next-actions list" #project/organising-myself
    3a. "Define context tags" #project/organising-myself parallelizable=true
    3b. "Build weekly review template" #project/organising-myself parallelizable=true
  ...

NO CHANGES MADE — re-run without --dry-run to apply
```

The preview reflects the **final** state including auto-added `#project/<slug>` routing tags. What you see is what gets written.

---

## `--force-tasks`

Cancels every **open** task for the project before importing fresh.

- **Completed and cancelled tasks remain as history.** Lithos has no hard-delete primitive (E5), and re-cancelling a completed task would rewrite history from "done" to "cancelled" — wrong. The new import creates a fresh open set alongside the preserved record.
- **Gated by interactive y/N confirm.** Default is no on bare Enter. Use `--yes` to bypass for scripted use.

```bash
$ lithos-loom project import doc.md --tasks-only --slug existing --force-tasks
Cancel 4 open tasks (8 resolved tasks will remain as history) and create 12 new ones? [y/N]: y
```

The prompt always shows both counts so you know exactly what's being destroyed vs preserved.

---

## Partial-failure recovery (E1)

If Lithos fails mid-batch (e.g. network blip after N of M tasks are created), the import:

1. Stops creating tasks.
2. Posts a `[Friction]` finding attached to the **first** successfully-created task. Summary includes the recovery command, project slug, and `N/M` count.
3. Exits non-zero with an operator-facing message that surfaces the recovery command.

```
lithos-loom: partial import — created 4/10 tasks before failure (LithosClientError: network ...).
A [Friction] finding has been posted with the recovery command; re-run with
--tasks-only --slug X --force-tasks to complete.
```

The recovery command uses existing flag composition — `--tasks-only --slug X --force-tasks` will cancel the partial set and re-import fresh, no new mechanics needed.

If zero tasks were created before the failure (failure happened during doc-create or the very first task_create), the breadcrumb is logged as a `[Friction]` WARNING instead — no task exists to attach the finding to.

---

## All flags

| Flag | Default | Purpose |
|---|---|---|
| `<source>` (positional) | required | Path to the local Markdown file. |
| `--slug`, `-s` | derived | Project slug. Greenfield: optional (see "Slug derivation" above). `--tasks-only`: required. |
| `--tags` | none | Comma-separated extra tags for the project doc (greenfield only; ignored with `--tasks-only`). Union'd with frontmatter `tags:` + `project-context`. |
| `--tasks-only` | false | Skip doc creation; just import tasks against an existing project. Requires `--slug`. |
| `--no-tasks` | false | Skip task extraction entirely. Mutually exclusive with `--tasks-only`. |
| `--force-tasks` | false | Cancel open tasks for this project before importing. Gated by interactive y/N unless `--yes` is passed. |
| `--yes`, `-y` | false | Suppress the `--force-tasks` confirmation prompt. Silently a no-op without `--force-tasks`. |
| `--dry-run` | false | Print the full plan and exit; no Lithos writes (only read-only pre-flight call). |
| `--format`, `-f` | `text` | `text` (vault path on stdout) or `json` (`{id, slug, vault_path, tasks_created}`). |
| `--config`, `-c` | `$LITHOS_LOOM_CONFIG` | Explicit TOML config path. |

### Mutually-exclusive flag combinations

| Combo | Result |
|---|---|
| `--no-tasks` + `--tasks-only` | exit 2 |
| `--no-tasks` + `--force-tasks` | exit 2 (force-tasks needs task extraction) |
| `--tasks-only` without `--slug` | exit 2 |

---

## Exit codes

Mirror `project create`:

| Code | Meaning |
|---|---|
| `0` | Success (or clean user abort at `--force-tasks` prompt). |
| `1` | Lithos call failure, slug-collision refusal, missing-project refusal, partial-import failure, config load failure. |
| `2` | Input validation failure (mutually-exclusive flags, invalid slug, malformed task lines, cross-project tag, empty parent, `lithos_id`/`--slug` mismatch, unreadable source). |

---

## Worked examples

### Onboarding an existing Obsidian project doc

You have `~/Dropbox/obsidian/dave/projects/project-website-redesign.md` with 14 open `- [ ]` lines.

```bash
# Preview first.
lithos-loom project import ~/Dropbox/obsidian/dave/projects/project-website-redesign.md --dry-run

# Looks good — go.
lithos-loom project import ~/Dropbox/obsidian/dave/projects/project-website-redesign.md
# → /home/dns/Dropbox/obsidian/dave/_lithos/projects/website-redesign/website-redesign-project-context.md
```

The new project lands as `website-redesign` (the leading `project-` prefix is stripped), the 14 tasks are created with `metadata.project = "website-redesign"` and `#project/website-redesign` tag, and the projected doc + tasks appear in your vault's `_lithos/` tree within ~250ms.

### Recovering from a partial-import failure

The first attempt failed at task 8 of 14 with a network blip. Lithos has:
- A new project doc (`website-redesign`).
- 7 tasks created.
- A `[Friction]` finding attached to the first task with the recovery command.

```bash
# Wipe the 7 partial tasks (8 resolved will remain as history) and re-import.
lithos-loom project import ~/Dropbox/.../project-website-redesign.md \
  --tasks-only --slug website-redesign --force-tasks --yes
```

Resolved-task history from previous successful imports is preserved; only the partial set is cancelled.

### Adding tasks to a project you created manually

You ran `lithos-loom project create --title "Phase 2"` to set up the project context, then drafted the task list in a separate `phase-2-tasks.md` scratch file.

```bash
lithos-loom project import phase-2-tasks.md --tasks-only --slug phase-2
```

No doc write happens; the file's `phase-2-tasks` stem is irrelevant — `--slug phase-2` is what matters.

### Importing just the doc body, no tasks

Some PRDs have prose `- [ ]` checklists that aren't really actionable tasks (success criteria, acceptance gates, etc.). For those:

```bash
lithos-loom project import discovery-notes.md --no-tasks
```

Body is persisted verbatim, including the `- [ ]` lines.

### Scripted usage

```bash
# Capture the new task count for downstream automation.
result=$(lithos-loom project import doc.md --format json)
echo "$result" | jq '.tasks_created'  # → 12
```

---

## See also

- [`docs/SPECIFICATION.md`](../SPECIFICATION.md) — the operator + integrator spec; §4.8 covers this command in summary, §7 covers projection and bidirectional sync.
- [`docs/prd/archive/bulk-task-import.md`](../prd/archive/bulk-task-import.md) — original design decision table.
- [`docs/macros/README.md`](../macros/README.md) — the Templater macros for `create-project` and `capture-task`. The `project import` CLI is intentionally not surfaced as a macro (it's a one-shot adoption tool, not a recurring capture flow).
- [`docs/prd/archive/task-archive.md`](../prd/archive/task-archive.md) — companion feature for migrating completed-task history.
