"""Pure parser for `- [ ]` task lines in a project import source doc.

Extracts open-task lines, parses Tasks-plugin metadata (tags, priority
emojis, ``#project/<slug>`` routing), and returns the doc body with
extracted lines stripped — so the project-context doc stores narrative
only, and tasks live as real Lithos task entities (tasks are the single
source of truth, not prose).

The module is intentionally I/O-free. The CLI layer (``cli/project.py``)
owns reading the file and writing to Lithos; this module just parses.

The ``TAG_REGEX`` is exported so other CLI surfaces can reuse the same
tag-parsing contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# Tag pattern: allowed chars ``[A-Za-z0-9_/-]``. Must be preceded by
# whitespace or line start so ``foo#bar`` (where ``#bar`` isn't a tag)
# isn't picked up. The all-digit exclusion (``#123`` isn't a tag) is
# applied in the extractor below, not in the regex itself — keeping the
# exported regex simple so callers can swap their own filter if needed.
TAG_REGEX = re.compile(r"(?:^|(?<=\s))#[A-Za-z0-9_/-]+")

# Priority emojis recognised on import. Order = precedence: when
# multiple priority emojis appear on one task line (unusual but
# possible) the highest one wins. All emojis are stripped from the
# description regardless of which "won".
PRIORITY_EMOJI_MAP: dict[str, str] = {
    "\U0001f53a": "highest",  # 🔺
    "⏫": "high",  # ⏫
    "\U0001f53c": "medium",  # 🔼
    "\U0001f53d": "low",  # 🔽
    "⏬": "lowest",  # ⏬
}

# Marker an operator puts in a parent task description to flip its
# children from parallel (the default) to sequential. Case-sensitive
# and must be a standalone token so it can't false-positive on
# ``[sequential planning]`` or similar prose.
_SEQUENTIAL_MARKER = "[sequential]"

# Task-line detector: ``- [ ]`` after optional leading whitespace.
# Captures the indent prefix and the rest of the line. Other markers
# (``[x]``, ``[/]``, ``[-]``, ``[>]``) are intentionally NOT matched —
# they stay verbatim in the body as historical context.
_TASK_LINE_RE = re.compile(r"^(\s*)-\s+\[ \]\s*(.*)$")

# Fenced code block opener/closer: 3+ backticks or 3+ tildes at line
# start. The closer must use the SAME char and AT LEAST the opener's
# length (CommonMark spec). Captured for the state-machine match.
_FENCE_RE = re.compile(r"^(?P<marker>`{3,}|~{3,})")

# Blockquote: ``>`` after optional leading whitespace. We treat the
# whole line as quoted (no nested-quote parsing); any ``- [ ]`` inside
# is ignored.
_BLOCKQUOTE_RE = re.compile(r"^\s*>")

# Auto-added project-routing tag prefix. Tags of the form
# ``#project/<slug>`` are filtered out of the per-task tag list:
# matching importing slug → silently consumed (auto-added later);
# different slug → flagged as ``cross_project_tag`` for validation
# refusal.
_PROJECT_TAG_PREFIX = "project/"

ValidationKind = Literal[
    "cross_project_tag",
    "empty_parent",
]


@dataclass(frozen=True)
class ParsedTaskLine:
    """One parsed ``- [ ]`` task line, ready for graph-building."""

    line_number: int  # 1-indexed source-file line number for error messages
    indent: int  # leading-whitespace count (chars); used for hierarchy
    description: (
        str  # task text after stripping ``- [ ]``, tags, priority emojis, marker
    )
    tags: tuple[str, ...]  # ``#foo`` tags (no leading ``#``), in source order, deduped
    priority: (
        str | None
    )  # ``"highest"`` / ``"high"`` / ``"medium"`` / ``"low"`` / ``"lowest"`` / ``None``
    cross_project_tag: (
        str | None
    )  # ``project/<other-slug>`` when present and != importing slug
    is_sequential_parent: bool  # description carried ``[sequential]`` marker
    is_empty: bool  # description is empty after all stripping


@dataclass(frozen=True)
class ValidationError:
    """One validation problem found in the source doc.

    Surfaced via the validate-all-then-abort report before any Lithos writes.
    """

    line_number: int
    kind: ValidationKind
    message: str


def parse_doc(
    text: str, importing_slug: str
) -> tuple[list[ParsedTaskLine], list[ValidationError], str]:
    """Parse a Markdown doc into task lines, validation errors, and stripped body.

    Single-pass state machine tracking three contexts (top-level,
    fenced-code-block, blockquote). Only matches ``- [ ]`` at line
    start (after optional leading whitespace). Lines inside code blocks
    or blockquotes are ignored as task candidates and pass through to
    the stripped body verbatim.

    Returns:
        (parsed_lines, errors, stripped_body) where ``parsed_lines``
        contains every matched task line, ``errors`` contains every
        validation failure found during parsing (cross-project tags,
        plus other kinds added by the graph builder), and
        ``stripped_body`` is the doc text with all matched task lines
        removed (their trailing newline goes with them).
    """
    parsed: list[ParsedTaskLine] = []
    errors: list[ValidationError] = []
    body_lines: list[str] = []
    fence_marker: str | None = (
        None  # outer-fence chars (e.g. ``"```"``); None when outside
    )

    for line_number, line in enumerate(text.splitlines(keepends=False), start=1):
        if fence_marker is not None:
            body_lines.append(line)
            if line.startswith(fence_marker[0] * len(fence_marker)) and _FENCE_RE.match(
                line
            ):
                fence_close = _FENCE_RE.match(line)
                assert fence_close is not None  # guarded by startswith above
                closer = fence_close.group("marker")
                if closer[0] == fence_marker[0] and len(closer) >= len(fence_marker):
                    fence_marker = None
            continue

        fence_open = _FENCE_RE.match(line)
        if fence_open is not None:
            fence_marker = fence_open.group("marker")
            body_lines.append(line)
            continue

        if _BLOCKQUOTE_RE.match(line):
            body_lines.append(line)
            continue

        task_match = _TASK_LINE_RE.match(line)
        if task_match is None:
            body_lines.append(line)
            continue

        indent = len(task_match.group(1))
        raw_desc = task_match.group(2)
        parsed_line, err = _parse_task_body(
            line_number=line_number,
            indent=indent,
            raw_desc=raw_desc,
            importing_slug=importing_slug,
        )
        parsed.append(parsed_line)
        if err is not None:
            errors.append(err)

    # Preserve trailing newline if present (splitlines drops it; we add
    # it back so the stripped body round-trips through write/read).
    stripped_body = "\n".join(body_lines)
    if text.endswith("\n") and stripped_body:
        stripped_body += "\n"
    return parsed, errors, stripped_body


def _parse_task_body(
    *,
    line_number: int,
    indent: int,
    raw_desc: str,
    importing_slug: str,
) -> tuple[ParsedTaskLine, ValidationError | None]:
    """Parse the post-``- [ ]`` portion of one task line."""
    priority, desc_no_priority = _extract_priority(raw_desc)
    tags, cross_project_tag, desc_no_tags = _extract_tags(
        desc_no_priority, importing_slug
    )

    is_sequential_parent = _SEQUENTIAL_MARKER in desc_no_tags
    if is_sequential_parent:
        desc_no_tags = desc_no_tags.replace(_SEQUENTIAL_MARKER, "")

    description = _collapse_whitespace(desc_no_tags)
    is_empty = not description

    err: ValidationError | None = None
    if cross_project_tag is not None:
        err = ValidationError(
            line_number=line_number,
            kind="cross_project_tag",
            message=(
                f"line {line_number}: task carries #{cross_project_tag} "
                f"(different from importing project '{importing_slug}'); "
                "remove the tag or import this task via its owning project"
            ),
        )

    parsed = ParsedTaskLine(
        line_number=line_number,
        indent=indent,
        description=description,
        tags=tuple(tags),
        priority=priority,
        cross_project_tag=cross_project_tag,
        is_sequential_parent=is_sequential_parent,
        is_empty=is_empty,
    )
    return parsed, err


def _extract_priority(text: str) -> tuple[str | None, str]:
    """Return (priority_name, text_with_priority_emojis_stripped).

    Iterates ``PRIORITY_EMOJI_MAP`` in declaration order so the first
    hit wins (precedence: highest → lowest). All priority emojis are
    stripped from the returned text regardless of which won.
    """
    priority: str | None = None
    for emoji, name in PRIORITY_EMOJI_MAP.items():
        if emoji in text:
            if priority is None:
                priority = name
            text = text.replace(emoji, "")
    return priority, text


def _extract_tags(text: str, importing_slug: str) -> tuple[list[str], str | None, str]:
    """Return (tags, cross_project_tag_or_None, text_with_tags_stripped).

    Iterates ``TAG_REGEX`` matches in source order, deduping. All-digit
    matches (e.g. ``#123``) are NOT tags — they stay as literal text in
    the description (so issue references like ``#123`` are preserved).

    ``#project/<importing-slug>`` is silently consumed (auto-added
    later if missing). ``#project/<other-slug>`` is captured into the
    ``cross_project_tag`` return slot for validation refusal — the first
    such tag wins; subsequent ones are ignored (the whole import will
    abort regardless).
    """
    tags: list[str] = []
    seen: set[str] = set()
    cross_project_tag: str | None = None

    def _replace(match: re.Match[str]) -> str:
        nonlocal cross_project_tag
        tag_text = match.group()[1:]  # drop leading ``#``
        if tag_text.isdigit():
            return match.group()  # keep ``#123`` as literal text
        if tag_text.startswith(_PROJECT_TAG_PREFIX):
            slug_in_tag = tag_text[len(_PROJECT_TAG_PREFIX) :]
            if slug_in_tag != importing_slug and cross_project_tag is None:
                cross_project_tag = tag_text
            return ""  # strip both same-project and cross-project tags
        if tag_text not in seen:
            seen.add(tag_text)
            tags.append(tag_text)
        return ""

    stripped = TAG_REGEX.sub(_replace, text)
    return tags, cross_project_tag, stripped


def _collapse_whitespace(text: str) -> str:
    """Collapse internal runs of whitespace and strip ends.

    Tag/emoji stripping leaves double-spaces and trailing whitespace.
    ``re.sub(r"\\s+", " ", text).strip()`` normalises to single-space
    runs with no leading/trailing whitespace.
    """
    return re.sub(r"\s+", " ", text).strip()
