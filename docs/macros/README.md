# Lithos-loom Obsidian macros

User-installed Templater macros that complete the Obsidian-side capture flows for `lithos-loom`. Each macro lives in this folder as a `.md` file containing only a Templater `<%* %>` execution block — copy the file verbatim into your vault's Templater Template Folder, no editing required.

| Macro | Purpose |
|-------|---------|
| [`capture-task.md`](capture-task.md) | Single-modal form to create a new Lithos task from inside Obsidian. Inserts a wikilink to the projected line at cursor. |
| [`create-project.md`](create-project.md) | Single-modal form to create a new Lithos project-context doc. Inserts a wikilink to the projected vault file at cursor. |

---

# `capture-task.md` — capture Lithos task macro

Creates a new Lithos task from a one-dialog form, then inserts a wikilink at cursor pointing at the canonical projected line in `_lithos/tasks.md`.

## What it does

1. Calls `lithos-loom project list --format json` to populate the project dropdown.
2. Calls `lithos-loom obsidian-sync show --format json` to discover the configured `tasks_file` path (defaults to `_lithos/tasks.md` but is operator-configurable per `[obsidian_sync].tasks_file`).
3. Opens an Obsidian Modal with all six fields visible at once: project, title (defaulted to your current selection), brief, scheduled date (native HTML5 date picker — click the calendar icon or type), priority, tags.
4. On submit, shells out to `lithos-loom task create --no-insert ...` which creates the task in Lithos and returns just the task_id.
5. Inserts a wikilink at cursor pointing at the configured tasks_file:
   ```
   [[<tasks_file>|<title>]] 🆔 lithos:<id>
   ```
   So a default-config vault gets `[[_lithos/tasks.md|...]]`; a host with `tasks_file = "_inbox/lithos.md"` gets `[[_inbox/lithos.md|...]]`.
6. The daemon's `obsidian-projection` subscription receives the `task.created` event from Lithos and writes the canonical Tasks-plugin line into the same `tasks_file` independently — within ~250ms.

## Why a wikilink, not the task line itself?

The intuitive "insert the task line at cursor" model has a fatal architectural flaw: the daemon's projection already places the canonical line in the configured `tasks_file`. If the macro ALSO inserts the line at cursor, you end up with the same task in two places — and only the projection's copy gets updates. Worse, if both files match a Tasks-plugin daily query, the task renders **twice** in the daily view.

The wikilink shape sidesteps that:
- **One source of truth** for the actionable task: the configured `tasks_file`, managed by the daemon. Tick/cancel/priority/due-date edits there flow back to Lithos via the `obsidian-status-transition` / `-priority-changed` / `-due-date-changed` handlers.
- **Capture context preserved** in the current note: a clickable reference that says "I had this thought while writing about X". Doesn't double-render in Tasks queries (it's not a `- [ ]` line).
- **Greppable**: the trailing `🆔 lithos:<id>` is searchable from anywhere in the vault if you forget where you captured it.
- **Config-aware**: the wikilink target is resolved at macro fire time from `lithos-loom obsidian-sync show`, so hosts that customise `[obsidian_sync].tasks_file` get a working link without editing the macro.

To navigate from the wikilink to the actual task line: click → `tasks_file` opens → `Ctrl-F` the task id.

## Prerequisites

- The `lithos-loom` daemon is running. Without it the task is created in Lithos but the canonical line never lands in `_lithos/tasks.md`, so the wikilink dangles. Verify with `pgrep -af 'lithos_loom\.children'` (expect at least two children: route-runner + obsidian-sync).
- The `lithos-loom` binary is on **Obsidian's** launcher PATH (not just your shell's). Obsidian Desktop inherits PATH from the launcher session, not your `~/.bashrc` / `~/.zshrc`. Verify via Obsidian Developer Console (`Ctrl-Shift-I` → Console tab):
  ```javascript
  require("child_process").execSync("which lithos-loom").toString()
  ```
  If that errors, see the project README's "CLI on PATH for the capture macro" section.
- The [Templater](https://github.com/SilentVoid13/Templater) community plugin is installed and enabled.
- A `[projects.<slug>]` table exists in your `lithos-loom.toml` for at least one project (otherwise the project dropdown is empty and the macro exits early).

## Install

This macro is a Templater **template** (it uses the `<%* ... %>` execution block + `tp.*` template helpers), so it lives in the Template Folder — not the User Script Functions Folder (that one is for plain `.js` files exported as `tp.user.<name>` functions; see the [Templater script-user-functions docs](https://silentvoid13.github.io/Templater/user-functions/script-user-functions.html)).

1. **Pick a template folder** in your vault if you don't already have one (e.g. `_meta/templates/`). Tell Templater about it: `Settings → Templater → Template Folder Location` and set it to that folder. (See the [Templater settings docs](https://silentvoid13.github.io/Templater/settings.html).)
2. **Copy `capture-task.md` into your template folder.** No editing — the file contains only the Templater execution block and is meant to be copied verbatim:
   ```bash
   cp /path/to/lithos-loom/docs/macros/capture-task.md <vault>/_meta/templates/
   ```
3. **Register the template with Templater:** `Settings → Templater → Template Hotkeys` → "Add new hotkey for template" → pick `capture-task.md`.
4. **Bind your hotkey** via `Settings → Hotkeys` (the standard Obsidian Hotkeys pane). Search for `capture-task`. You will see **two** auto-generated commands — Templater registers both whenever you add a template:

   | Command | What it does |
   |---|---|
   | `Templater: Create capture-task` | Creates a new "Untitled" note from the template. **NOT what we want** — this produces an empty new note instead of inserting at the cursor of your current note. |
   | `Templater: Insert capture-task` | Inserts the template at cursor in the active markdown file. **Bind your hotkey to this one** (e.g. `Alt+T`). |

   Leave `Templater: Create capture-task` unbound.

5. **Sanity check:** open any markdown note, place cursor in a paragraph, fire your hotkey. The modal should appear immediately with the project dropdown defaulted to the first project. If nothing happens, confirm Template Folder Location is set, and `capture-task.md` appears in `Settings → Templater → Template Hotkeys`.

## Behaviour notes

- **Selection-as-title**: if you've highlighted text before invoking the macro, that text is the default title. One hotkey turns a phrase into a task.
- **Modal interactions**: Tab through fields; `Enter` while focused on the Title field submits the form; `Esc` cancels (nothing is created or inserted).
- **Title is required**: submitting with an empty title shows a Notice and keeps the modal open.
- **Empty optional fields are omitted**: leave brief/scheduled/priority/tags blank and the corresponding CLI flag isn't passed; Lithos persists no value for that field.
- **Tags**: passed as comma-separated; the CLI strips whitespace and drops empty entries. So `"foo, , bar"` becomes `["foo", "bar"]`.
- **Errors**: any non-zero exit from `lithos-loom` (unknown project, network failure, Lithos validation envelope) surfaces in a 10-second Notice popup with the stderr message. The macro returns without inserting anything.
- **Lithos availability**: the macro doesn't pre-check Lithos health. If Lithos is unreachable, the `task create` invocation surfaces the connection error directly. Run `lithos-loom doctor` first if the macro is failing silently.

## CLI flags reference (for non-macro flows)

The macro uses `--no-insert` exclusively because that pairs cleanly with the wikilink-at-cursor model. The CLI ships two other output modes for non-macro callers — shell scripts, manual operator use, automation:

### Default: print the projected line to stdout

```bash
lithos-loom task create --project X --title Y
# → - [ ] Y 🆔 lithos:<id> #project/X
```

Useful if you want to redirect or pipe the line elsewhere.

### `--target-file PATH`

Appends the projected line to `PATH` instead of printing it. The file is created (with parent dirs) if missing.

```bash
lithos-loom task create --project X --title Y --target-file ~/inbox.md
```

Useful for "create a task and put the line in next week's daily note" flows.

### `--no-insert`

What the macro uses. Creates the task and prints just the task_id to stdout; the projected line is discarded.

```bash
task_id=$(lithos-loom task create --project X --title Y --no-insert)
echo "created $task_id"
```

`--target-file` and `--no-insert` are mutually exclusive — passing both is a usage error (exit 2).

---

# `create-project.md` — create Lithos project macro

Creates a new Lithos project-context doc from a one-dialog form, then inserts a wikilink at cursor pointing at the projected vault file.

## What it does

1. Calls `lithos-loom obsidian-sync show --format json` to discover the vault root + `projects_dir` so the inserted wikilink resolves to the projected file the daemon will write.
2. Opens an Obsidian Modal with title + slug + tags + description. The slug field auto-derives from the title (lowercased, alphanumeric-only, hyphens for separators) and re-derives on every title keystroke until the operator edits the slug manually — at which point auto-tracking stops.
3. Validates the slug client-side against `^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$` for instant feedback before any shell-out.
4. Writes the description to a temp file (so multiline content doesn't go through the shell) and shells out to `lithos-loom project create --format json --body-file <tmpfile> ...`.
5. Parses the JSON `{id, slug, vault_path}` response and inserts a wikilink at cursor pointing at the projected vault file:
   ```
   [[_lithos/projects/<slug>/<slug>-project-context|<title>]]
   ```
6. The daemon's `project-context-projection` subscription receives the `note.created` event from Lithos and writes the canonical Markdown into the same vault path independently — within ~250ms.

## Why a wikilink, not the doc content itself?

Same logic as `capture-task` — the daemon's projection is the single source of truth for the vault file. Inserting the content at cursor would create a stale duplicate that doesn't track Lithos updates. The wikilink gives a clickable navigation handle to the canonical doc; clicking opens the projected file, which is editable in Obsidian and pushed back via the `note-push` handler.

## Prerequisites

- The `lithos-loom` daemon is running with both `obsidian-projection` AND `project-context-projection` subscriptions configured. Without the latter, the new doc is created in Lithos but never projected into the vault — the inserted wikilink dangles.
- The `lithos-loom` binary is on **Obsidian's** launcher PATH (same gotcha as `capture-task`).
- The [Templater](https://github.com/SilentVoid13/Templater) community plugin is installed and enabled.

## Install

Same shape as `capture-task`:

1. Copy `create-project.md` into your Templater Template Folder:
   ```bash
   cp /path/to/lithos-loom/docs/macros/create-project.md <vault>/_meta/templates/
   ```
2. Register the template with Templater (`Settings → Templater → Template Hotkeys`).
3. Bind your hotkey via `Settings → Hotkeys` to **`Templater: Insert create-project`** (not the `Create` variant).

## Behaviour notes

- **Selection-as-title**: highlighting text before invoking the macro pre-fills the title.
- **Slug auto-tracking**: the slug field updates as you type the title — until you click into the slug field and edit it. After that, the slug stays put.
- **Title is required**; slug must match the regex above (the modal won't submit otherwise).
- **`project-context` tag** is added automatically by the CLI; operator-supplied tags are merged with no duplicates.
- **Errors** (slug collision, invalid input, network failure) surface in a 10-second Notice popup with the stderr message. The macro returns without inserting anything; the tmpfile is cleaned up in either case.

## CLI flag reference

The macro uses `--format json --body-file <tmpfile>` exclusively. For non-macro flows:

```bash
# Simple case: title only (body left empty, fill in via Obsidian).
lithos-loom project create --title "My New Project"

# With body inline:
lithos-loom project create --title "My Project" --body "One-line description"

# With body from file:
lithos-loom project create --title "My Project" --body-file ./README.md

# JSON output (for scripted callers):
lithos-loom project create --title "..." --format json
# → {"id": "...", "slug": "...", "vault_path": "..."}
```

## `project import` (companion CLI, not exposed as a macro)

For importing an existing local Markdown file as a Lithos project — and extracting its `- [ ]` lines as real Lithos tasks (greenfield doc + tasks, `--tasks-only`, `--force-tasks`, `--dry-run`, mid-batch recovery):

**See the full reference at [`docs/cli/project-import.md`](../cli/project-import.md).**

```bash
# Most common: greenfield import of an existing Obsidian project doc.
lithos-loom project import /path/to/existing.md

# Preview without writing.
lithos-loom project import /path/to/existing.md --dry-run
```

Intentionally not exposed as a Templater macro — this is a one-shot adoption tool, not a recurring capture flow.
