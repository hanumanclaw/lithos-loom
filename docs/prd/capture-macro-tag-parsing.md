---
title: Lithos Loom — Capture Macro Selection-Tag Parsing
milestone: Track 1 (Slice 3 enhancement)
status: draft
target_version: 0.2.0
references:
  - docs/macros/capture-task.md (existing macro source)
  - docs/macros/README.md (operator-facing docs)
  - docs/SPECIFICATION.md (capture macro flow — §4.5 + Appendix A)
labels: [needs-triage, lithos-loom, obsidian, capture-macro]
---

# Lithos Loom — Capture Macro Selection-Tag Parsing

## Problem Statement

Slice 3's capture-task macro (`docs/macros/capture-task.md`) defaults the modal's Title field to the operator's current Obsidian selection. Tags are a separate free-text input the operator types after the modal opens. In practice, when the operator selects a phrase that already contains hashtags — e.g. `"Review staging deploy #urgent #project/lithos-loom"` — the workflow today is:

1. Highlight the phrase, fire the hotkey.
2. Modal opens with Title pre-filled including the `#foo` tokens.
3. Operator manually deletes the tags from the Title.
4. Operator re-types the tags into the Tags field, stripping the `#`.

Friction: one mouse-or-keyboard sweep, two manual transformations, both lossless (the `#foo` tokens carry intent that the macro can detect mechanically). The shipped task in Lithos either ends up with `#foo` literally in its title (bad — projects weird) or the operator does the manual cleanup every time (annoying).

## Solution

When the macro opens its modal and a selection is present, scan the selection for hashtags using an Obsidian-compatible regex, lift them out into the Tags field (pre-populated, still editable), and pre-fill the Title field with the stripped + whitespace-normalised remainder. Special-case `#project/<slug>` tokens: if `<slug>` matches a configured project, auto-select the Project dropdown and consume the token (don't also add it as a plain tag).

All transformations are visible to the operator in the modal before submit — they can override any decision the parser made.

### Flow

```
Operator highlights "Ship staging deploy #urgent #project/lithos-loom by Friday"
  └─> hotkey fires capture-task macro
      └─> macro parses selection (NEW):
          ├─> tag regex finds ["#urgent", "#project/lithos-loom"]
          ├─> `#project/lithos-loom` matches a TOML project slug → Project dropdown auto-selects
          │     "lithos-loom"; token CONSUMED (not added to Tags)
          ├─> `#urgent` becomes a plain tag → added to Tags field
          ├─> stripped first line → "Ship staging deploy by Friday" (double-space collapsed)
          └─> modal opens pre-filled:
                  Project:   [lithos-loom ▾]
                  Title:     Ship staging deploy by Friday
                  Tags:      urgent
                  (other fields empty as before)
      └─> operator submits → `lithos-loom task create --project lithos-loom --title "..." --tags urgent`
```

## Locked Design Decisions

| # | Area | Decision |
|---|------|----------|
| D40 | Tag syntax | Obsidian-compatible regex `#[A-Za-z0-9_/-]+`, EXCLUDING tokens that are all-digit after the `#` (so `#123` issue references don't mis-parse). Same rule Obsidian's own tag pane uses |
| D41 | Title after extraction | Stripped, whitespace-normalised (runs of whitespace → single space, edges trimmed). Operator sees the cleaned title pre-filled and can edit it before submit. Lithos task's title carries no `#foo` tokens so the projection renders cleanly |
| D42 | `#project/<slug>` handling | If a `#project/<slug>` token appears AND `<slug>` matches a configured TOML project: auto-select the Project dropdown to that slug, consume the token (don't also add it as a Lithos tag). If the slug doesn't match: treat as a plain tag, project dropdown defaults, no warning |
| D43 | Empty-title fallback | If stripping tags yields an empty / whitespace-only title, leave the Title field empty. The modal's existing `"Title is required"` submit guard catches the empty case so the operator MUST type a title before submitting. Explicit-about-it behaviour rather than papering over with a fallback to the verbatim selection |

## Relationship to Existing Track 1 Architecture

This is a **pure macro-side change**. No new CLI surface, no new subscription, no new sources, no Lithos-side dependencies. The macro continues to shell out to `lithos-loom task create` with the same flag shape; parsed tags go into the existing `--tags` flag, the auto-routed project goes into `--project`.

- `lithos-loom project list --format json` (existing) — already loaded by the macro at startup; provides the project-slug allowlist for D42's `#project/<slug>` matching.
- `lithos-loom task create` (existing) — no change. The CLI doesn't need to know whether tags came from the operator's keystrokes or the parser.

## User Stories

One vertical slice, embedded in the existing capture-task macro file. No new files.

### Slice 3.1 — Capture macro selection-tag parsing

47. As an operator, I want the capture macro to scan my highlighted text for `#tag`-style tokens before opening the modal, so that tags I'd otherwise have to retype manually are pre-populated.

48. As an operator, I want the parsed tags pre-filled in the modal's existing Tags field (comma-separated, no `#` prefix), unioned with whatever I type after the modal opens (no duplicates, parser-order preserved), so that I can review and edit the result before submitting.

49. As an operator, I want the Title field to show my selection with the `#tag` tokens stripped and whitespace normalised (collapsed runs, trimmed edges), so that the task title in Lithos and in the projected line doesn't carry verbatim `#foo` tokens that render weirdly.

50. As an operator, I want `#project/<slug>` tokens in my selection to auto-select the Project dropdown when `<slug>` matches a configured TOML project, and the token consumed (not double-added to Tags), so that explicit project intent in my selection routes cleanly without manual dropdown clicks.

51. As an operator, I want unrecognised `#project/<slug>` tokens (slug not in TOML) to fall through to the plain-tag handling — no warning, no refusal — so that I can freely use `#project/future-name` in notes before the project exists in TOML.

52. As an operator, I want multi-line selections to use only the first stripped line as the Title (with tags from any line lifted out), so that the Tasks-plugin-compatible task line stays single-line.

53. As an operator, I want `#123`-style issue-number references in my selection to NOT be parsed as tags (regex excludes all-digit-after-hash), so that GitHub-style references in my notes don't get inadvertently converted to Lithos tags.

54. As an operator, when the parser detects all selection content is tags (e.g. I highlighted `#urgent #lithos-loom` alone), I want the Title field to stay empty and the modal's existing `"Title is required"` guard to catch the empty submit, so that I'm forced to be explicit rather than have the macro guess at a title for me.

## Implementation Decisions

### Macro-side changes only

All work lands in `docs/macros/capture-task.md` (and the operator's vault copy after they re-install). No Python / loom-CLI changes.

```javascript
// New: pure parser helpers inside the macro <%* %> block

// Obsidian-compatible tag regex. Matches `#[A-Za-z0-9_/-]+` but
// requires at least one non-digit so #123 (issue references)
// doesn't match. The lookahead `(?=.*[A-Za-z_/-])` is the
// non-digit guard.
const TAG_RE = /#(?=[A-Za-z0-9_/-]*[A-Za-z_/-])[A-Za-z0-9_/-]+/g;

// Returns {title, tags, projectSlug} where:
// - title is the first line of the selection with tags stripped
//   and whitespace normalised
// - tags is an array of plain tag strings (no leading #), in
//   selection order, deduplicated
// - projectSlug is the matched project from a #project/<slug>
//   token if one matched the projects array; null otherwise
function parseSelection(selection, projects) {
  const allMatches = [...(selection || "").matchAll(TAG_RE)];
  const allTags = allMatches.map(m => m[0].slice(1)); // strip leading #

  // Find a #project/<slug> token that matches a configured project.
  let projectSlug = null;
  const consumed = new Set();
  for (let i = 0; i < allTags.length; i++) {
    const tag = allTags[i];
    if (tag.startsWith("project/")) {
      const candidate = tag.slice("project/".length);
      if (projects.includes(candidate)) {
        projectSlug = candidate;
        consumed.add(i);
        break; // first match wins
      }
    }
  }

  // Remaining tags: in order, dedup, with the consumed project tag removed.
  const seen = new Set();
  const tags = [];
  for (let i = 0; i < allTags.length; i++) {
    if (consumed.has(i)) continue;
    if (seen.has(allTags[i])) continue;
    seen.add(allTags[i]);
    tags.push(allTags[i]);
  }

  // Title = first line of selection, with tags stripped, whitespace
  // normalised, edges trimmed.
  const firstLine = (selection || "").split("\n")[0] || "";
  const stripped = firstLine.replace(TAG_RE, "").replace(/\s+/g, " ").trim();

  return { title: stripped, tags, projectSlug };
}
```

The existing modal-open path already calls `tp.file.selection()`. Pass that selection (plus the `projects` array already loaded from `lithos-loom project list --format json`) through `parseSelection` to get the pre-fill values for Title, Tags, and Project dropdown.

### Operator-typed tag merge

The Tags input's `.onChange` handler already captures operator keystrokes. To preserve the existing semantic, run a small union+dedup on submit (or, simpler, set the input's initial value to the parsed CSV and let the operator freely edit — operator-typed comma-separated values then become the authoritative source). The latter is simpler; the parser pre-fills, the operator owns the field thereafter.

### Modal display

The Project dropdown's `dd.setValue(projectSlug || projects[0])` call already exists; passing the parsed `projectSlug` (or falling back to first project) requires no UI change.

## Open Questions (Deferred)

1. **Tasks-plugin date markers in selection** (`📅 2026-05-24`). Same parsing impulse as tags — could auto-fill the Scheduled field. Defer until tag parsing has soaked; if operators ask for it, file a follow-up PRD. Scope creep risk.

2. **Tasks-plugin priority emoji in selection** (`⏫ 🔺 🔼 🔽 ⏬`). Same deferral. Could auto-select Priority dropdown. Wait for an actual ask.

3. **Tags inside code spans** (`` `#bash to run` ``). Real Obsidian doesn't treat code-spanned `#foo` as a tag; our naive regex would. Probably fine — operators rarely highlight content with inline code spans. Revisit if soak surfaces false positives.

4. **Bare `#<slug>` (no `project/` prefix) that matches a configured project.** Considered + rejected during planning (per the AskUserQuestion in the grilling session): too ambiguous, operator might mean the slug literally as a tag. Document the rejection here for future debate.

## Verification

After implementation, hand-test in Obsidian (no automated tests for macros today). The eight scenarios below correspond 1:1 to US47–54.

| # | Selection | Expected modal pre-fill |
|---|---|---|
| 1 | `Review staging deploy #urgent` | Title=`Review staging deploy`, Tags=`urgent`, Project=default |
| 2 | `Ship #urgent #lithos-loom #stretch` | Title=`Ship`, Tags=`urgent, lithos-loom, stretch`, Project=default |
| 3 | `Refactor cache #urgent`, then operator types `prio` in Tags input | Final submit: `--tags urgent,prio` |
| 4 | `Document the API #project/lithos-loom #breaking` | Title=`Document the API`, Tags=`breaking`, Project=`lithos-loom` (auto-selected, `project/lithos-loom` consumed) |
| 5 | `Plan Q3 #project/future-thing` (slug not in TOML) | Title=`Plan Q3`, Tags=`project/future-thing`, Project=default |
| 6 | `Ship feature\nMore context #urgent` (multi-line) | Title=`Ship feature` (first line only), Tags=`urgent` (parsed from whole selection) |
| 7 | `Fix #123 (regression)` | Title=`Fix #123 (regression)` (unchanged — `#123` is all-digit, not a tag), Tags=empty |
| 8 | `#urgent #lithos-loom` (only tags) | Title=empty, Tags=`urgent, lithos-loom`; submit guard rejects until operator types a title |

## Risks

- **JS regex drift from Obsidian's own.** Obsidian's tag rules may change in a future version. The regex above is documented as "Obsidian-compatible as of 2026-05" — if soak surfaces edge cases (e.g. emoji-in-tag support landing upstream), revisit.

- **Operator surprise on first use.** The previous behaviour was "tags get verbatim into Title." Operators with existing muscle memory may type tags into the Title field expecting to manually move them. The README update should call out the new behaviour explicitly so it's not a silent UX flip.

- **No automated test coverage for the macro.** Capture-task.md is a hand-tested file (no unit harness for embedded JS). The verification table above is the contract; future regressions will surface manually. Acceptable given the macro's scope.
