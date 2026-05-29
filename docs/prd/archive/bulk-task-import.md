---
title: Lithos Loom — Bulk Task Import on `project import`
milestone: Track 1 (Slice 5 enhancement)
status: draft
target_version: 0.3.0
references:
  - src/lithos_loom/cli/project.py (existing project import / create)
  - docs/prd/task-archive.md (Slice 6 — companion feature for completed tasks)
  - docs/prd/capture-macro-tag-parsing.md (Slice 3.1 — shares the tag-regex contract)
  - docs/PLAN.md (Slice 5 build order)
labels: [needs-triage, lithos-loom, obsidian, project-import, tasks]
---

# Lithos Loom — Bulk Task Import on `project import`

## Problem Statement

`project import` (Slice 5) creates a Lithos project-context doc from a local Markdown file. The doc body is persisted verbatim — including any `- [ ]` task lines the operator already wrote in the source — but those task lines are **just text in a doc**. They are not Lithos task entities. Consequences:

1. Adopting an existing Obsidian project doc (e.g. `~/Dropbox/obsidian/dave/projects/project-organising-myself.md`) requires two operator steps: (a) `project import` to create the doc, then (b) re-typing every `- [ ]` line into the capture macro one at a time to file them as Lithos tasks. Tedious for projects with 20+ open tasks.
2. The task lines that remain in the doc body are visible to the Tasks-plugin query that surfaces `_lithos/tasks.md`, but they aren't claimable, status-trackable, or projection-managed. They live in a different ownership domain than the Lithos task entities, which is confusing.
3. The `#project/<slug>` tag-routing intent the operator wrote in their source file is silently lost — the import doesn't lift it, the macro doesn't see it because the operator never highlights and re-captures.

For the user's "I have ~12 existing project docs in Obsidian with task lists; I want to onboard them to Lithos" workflow, this is the dominant friction.

## Solution

Extend `project import` to extract `- [ ]` open task lines from the source body, parse a defensive subset of Tasks-plugin metadata (tags + priority + auto-`#project/<slug>`), and create Lithos task entities for each one with dependency edges derived from indentation. The extracted lines are stripped from the doc body (single source of truth: tasks live as Lithos task entities, doc holds narrative context only). Two import modes — **greenfield** (create project + tasks) and **tasks-only** (`--tasks-only`, just create tasks against an existing project) — handle both the cold-start adoption flow and the incremental "add tasks to a project I already created manually" flow.

The whole operation is validate-all-then-abort: if any task line is malformed, any `#project/<other>` cross-project tag appears, or any parent task is empty, the entire import is refused with a complete error report before any Lithos writes happen. `--dry-run` previews the full plan without touching Lithos. `--force-tasks` deletes existing project tasks before re-importing (gated by interactive y/n; `--yes` to suppress).

### Flow (greenfield)

```
operator: cp ~/Dropbox/obsidian/dave/projects/organising-myself.md /tmp/import-source.md
operator: uv run lithos-loom project import /tmp/import-source.md --dry-run

  NO CHANGES MADE — re-run without --dry-run to apply

  WOULD CREATE project:
    slug=organising-myself
    title=Organising Myself
    tags=[project-context, productivity]
    body=<284 chars after stripping 9 task lines>

  WOULD CREATE 9 tasks (top-level: flat; nested: depends_on parent):
    1. "Set up GTD inbox" #productivity priority=high
    2. "Triage weekly review" #productivity #weekly priority=medium
    3. "Implement next-actions list"
         3a. "Define context tags" parallelizable=true  (depends_on=#3)
         3b. "Build weekly review template" parallelizable=true  (depends_on=#3)
    4. "Migrate from todoist"
    ...

  NO CHANGES MADE — re-run without --dry-run to apply

operator: uv run lithos-loom project import /tmp/import-source.md

  ✓ Project created: organising-myself (id=abc-123)
  ✓ Created 9 Lithos tasks
  → projected at /home/dns/.../vault/_lithos/projects/organising-myself/organising-myself.md
```

### Flow (tasks-only against existing project)

```
operator: uv run lithos-loom project import /tmp/new-tasks.md \
            --tasks-only --slug existing-project --dry-run

  NO CHANGES MADE — re-run without --dry-run to apply

  Project: existing-project (existing) — doc unchanged
  WOULD CREATE 4 tasks:
    1. "Add OAuth provider config" #auth priority=high
    2. "Write integration tests"
    ...

  NO CHANGES MADE — re-run without --dry-run to apply

operator: uv run lithos-loom project import /tmp/new-tasks.md \
            --tasks-only --slug existing-project

  ✓ Created 4 Lithos tasks for existing-project
```

### Flow (mid-batch failure recovery)

```
operator: uv run lithos-loom project import /tmp/big-import.md

  ✓ Project created: big-import (id=xyz-789)
  Creating 30 tasks...
  ✓ 12/30
  ERROR: Lithos returned connection_reset on task 13 of 30

  [Friction] finding posted to project doc xyz-789:
    State: doc created (slug=big-import); 12 of 30 tasks created
    Recovery: re-run as
      uv run lithos-loom project import /tmp/big-import.md \
        --tasks-only --slug big-import --force-tasks

operator: uv run lithos-loom project import /tmp/big-import.md \
            --tasks-only --slug big-import --force-tasks

  Deleting 12 existing tasks for big-import...  [y/N] y
  ✓ Deleted 12 tasks
  ✓ Created 30 tasks
```

## Locked Design Decisions

| # | Area | Decision |
|---|------|----------|
| D56 | Two-mode design | Greenfield (no flag) creates doc + tasks; tasks-only (`--tasks-only`) skips doc creation, just creates tasks against an existing project. Modes are explicit via the flag; slug-collision semantics are symmetric: greenfield refuses if slug exists, tasks-only refuses if slug doesn't exist |
| D57 | Default ON | Task extraction is the headline feature. Operators who want doc-only import pass `--no-tasks` to opt out |
| D58 | Status scope | Only `[ ]` open tasks are extracted. Other markers (`[x]`, `[/]`, `[-]`, `[>]`) stay verbatim in the doc body as historical / contextual content. Importing completed tasks is task-archive's job (Slice 6 PRD), not import's |
| D59 | Body after extraction | Extracted `[ ]` lines are stripped from the doc body before persisting. Clean separation: project-context doc holds narrative context; tasks live as Lithos task entities (single source of truth). Tasks-plugin queries against `_lithos/tasks.md` still surface them |
| D60 | Re-import refused; `--force-tasks` is the escape | By default, re-importing a file refuses (greenfield: slug exists; tasks-only: tasks for project exist). `--force-tasks` flips the refusal into "delete existing project tasks, then create fresh." Force gate requires interactive y/n confirmation; `--yes` suppresses for scripting |
| D61 | Metadata parsed per line | Tags (`#[A-Za-z0-9_/-]+` excluding all-digit per [[capture-macro-tag-parsing]] D40), priority emojis (⏫🔺🔼🔽⏬ → high/highest/medium/low/lowest), and an auto-added `#project/<slug>` tag if the line doesn't already carry one. Extended Tasks-plugin metadata (⏳ 🛫 ➕ ✅ 🔁 📅) is out of scope this PRD — most have no Lithos analogue today; defer until soak surfaces a need |
| D62 | Cross-project tag collision | Refuse the entire import. If any `[ ]` line carries `#project/<other-slug>` (different from the project being imported), the import aborts at validation time with a complete error report listing every offending line. Forces operator to either clean up the source file or use the capture macro to file the cross-project tasks individually |
| D63 | Top-level task hierarchy | Flat. Top-level `- [ ]` lines become independent Lithos tasks with no `depends_on` between them. Doc ordering carries no execution semantics |
| D64 | Indented children represent composition | "Shape A": parent is a real Lithos task with `metadata.depends_on = [child_ids]`. Children have no `depends_on` to the parent. Operator marks the parent complete manually after all children are done. The parent represents the integration / review / sign-off work; Lithos's claim mechanism naturally blocks parent claims until children complete (no agent can claim a task whose deps are unfulfilled) but does NOT auto-complete the parent |
| D65 | Sibling order within a parent group | Parallel by default. All siblings get `metadata.parallelizable = true` and no `depends_on` between them. Per-parent `[sequential]` marker (the literal string `[sequential]` appearing in the parent task's line) flips that parent's children to sequential (child N depends on child N-1) |
| D66 | Empty parent refused | If a parent task has indented children but its own description is empty after stripping the `- [ ]` prefix (reads as a heading), the import refuses with a pre-flight error listing the offending line numbers. Operator must either flesh out the parent description or remove the parent line (children become top-level / get a new parent) |
| D67 | Line filter | `- [ ]` at line start (after optional leading whitespace) only. Star (`*`) and plus (`+`) list markers are NOT parsed. Lines inside fenced code blocks (` ``` ` or `~~~`) and blockquotes (`>` prefix) are skipped. Mid-sentence `- [ ]` references in prose are ignored. Restricted scope matches the canonical Obsidian convention |
| D68 | Validate-all-then-abort | All parse / validation failures (malformed tag, cross-project tag, empty parent, etc.) are gathered in a single pass; if any fail, the import aborts with a complete error report listing every problem with line numbers. Operator fixes everything in one edit cycle then retries. No partial Lithos writes |
| D69 | Partial-failure recovery | No auto-rollback. If Lithos fails mid-batch (network blip after N tasks created), abort and emit a `[Friction]` finding on the project doc with the state breakdown (doc created Y/N, tasks created N/M) and a tailored recovery command using existing flags (e.g. `--tasks-only --slug X --force-tasks`). Operator-driven recovery; no new mechanics needed because the existing flag combinations compose to handle every case |
| D70 | Slug resolution | Greenfield: `--slug` is optional; defaults to slugified frontmatter `title` → file stem. Override permitted for operator-driven naming cleanup. Tasks-only: `--slug` is REQUIRED. Frontmatter is ignored for routing in tasks-only mode (explicit safety against silent mis-routing) |
| D71 | `lithos_id` ↔ `--slug` consistency check | In tasks-only mode, if the source file's frontmatter carries `lithos_id` AND `--slug` is provided AND the two do not resolve to the same project, refuse with a clear error. Catches "I copied frontmatter from doc A but ran with `--slug B`." In greenfield mode, `lithos_id` in frontmatter is still refused outright (existing behavior — projected files cannot be re-imported greenfield) |
| D72 | `--dry-run` preview | Prints the full plan (project doc body after stripping + extracted tasks with parsed metadata + dependency edges + sibling parallelism flags) and exits without Lithos writes. Only Lithos call is the slug-collision pre-flight (read-only). Output is framed with `NO CHANGES MADE — re-run without --dry-run to apply` at both start AND end so it can't be mistaken for a success log |
| D73 | Typo hint on "project not found" | On tasks-only "no project at slug=X" error, query existing Lithos projects (one extra `note_list` call on the error path; negligible cost) and suggest matches within edit distance 2. Standard CLI ergonomic; significantly reduces the wrong-slug footgun |
| D74 | Cross-mode hints in refusal | Every refusal message suggests the OTHER mode as the recovery path. Greenfield + slug-exists → "did you mean `--tasks-only --slug X`?". Tasks-only + slug-not-found → "use without `--tasks-only` to create a new project, or check the slug (suggestions: ...)". Operator's intent is recoverable in one re-run without consulting docs |
| D75 | Default slug strips leading `project-` from file stem | When deriving the default slug from the source file stem (greenfield mode, no `--slug`, no frontmatter `title`), strip a leading `project-` prefix (case-insensitive) before slugifying. The `project-` prefix is a common filesystem-organization convention (e.g. `~/.../projects/project-organising-myself.md`) which becomes redundant once docs live under `projects/<slug>/...`. **NOT** stripped from frontmatter `title` — that's explicit operator intent; respected as-is. When the strip fires, `--dry-run` output and the success output flag it explicitly: `slug=organising-myself (stripped leading "project-" from stem "project-organising-myself"; override with --slug)`. Override with `--slug <slug>` on the rare legitimate case (e.g. a project literally named "project-management" where the operator wants the prefix preserved) |

## Relationship to Existing Track 1 Architecture

This is a **`project import` enhancement** — same command, additional behavior. No new subscription, no new source, no new child process. Calls existing surface:

- `lithos_list(path_prefix=f"projects/{slug}/", limit=1)` — slug-collision pre-flight (already used by `project create`).
- `lithos_write(...)` — doc creation (already used). For greenfield mode only.
- `lithos_task_create(...)` — NEW call site for this PRD. Creates one Lithos task per extracted `[ ]` line. Existing tool, just not invoked from `project import` today.
- `lithos_task_list(project=slug)` — used in tasks-only mode for the pre-flight "any existing tasks?" check, and (in `--force-tasks` mode) to enumerate what to delete.
- `lithos_task_cancel` (or `_delete` — whichever Lithos exposes for hard-removal) — used by `--force-tasks` to wipe pre-existing tasks.
- `lithos_finding_post` — used in mid-batch failure to emit the `[Friction]` recovery breadcrumb.

No new event types, no new bus subscriptions. The new tasks land in Lithos and naturally flow through existing Slice 1/2 task-projection — the bidirectional sync (Slice 5) handles ongoing updates without changes.

## User Stories

One vertical slice. The CLI changes land in `src/lithos_loom/cli/project.py` (extending the existing `project_import` Typer command). New helper modules for line parsing and dependency graph construction land in `src/lithos_loom/`.

### Slice 5.1 — Bulk task import

#### Greenfield + tasks-only modes

76. As an operator, I want `project import <file>` to also create Lithos tasks from `- [ ]` lines in the file's body, so that adopting an existing Obsidian project doc bootstraps both the project AND its open work items in one command.

77. As an operator, I want `--no-tasks` to skip task extraction, so that I can import just the doc body when I don't want my freeform task list lifted into Lithos.

78. As an operator, I want `project import --tasks-only --slug <slug>` to skip doc creation and just import open tasks against an existing project, so that I can incrementally add tasks to projects I've already created in Lithos (manually or via other tools).

79. As an operator, I want `--tasks-only` to require the `--slug` flag explicitly (and ignore frontmatter for routing), so that I never silently file tasks against the wrong project due to ambiguous frontmatter.

80. As an operator, I want greenfield slug to be optional (defaulting to slugified frontmatter `title` → file stem, with `--slug` available as an override), so that I can clean up inconsistent naming as I migrate existing project docs.

100. As an operator with existing project files at `~/Dropbox/obsidian/dave/projects/project-organising-myself.md` (using the `project-` filename prefix as a folder-organization convention), I want default slug derivation to strip a leading `project-` prefix from the file stem (case-insensitive) — but NOT from frontmatter `title` — so that the resulting Lithos path doesn't carry triple-redundancy (`projects/project-organising-myself/project-organising-myself-project-context.md` becomes `projects/organising-myself/organising-myself-project-context.md`), with `--slug <slug>` available as an override for the rare legitimate "project-management" case and `--dry-run` showing the resolved slug before any writes.

#### Refusal semantics

81. As an operator, I want the importer to refuse a greenfield import if the slug already exists in Lithos, with an error message pointing to `--tasks-only` as the way to add tasks against the existing project, so that I don't accidentally try to recreate a project I already have.

82. As an operator, I want the importer to refuse a `--tasks-only` import if the named project doesn't exist in Lithos, with an error message suggesting near-miss slug matches (edit distance 2) and pointing to greenfield mode as the alternative, so that typos and wrong-mode invocations are caught before any tasks are created.

83. As an operator, I want the importer to refuse a `--tasks-only` import if the source file's `lithos_id` frontmatter doesn't match the project resolved by `--slug`, so that I'm prevented from filing tasks against the wrong project when I've copied frontmatter from a different source file.

#### Task extraction rules

84. As an operator, I want only `- [ ]` open task lines extracted as Lithos tasks (other markers like `[x]`, `[/]`, `[-]`, `[>]` stay verbatim in the doc body), so that completed and in-progress tasks remain visible in the projected doc as historical context rather than being silently migrated.

85. As an operator, I want extracted task lines stripped from the imported doc body, so that the project-context doc holds narrative context only and tasks are tracked in Lithos as the single source of truth (no dual ownership).

86. As an operator, I want the importer to skip `- [ ]` lines inside fenced code blocks and blockquotes, and only treat line-start `- [ ]` (after whitespace) as task lines, so that example task lines in code samples and quoted material aren't accidentally imported.

#### Metadata parsing

87. As an operator, I want the importer to parse tags (`#foo`) and priority emojis (⏫🔺🔼🔽⏬) from each task line and map them to Lithos task fields, so that the metadata I've already written in my Obsidian tasks survives the import.

88. As an operator, I want the `#project/<slug>` tag automatically added to every imported task (matching the importing project's slug) when not already present, so that imported tasks are always discoverable via the project-tag filter even if I forgot to add the tag myself in the source file.

89. As an operator, I want the importer to refuse the entire import if any task line carries a `#project/<other-slug>` tag (different from the project being imported), with a clear error listing every offending line, so that cross-project intent isn't silently overridden.

#### Hierarchy

90. As an operator, I want top-level `- [ ]` lines to be imported as independent Lithos tasks with no inter-task dependencies, so that doc ordering doesn't impose strict-sequential execution that I didn't intend.

91. As an operator, I want indented `- [ ]` lines under a parent task to be imported as a component-of relationship — parent gets `metadata.depends_on = [child_ids]`, parent marked complete manually after all children done — so that my project plans' nested structure is preserved as real Lithos dependencies.

92. As an operator, I want sibling children of a parent group to be parallelizable by default (`metadata.parallelizable = true`, no `depends_on` between siblings), so that components within a feature can be worked on in any order or concurrently.

93. As an operator, I want to mark a specific parent group as sequential by adding `[sequential]` after the parent's task text (e.g. `- [ ] Implement [sequential]`), so that I can override the parallel default for groups that genuinely need ordered execution.

94. As an operator, I want the importer to refuse an import if any parent task is empty (just `- [ ]` with no extra content — reads as a heading), with a pre-flight error listing the offending line numbers, so that I'm forced to either flesh out the parent description or remove the line rather than have the tool guess at my intent.

#### Validation and preview

95. As an operator, I want all validation errors (malformed lines, cross-project tags, empty parents, etc.) to be reported in a single pass and abort the whole import without making any Lithos writes, so that I can fix every problem in one edit cycle rather than iterating fix-retry-fix-retry.

96. As an operator, I want `--dry-run` to preview the full plan (doc body after stripping + extracted tasks with parsed metadata + dependency edges + parallelism flags) without making any Lithos writes, so that I can verify the import is doing what I expect before committing to it.

97. As an operator, I want `--dry-run` output to be unmistakably framed as a preview (prominent "NO CHANGES MADE" markers at start and end), so that I never confuse a preview with a successful import.

#### Force and recovery

98. As an operator, I want `--force-tasks` to delete all existing project tasks for the slug and import fresh, gated by an interactive y/n confirmation (suppressible with `--yes` for scripting), so that I can recover from partial imports without doing manual cleanup but can't accidentally trigger destruction.

99. As an operator, when an import fails mid-batch (Lithos network failure after some tasks have been created), I want a `[Friction]` finding posted with the state breakdown (doc created Y/N, tasks created N/M) and an explicit recovery command using existing flag combinations, so that I know exactly which re-run command to use to complete the import.

## Implementation Decisions

### New parser module: `src/lithos_loom/task_line_parser.py`

Pure functions, no I/O. Shared with the future capture-macro tag-parsing PRD (Slice 3.1) — the tag regex `#[A-Za-z0-9_/-]+` (excluding all-digit per D40) is identical. Extracted into a shared module so the two PRDs converge on one regex contract.

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ParsedTaskLine:
    line_number: int       # 1-indexed source-file line number for error messages
    indent: int            # leading-whitespace count, used for hierarchy
    description: str       # task text after stripping `- [ ]`, tags, priority emojis
    tags: tuple[str, ...]  # parsed `#foo` tags (no leading `#`), in source order, deduped
    priority: str | None   # "highest" | "high" | "medium" | "low" | "lowest" | None
    cross_project_tag: str | None  # `#project/<slug>` if present and != importing slug; None otherwise
    is_sequential_parent: bool  # parent has `[sequential]` marker in description
    is_empty: bool         # description is empty after all stripping

def parse_doc(text: str, importing_slug: str) -> tuple[list[ParsedTaskLine], list[ValidationError], str]:
    """Parse a Markdown doc into task lines, validation errors, and stripped body.

    Single pass over the text. Skips lines inside fenced code blocks (``` or ~~~)
    and blockquotes (`>` prefix). Only matches `- [ ]` at line start after optional
    leading whitespace. Returns:
      - parsed task lines (only those that matched the filter)
      - validation errors (malformed lines, cross-project tags, empty parents)
      - the doc body with all matched task lines stripped
    """
```

### New dependency-graph builder: `src/lithos_loom/task_graph.py`

Pure function. Takes `list[ParsedTaskLine]` and returns a list of `(line, depends_on_lines, parallelizable_bool)` tuples ready for `lithos_task_create`. Implements the indentation-driven parent-child rule (D64), the sibling parallelism default (D65), and the `[sequential]` marker override.

```python
@dataclass(frozen=True)
class TaskCreatePlan:
    line: ParsedTaskLine
    depends_on_line_numbers: tuple[int, ...]  # references resolved to task ids at execution time
    parallelizable: bool

def build_plan(lines: list[ParsedTaskLine]) -> tuple[list[TaskCreatePlan], list[ValidationError]]:
    """Build task-create plans with dependency edges from indentation.

    Per D63 top-level tasks are flat (no depends_on between them).
    Per D64 parent tasks get depends_on = children (via line_number; resolved
    to task ids when tasks are created and ids assigned).
    Per D65 siblings are parallelizable by default; `[sequential]` marker
    on the parent flips that group to sequential (child N depends on child N-1).
    Per D66 empty parents are flagged as validation errors.
    """
```

### CLI surface changes

`project_import` (existing Typer command in `src/lithos_loom/cli/project.py`) gains new flags:

```python
@project_app.command("import")
def project_import(
    source: Path = typer.Argument(...),
    slug: str | None = typer.Option(None, "--slug", "-s",
        help="Project slug. Optional in greenfield (defaults to slugified title/stem); "
             "REQUIRED with --tasks-only."),
    tags: str | None = typer.Option(None, "--tags",
        help="Extra comma-separated tags for the project doc (ignored with --no-tasks-only-N/A)."),
    tasks_only: bool = typer.Option(False, "--tasks-only",
        help="Skip project doc creation; just import tasks against an existing project. "
             "Requires --slug. Project must already exist in Lithos."),
    no_tasks: bool = typer.Option(False, "--no-tasks",
        help="Skip task extraction entirely; import only the project doc body. "
             "Mutually exclusive with --tasks-only."),
    force_tasks: bool = typer.Option(False, "--force-tasks",
        help="Delete all existing project tasks for this slug before importing. "
             "Gated by an interactive y/n prompt unless --yes is also passed."),
    yes: bool = typer.Option(False, "--yes", "-y",
        help="Suppress the --force-tasks interactive confirmation. For scripted use."),
    dry_run: bool = typer.Option(False, "--dry-run",
        help="Print the full plan (doc + tasks + dependency edges) and exit "
             "without writing to Lithos."),
    output_format: str = typer.Option(_FORMAT_TEXT, "--format", "-f"),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    ...
```

Mutually-exclusive flag combinations:

- `--no-tasks` + `--tasks-only` → exit 2 ("conflicting flags")
- `--no-tasks` + `--force-tasks` → exit 2 (force-tasks is meaningless without task extraction)
- `--yes` without `--force-tasks` → ignored silently (no-op; not an error)

Exit codes (mirror existing `project create`):

- `0` — success
- `1` — Lithos call / config-load failure / slug-collision (refusal)
- `2` — input validation failure (mutually-exclusive flags, malformed lines, empty parents, cross-project tags, lithos_id mismatch, etc.)

### Tests

New test files mirroring the existing CLI test pattern:

- `tests/test_task_line_parser.py` — parser unit tests. ~25 tests covering: line filter (code blocks, blockquotes, list markers), tag regex (Obsidian-compatible + all-digit exclusion), priority emoji mapping, `#project/<slug>` extraction (self vs other), `[sequential]` marker detection, empty-after-stripping detection, multi-line edge cases.
- `tests/test_task_graph.py` — graph builder unit tests. ~15 tests covering: flat top-level, single parent + children, deeply-nested children, `[sequential]` sibling override, empty parent error, mixed sequential/parallel groups in one doc.
- `tests/test_cli_project_import_bulk.py` — CLI integration tests (mocking LithosClient). ~30 tests covering: greenfield happy path, `--tasks-only` happy path, `--no-tasks` skips extraction, `--dry-run` preview output, `--force-tasks` interactive prompt (and `--yes` bypass), every refusal path (slug exists, slug doesn't exist, `lithos_id` mismatch, cross-project tag, empty parent, malformed validation report), partial-failure friction-finding emission with correct recovery command, exit codes.

All three test files keep the established AAA-pattern + descriptive-name conventions from `tests/test_cli_project_create.py`.

## Open Questions (Deferred)

1. **Extended Tasks-plugin metadata** (⏳ scheduled, 🛫 start, ➕ created, ✅ done, 🔁 recurrence, 📅 due). Most have no first-class Lithos analogue today. Could be stored in `metadata.<field>` for future support, but partial support is worse than no support (operator can't tell which fields will actually be honored by Lithos's task lifecycle). Revisit after soak: if operators routinely add 📅 dates to project plans and want them lifted, file a follow-up PRD scoped to that single field.

2. **Completed task harvesting (`[x]` lines).** Out of scope this PRD. Overlaps significantly with [[task-archive]] (Slice 6): if you want historical context preserved, that's the right home for it. The current decision keeps `[x]` lines verbatim in the doc body — they're visible there, not lost.

3. **Re-import with content drift detection.** When an operator edits the source file between imports (some task lines removed, others added, others reworded), the current model is "the import is a one-shot create; re-import is forbidden unless `--force-tasks`." A more ambitious model could: dedup-by-description, detect missing-from-source tasks and cancel them, detect changed-description tasks and update them. Significant complexity for an uncertain payoff (when do operators actually want this?). Defer until soak surfaces a clear need.

4. **`lithos-loom project delete <slug>` helper.** Several locked decisions assume the operator can clean up a project + its tasks atomically (e.g. to retry a botched greenfield import). Today that's two MCP calls (`lithos_delete` for the doc + manual `lithos_task_cancel` for each task). A helper command would be useful. Out of scope this PRD; file as a follow-up if soak surfaces the friction.

5. **Bulk task import on standalone files (not project docs).** This PRD scopes to `project import` specifically. Could there be a `lithos-loom task import <file> --project <slug>` for filing tasks from non-project-doc markdown? Same parser + same graph builder; different CLI command shape. Defer until a clear use case surfaces (probably "I have a standalone meeting-notes doc with action items").

6. **Markdown parser quality bar.** The line filter (D67) requires distinguishing fenced code blocks and blockquotes from regular task lines. Two implementation paths: (a) hand-rolled state machine (cheap, works for the 95% case, can mishandle pathological nesting); (b) full Markdown parser (e.g. `markdown-it-py` or `mistune`, ~50KB dep, correct on edge cases). Default to (a) for v1 — the parser only needs to distinguish three contexts (top-level, code-block, blockquote). Revisit if soak surfaces false positives on real operator docs.

## Verification

After implementation, hand-test against staging Lithos (`localhost:8766`) with a real Obsidian source file. Verification covers the 24 user stories in 12 test scenarios; each scenario corresponds to a row in the table below.

| # | Scenario | Setup | Verify |
|---|---|---|---|
| 1 | Greenfield happy path | Source file: 3 top-level `[ ]`, no metadata | Project created; 3 Lithos tasks created; doc body has the task lines stripped; vault projection appears within ~250ms |
| 2 | Greenfield with metadata | Source file with priority emojis + `#foo` tags + auto-add `#project/<slug>` | Tasks created with parsed `priority` field and `tags = [foo, project-<slug>]` |
| 3 | Indented children, parallel default | Source with 1 parent + 3 indented children, no `[sequential]` marker | Parent task created with `depends_on = [child_ids]`; each child has `parallelizable = true`, no `depends_on` between them |
| 4 | Sequential marker override | Source with `- [ ] Implement [sequential]` + 2 indented children | Children form a sequential chain (child 2 `depends_on` child 1); parent depends on both |
| 5 | Tasks-only happy path | Existing project; source file with 4 `[ ]` lines | No doc write; 4 tasks created against existing slug |
| 6 | Tasks-only requires `--slug` | `project import file --tasks-only` (no slug) | Exit 2 with "tasks-only requires --slug" |
| 7 | Greenfield + existing slug refusal | Source for slug `existing-x` | Exit 1 with "slug already exists; did you mean `--tasks-only --slug existing-x`?" |
| 8 | Tasks-only + missing slug refusal + typo hint | `--tasks-only --slug projetc-x` (typo) | Exit 1 with "no project at slug 'projetc-x'; did you mean: project-x?" |
| 9 | Cross-project tag refusal | Source file with `- [ ] foo #project/other-slug` | Exit 2; error lists every offending line; no Lithos writes |
| 10 | Empty parent refusal | Source with `- [ ]\n  - [ ] child` (empty parent) | Exit 2; error names the offending line numbers |
| 11 | `lithos_id`/`--slug` mismatch refusal | Tasks-only with frontmatter `lithos_id: A`, `--slug B` (different project) | Exit 2 with "lithos_id resolves to project X; --slug=B; refusing" |
| 12 | Dry-run preview | Greenfield `--dry-run` | Output starts AND ends with "NO CHANGES MADE"; no Lithos writes (verified via Lithos doc list before/after) |
| 13 | `--force-tasks` y/n gate | Tasks-only `--force-tasks` (no `--yes`) | Interactive prompt; "n" aborts (exit 0, no changes); "y" deletes + re-imports |
| 14 | `--force-tasks --yes` bypass | Tasks-only `--force-tasks --yes` | No prompt; deletes + re-imports silently |
| 15 | Validation aggregates errors | Source with cross-project tag AND empty parent AND `#123` (looks like task ref) in one doc | Single error report listing both real errors; `#123` (all-digit) is NOT flagged as a tag (per D61 / D40) |
| 16 | Mid-batch failure recovery | Inject network failure on task 5 of 10 (kill staging Lithos mid-call) | `[Friction]` finding posted to project doc with state ("doc created; 4 of 10 tasks created"); error message shows exact recovery command `--tasks-only --slug X --force-tasks` |
| 17 | Default slug strips `project-` from stem (D75) | Source file `project-foo.md` with no frontmatter `title`; greenfield mode, no `--slug` | Resolved slug = `foo`; stderr / dry-run output flags the strip with the override hint; explicit `--slug project-foo` overrides the strip; frontmatter `title: Project Foo` is NOT stripped (slug stays `project-foo`) |

## Risks

- **`lithos_task_cancel` semantics for `--force-tasks` cleanup.** The PRD assumes a way to hard-remove tasks. If `lithos_task_cancel` only marks tasks as cancelled (rather than deleting them), `--force-tasks` re-import will leave a trail of cancelled-but-not-deleted task entities for the slug. Worth confirming with Lithos's task tool surface before implementation; may need an upstream issue if no hard-delete path exists.

- **Markdown parser pathological cases.** The hand-rolled state machine (per Open Question 6) will probably mishandle obscure Markdown structures (nested fenced blocks, indented code blocks with `- [ ]` content, footnote-style references). Mitigate with a test corpus drawn from real operator project docs during soak; if false positives appear, upgrade to a real parser.

- **Operator surprise on first use of `--force-tasks`.** Even with the y/n gate, an operator confident from successful dry-runs may type "y" without reading the prompt carefully. The interactive prompt should include both counts ("Delete 12 existing tasks and create 30 new ones? [y/N]") and not default-to-yes on Enter. Documented mitigation; no eliminating the human-error angle entirely.

- **Auto-added `#project/<slug>` tag drift.** D61 adds `#project/<slug>` to every task that doesn't already carry one. If the operator subsequently renames the project (manually re-slugs in Lithos), the tag becomes stale. Lithos has no slug-renaming primitive today; this risk is inherited from the broader "slug is canonical" architecture. Not unique to this PRD.

- **Re-import flow assumes operator deletes the old doc first in greenfield-retry.** Per D69 / D74, recovery from a partial greenfield failure routes through `--tasks-only --force-tasks` (which preserves the partially-created doc) — NOT through re-running greenfield. If the operator instead deletes the doc and re-runs greenfield, that works too, but the recovery message doesn't suggest it. Documented behavior; alternative path costs more typing for less win. Worth confirming during soak that operators converge on the suggested path.

- **PRD scope vs implementation effort.** This is one of the larger Slice 5 enhancement PRDs (parser + graph builder + 7 new CLI flags + ~70 new tests). Estimated implementation effort: 600-900 LOC + tests, ~1 day of focused work. Worth confirming the operator wants this before Track 2 vs deferring further.
