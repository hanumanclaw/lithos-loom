# prd-decompose Claude prompt

> Adapted from Matt Pocock's `to-issues` skill: <https://raw.githubusercontent.com/mattpocock/skills/refs/heads/main/skills/engineering/to-issues/SKILL.md>

This file is the prompt template fed to Claude when `prd-decompose` runs.
The plugin loads the PRD body from Lithos, substitutes it into this template,
and asks Claude for structured JSON output matching the schema below.

## Expected output schema

```json
{
  "stories": [
    {
      "title": "<short imperative title>",
      "brief": "<≥80-word brief: problem framing, what to build, success criteria, references>",
      "acceptance_criteria": ["<criterion 1>", "<criterion 2>", ...],
      "deps": [<1-based-index of prior story this depends on>, ...],
      "files_hint": ["<likely file path 1>", ...],
      "parallelizable": false
    }
  ]
}
```

## Prompt body

(TODO: write the full Pocock-shaped decomposition prompt here, referencing
the PRD body as `<<PRD_BODY>>` substitution token. See US-12 in
`docs/prd/mvp.md`.)
