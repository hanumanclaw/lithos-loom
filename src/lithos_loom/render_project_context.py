"""Renderer + frontmatter helpers for project-context vault projection.

Pure functions only — given a :class:`~lithos_loom.lithos_client.Note`,
:func:`render_doc` returns the Markdown string the projection writes
to ``<vault>/_lithos/projects/<slug>/<filename>.md``. No I/O. Mirrors
the design of :mod:`lithos_loom.render` for tasks.

Frontmatter shape is YAML, with these keys:

* ``lithos_id`` — UUID, the canonical KB identifier
* ``lithos_version`` — int, used for optimistic locking on push-back
* ``lithos_updated_at`` — ISO datetime, last server-side update
* ``slug`` — string, redundant with directory name but useful for queries
* ``status`` — ``active`` | ``archived`` | ``quarantined`` (omitted when None)
* ``tags`` — list of strings (omitted when empty)

Body is ``# {title}\n\n{body}`` — matches
:attr:`~lithos.knowledge.KnowledgeDocument.full_content` so the
projected file is byte-comparable round-trip when nothing changes.

The bidirectional sync layer uses the same module via:

* :func:`extract_frontmatter` — splits a vault file into
  ``(frontmatter_dict, body_with_title)``. Used by the dir-watcher to
  parse ``lithos_id`` / ``lithos_version`` for push-back and by the
  note-push handler to recover body.
* :func:`compute_body_hash` — SHA-256 of body-only (frontmatter
  excluded). Used by the dir-watcher's body-only diff (frontmatter
  edits must never push back to Lithos).

The split is intentional: rendering and parsing are pure inverses
under valid inputs, so a round-trip ``render_doc(...) -> extract_frontmatter(...)``
recovers the same fields. Malformed user-edited frontmatter degrades
gracefully (the parser returns whatever YAML it can decode + the rest
as body); the renderer always produces well-formed YAML.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

import yaml

from lithos_loom.lithos_client import Note

__all__ = [
    "compute_body_hash",
    "extract_frontmatter",
    "render_doc",
]

logger = logging.getLogger(__name__)


# ``^---\n<yaml>\n---\n<body>`` — the standard Obsidian / Jekyll
# frontmatter delimiter shape. ``re.DOTALL`` so the YAML block can
# span multiple lines; the body capture goes to end of string.
# Strict-anchored at start so a ``---`` mid-document (e.g. a Markdown
# horizontal rule below the title) doesn't accidentally match.
_FRONTMATTER_RE = re.compile(
    r"^---\n(?P<frontmatter>.*?)\n---\n(?P<body>.*)\Z",
    re.DOTALL,
)


def render_doc(note: Note) -> str:
    """Render a :class:`Note` into a Markdown string with YAML frontmatter.

    The output shape is byte-stable for a given Note — running this
    twice on the same input yields identical bytes (load-bearing for
    the projection's content-hash dedup which skips writes when the
    rendered file would be a no-op).

    The body section is always ``# {title}\n\n{body}`` even when
    ``note.body`` is empty (a doc with no body still gets the H1
    title; query patterns rely on the title being inside the file,
    not just the frontmatter).

    Defensive against double-title rendering: ``lithos_write`` stores
    ``content`` verbatim, so an operator who passes
    ``content="# Title\\n\\nBody"`` ends up with the H1 baked into
    ``note.body``. The disk-watcher intake path strips it via
    ``extract_title_from_content``; the MCP write path does not. We
    treat both inputs identically here — if the body starts with the
    title's H1, drop it before re-rendering, so the projected file
    has exactly ONE ``# Title`` regardless of which write path put
    the doc in Lithos.
    """
    fm = _build_frontmatter(note)
    yaml_block = yaml.safe_dump(
        fm,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).rstrip()
    body = _strip_leading_title(note.body, note.title).rstrip()
    return f"---\n{yaml_block}\n---\n# {note.title}\n\n{body}\n"


def _strip_leading_title(body: str, title: str) -> str:
    """Drop a leading ``# {title}`` line + following blank line from ``body``.

    Returns ``body`` unchanged if the H1 doesn't match the title
    (operator's body genuinely starts with a different heading). Only
    a single leading H1 is considered — once we strip it the body is
    treated as title-less for rendering purposes.

    Comparison is whitespace-stripped on the H1 text but exact on
    title — different titles (even same slug) should not collide.
    """
    if not body:
        return body
    expected = f"# {title}"
    lines = body.split("\n", 2)
    first = lines[0].rstrip()
    if first != expected:
        return body
    # Drop the H1 line plus a single blank separator if present.
    if len(lines) >= 3 and lines[1] == "":
        return lines[2]
    if len(lines) >= 2:
        return lines[1]
    return ""


def _build_frontmatter(note: Note) -> dict[str, Any]:
    """Build the ordered frontmatter dict for a :class:`Note`.

    Order: id → version → updated_at → slug → status → tags — Python
    dicts preserve insertion order, and
    ``yaml.safe_dump(sort_keys=False)`` honours it. Stable key order
    is part of the byte-stable contract.

    Optional fields (``status``, ``tags``) are omitted when absent so
    the rendered file doesn't grow ``status: null`` / ``tags: []``
    lines that the operator's queries would have to ignore.
    """
    fm: dict[str, Any] = {
        "lithos_id": note.id,
        "lithos_version": note.version,
    }
    if note.updated_at is not None:
        fm["lithos_updated_at"] = note.updated_at.isoformat()
    if note.slug:
        fm["slug"] = note.slug
    if note.status is not None:
        fm["status"] = note.status
    if note.tags:
        fm["tags"] = list(note.tags)
    return fm


def extract_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a rendered Markdown file into ``(frontmatter_dict, body_with_title)``.

    Returns ``({}, text)`` when the file has no frontmatter at all
    (e.g. an operator manually added a Markdown file under
    ``_lithos/projects/`` without going through the projection) or
    when the frontmatter block is malformed YAML — both degrade
    gracefully rather than raising.

    The ``body_with_title`` half includes the ``# {title}`` line
    because that's what gets pushed back to Lithos via
    ``lithos_write``: Lithos's :attr:`KnowledgeDocument.full_content`
    re-renders the title from its own ``title`` field, so the
    pushed body should match the *full* content the operator sees in
    Obsidian.

    Malformed frontmatter is warn-logged but not raised — the dir-
    watcher needs to keep going through occasional bad files; the
    note-push handler will degrade to "no version to optimistic-lock
    against" and the conflict path will fire on actual divergence.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text
    yaml_block = match.group("frontmatter")
    body = match.group("body")
    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        logger.warning(
            "render_project_context: malformed frontmatter YAML; "
            "treating as no-frontmatter: %s",
            exc,
        )
        return {}, text
    if not isinstance(parsed, dict):
        # Edge case: ``---\n42\n---\n...`` parses as int 42. Treat as
        # no-frontmatter rather than raising.
        return {}, text
    return parsed, body


def compute_body_hash(text: str) -> bytes:
    """SHA-256 of the body half (frontmatter excluded).

    Used by the dir-watcher to detect body-only changes (frontmatter
    edits must not push back to Lithos). Files without frontmatter hash
    the whole content — there's nothing to exclude.

    Returns raw bytes (not hex) so callers can do byte-compare; the
    fs-watcher's hash maps already use bytes for the same reason.
    """
    _, body = extract_frontmatter(text)
    return hashlib.sha256(body.encode("utf-8")).digest()
