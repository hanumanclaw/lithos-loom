# Handoff format

Agents communicate by writing one **handoff file** per turn into
`/workspace/.handoff/`. The handoff is the only thing that crosses between
agents — your working notes stay in your own session.

A handoff is Markdown with this shape:

```markdown
## Status: FINDINGS | LGTM

## Summary
One short paragraph. The coder also reports test results here.

## Findings
(only when Status is FINDINGS — structured, one block per finding)
- finding_id: <assigned by the orchestrator; reference existing ones, do not invent>
  severity: critical | major | minor
  status: open | fixed | accepted | disputed | needs-clarification
  files: ["path:line", ...]
  rationale: <why>
  coder_response: <what changed, or why disputed>
```

For the coder's first turn there are no findings — just write
`## Status: LGTM` plus a `## Summary` of what you implemented and the result of
running the project's tests.
