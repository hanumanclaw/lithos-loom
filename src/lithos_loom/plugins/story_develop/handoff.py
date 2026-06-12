"""Handoff directory, filenames, and structured-finding parsing.

A handoff is the structured-markdown sign-off an agent writes per turn (see
``prompts/FORMAT.md``). T1 only seeded the dir; T2 adds parsing + validation of
the reviewer's findings block and the LGTM / severity-threshold verdict.

The parser is deliberately line-based and tolerant rather than strict YAML —
agent output varies, and a malformed handoff should be *re-promptable*, not a
crash. Validation raises :class:`HandoffError` with a human message that is fed
back to the agent as a correction prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

_PROMPTS = "lithos_loom.plugins.story_develop.prompts"

# --- severity (ported from Ralph++ tools/base.py) --------------------------

_SEVERITIES = ("minor", "major", "critical")
_SEVERITY_ORDER = {s: i for i, s in enumerate(_SEVERITIES)}

# Finding lifecycle states (T7 enforces transitions; T2 only parses/validates).
_OPEN_STATES = frozenset({"open", "disputed", "needs-clarification"})
_RESOLVED_STATES = frozenset({"fixed", "accepted", "superseded", "merged"})
_ALL_STATES = _OPEN_STATES | _RESOLVED_STATES


def severity_at_or_above(severity: str, threshold: str) -> bool:
    """True if *severity* meets or exceeds *threshold* (minor < major < critical)."""
    return _SEVERITY_ORDER[severity.lower()] >= _SEVERITY_ORDER[threshold.lower()]


def max_severity(severities: list[str]) -> str | None:
    """Highest severity in the list, or ``None`` when empty."""
    if not severities:
        return None
    return max((s.lower() for s in severities), key=lambda s: _SEVERITY_ORDER[s])


class HandoffError(ValueError):
    """A handoff file was missing required structure or had invalid values."""


@dataclass(frozen=True)
class Finding:
    """One addressable review finding (see ``prompts/FORMAT.md``)."""

    finding_id: str
    severity: str  # critical | major | minor
    status: str  # open | fixed | accepted | disputed | needs-clarification | ...
    files: list[str] = field(default_factory=list)
    rationale: str = ""
    coder_response: str = ""

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN_STATES


@dataclass(frozen=True)
class ReviewHandoff:
    """A parsed reviewer handoff: a verdict plus structured findings."""

    status: str  # "LGTM" | "FINDINGS"
    summary: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def is_lgtm(self) -> bool:
        return self.status == "LGTM"

    @property
    def open_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.is_open]

    @property
    def max_open_severity(self) -> str | None:
        return max_severity([f.severity for f in self.open_findings])

    def passes(self, threshold: str) -> bool:
        """True if the reviewer is satisfied for this round.

        Pass when LGTM, or the highest *open* finding is below *threshold*
        (sub-threshold findings are recorded but non-blocking — PRD decision #7).
        """
        if self.is_lgtm:
            return True
        top = self.max_open_severity
        return top is None or not severity_at_or_above(top, threshold)


# --- prompt + filename helpers ---------------------------------------------


def load_prompt(name: str) -> str:
    """Read a packaged prompt template (e.g. ``coder_init.md``)."""
    return resources.files(_PROMPTS).joinpath(name).read_text(encoding="utf-8")


def coder_handoff_name(round_no: int) -> str:
    """Filename for the coder's handoff in a given round (1-based)."""
    return f"round_{round_no:02d}_coder_done.md"


def reviewer_handoff_name(round_no: int, reviewer: str) -> str:
    """Filename for a reviewer's handoff in a given round."""
    return f"round_{round_no:02d}_review_{reviewer}.md"


def seed_handoff_dir(handoff_dir: Path) -> Path:
    """Create *handoff_dir* and write ``FORMAT.md`` into it.

    *handoff_dir* lives outside the git worktree and is mounted into the
    container at ``/workspace/.handoff``. Returns the directory path.
    """
    handoff_dir.mkdir(parents=True, exist_ok=True)
    (handoff_dir / "FORMAT.md").write_text(load_prompt("FORMAT.md"), encoding="utf-8")
    return handoff_dir


# --- parsing ----------------------------------------------------------------

_HEADER_RE = re.compile(r"^\s*#{1,6}\s+(.*?)\s*$")
_STATUS_RE = re.compile(r"status\s*:\s*([A-Za-z_-]+)", re.IGNORECASE)
_ITEM_RE = re.compile(r"^\s*-\s*(.*)$")
_KV_RE = re.compile(r"^\s*([A-Za-z_]+)\s*:\s*(.*)$")


def _sections(text: str) -> dict[str, str]:
    """Split markdown into ``{lowercased-header: body}`` by ``##`` headers."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            # normalise a trailing colon: "## Findings:" -> "findings" (tolerant).
            header = re.sub(r"\s*:\s*$", "", m.group(1).strip().lower())
            current = header
            sections.setdefault(header, [])
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _parse_status(sections: dict[str, str], full: str) -> str:
    """Resolve the LGTM/FINDINGS verdict from the ``Status`` header or text."""
    raw = ""
    for key, body in sections.items():
        if key.startswith("status"):
            # header may be "status: lgtm" or body may hold it
            raw = key[len("status") :].lstrip(": ").strip() or body.strip()
            break
    if not raw:
        m = _STATUS_RE.search(full)
        raw = m.group(1) if m else ""
    token = raw.strip().lower()
    if "lgtm" in token:
        return "LGTM"
    if "finding" in token:
        return "FINDINGS"
    raise HandoffError("missing or invalid '## Status:' — must be 'LGTM' or 'FINDINGS'")


def _split_files(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    parts = [p.strip().strip("\"'") for p in value.split(",")]
    return [p for p in parts if p]


def _parse_findings(block: str) -> list[Finding]:
    """Parse the ``## Findings`` body into a list of :class:`Finding`."""
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in block.splitlines():
        item = _ITEM_RE.match(line)
        if item:  # new list entry: "- finding_id: ..."
            current = {}
            items.append(current)
            rest = item.group(1)
            kv = _KV_RE.match(rest)
            if kv:
                current[kv.group(1).lower()] = kv.group(2).strip()
            continue
        if current is None:
            continue
        kv = _KV_RE.match(line)
        if kv:
            current[kv.group(1).lower()] = kv.group(2).strip()

    findings: list[Finding] = []
    for idx, raw in enumerate(items, start=1):
        severity = raw.get("severity", "").strip().lower()
        if severity not in _SEVERITY_ORDER:
            raise HandoffError(
                f"finding {idx}: severity must be one of "
                f"{', '.join(_SEVERITIES)} (got {severity!r})"
            )
        status = (raw.get("status") or "open").strip().lower()
        if status not in _ALL_STATES:
            raise HandoffError(
                f"finding {idx}: invalid status {status!r} "
                f"(allowed: {', '.join(sorted(_ALL_STATES))})"
            )
        findings.append(
            Finding(
                finding_id=raw.get("finding_id") or raw.get("id") or f"f-{idx:03d}",
                severity=severity,
                status=status,
                files=_split_files(raw.get("files", "")),
                rationale=raw.get("rationale", ""),
                coder_response=raw.get("coder_response", ""),
            )
        )
    return findings


def parse_review_handoff(text: str) -> ReviewHandoff:
    """Parse + validate a reviewer handoff. Raises :class:`HandoffError`.

    The error message is suitable to feed back to the agent as a correction.
    """
    if not text.strip():
        raise HandoffError("handoff file is empty")
    sections = _sections(text)
    status = _parse_status(sections, text)
    summary = sections.get("summary", "").strip()
    findings = (
        _parse_findings(sections.get("findings", "")) if "findings" in sections else []
    )
    if status == "FINDINGS" and not findings:
        raise HandoffError(
            "Status is FINDINGS but no '## Findings' entries were parsed"
        )
    return ReviewHandoff(status=status, summary=summary, findings=findings)
