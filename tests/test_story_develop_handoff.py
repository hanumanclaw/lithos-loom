"""Tests for structured-finding parsing, validation, and the verdict logic."""

from __future__ import annotations

import pytest

from lithos_loom.plugins.story_develop.handoff import (
    HandoffError,
    parse_review_handoff,
    reviewer_handoff_name,
    severity_at_or_above,
)

_LGTM = "## Status: LGTM\n## Summary\nAll good.\n"
_FINDINGS = (
    "## Status: FINDINGS\n"
    "## Summary\nTwo issues found.\n"
    "## Findings\n"
    "- finding_id: f-001\n"
    "  severity: major\n"
    "  status: open\n"
    '  files: ["a.py:10", "b.py:3"]\n'
    "  rationale: missing validation\n"
    "  coder_response:\n"
    "- finding_id: f-002\n"
    "  severity: minor\n"
    "  status: open\n"
    "  files: a.py:20\n"
    "  rationale: nit\n"
)


def test_parse_lgtm() -> None:
    h = parse_review_handoff(_LGTM)
    assert h.is_lgtm
    assert h.status == "LGTM"
    assert h.summary == "All good."
    assert h.findings == []
    assert h.max_open_severity is None
    assert h.passes("major") is True


def test_parse_findings_with_severities_and_files() -> None:
    h = parse_review_handoff(_FINDINGS)
    assert h.status == "FINDINGS"
    assert len(h.findings) == 2
    f1, f2 = h.findings
    assert f1.finding_id == "f-001"
    assert f1.severity == "major"
    assert f1.files == ["a.py:10", "b.py:3"]
    assert f2.files == ["a.py:20"]  # bare comma-less value also parses
    assert h.max_open_severity == "major"


def test_threshold_blocks_and_passes() -> None:
    h = parse_review_handoff(_FINDINGS)
    assert h.passes("major") is False  # a major open finding blocks at major
    assert h.passes("critical") is True  # nothing critical -> passes at critical


def test_resolved_findings_do_not_block() -> None:
    text = _FINDINGS.replace(
        'status: open\n  files: ["a.py:10"', 'status: fixed\n  files: ["a.py:10"'
    )
    h = parse_review_handoff(text)
    # f-001 is now 'fixed' (resolved); only the minor f-002 remains open
    assert h.max_open_severity == "minor"
    assert h.passes("major") is True


def test_empty_handoff_raises() -> None:
    with pytest.raises(HandoffError, match="empty"):
        parse_review_handoff("   ")


def test_missing_status_raises() -> None:
    with pytest.raises(HandoffError, match="Status"):
        parse_review_handoff("## Summary\njust some text\n")


def test_findings_without_entries_raises() -> None:
    with pytest.raises(HandoffError, match="no '## Findings'"):
        parse_review_handoff("## Status: FINDINGS\n## Summary\nclaims findings\n")


def test_invalid_severity_raises() -> None:
    bad = (
        "## Status: FINDINGS\n## Findings\n"
        "- finding_id: f-1\n  severity: huge\n  status: open\n"
    )
    with pytest.raises(HandoffError, match="severity"):
        parse_review_handoff(bad)


def test_invalid_status_value_raises() -> None:
    bad = (
        "## Status: FINDINGS\n## Findings\n"
        "- finding_id: f-1\n  severity: major\n  status: bogus\n"
    )
    with pytest.raises(HandoffError, match="status"):
        parse_review_handoff(bad)


def test_severity_at_or_above() -> None:
    assert severity_at_or_above("critical", "major") is True
    assert severity_at_or_above("minor", "major") is False
    assert severity_at_or_above("major", "major") is True


def test_reviewer_handoff_name() -> None:
    assert reviewer_handoff_name(1, "security") == "round_01_review_security.md"


def test_headers_with_trailing_colon_are_tolerated() -> None:
    # "## Findings:" / "## Summary:" (trailing colon) is a common variant and
    # must not break section lookup (Copilot review on PR #75).
    text = (
        "## Status: FINDINGS\n"
        "## Summary:\nNeeds a guard.\n"
        "## Findings:\n"
        "- finding_id: f-1\n  severity: major\n  status: open\n"
    )
    h = parse_review_handoff(text)
    assert h.status == "FINDINGS"
    assert h.summary == "Needs a guard."
    assert len(h.findings) == 1 and h.findings[0].severity == "major"
