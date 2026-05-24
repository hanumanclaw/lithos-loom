"""Tests for ``lithos_loom.render_project_context`` (Slice 4 US29 + US30).

The render module is pure — given a :class:`Note`, ``render_doc``
returns the file contents the projection writes. ``extract_frontmatter``
and ``compute_body_hash`` are the inverses used by the Slice 5
dir-watcher.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lithos_loom.lithos_client import Note
from lithos_loom.render_project_context import (
    compute_body_hash,
    extract_frontmatter,
    render_doc,
)


def _note(
    *,
    id_: str = "doc-1",
    title: str = "Lithos Loom",
    body: str = "Body content goes here.",
    version: int = 12,
    updated_at: datetime | None = datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
    tags: tuple[str, ...] = ("project-context", "track-1"),
    status: str | None = "active",
    note_type: str | None = "concept",
    path: str = "projects/lithos-loom/context.md",
    slug: str = "lithos-loom",
) -> Note:
    return Note(
        id=id_,
        title=title,
        body=body,
        version=version,
        updated_at=updated_at,
        tags=tags,
        status=status,
        note_type=note_type,
        path=path,
        slug=slug,
    )


# ── render_doc ──────────────────────────────────────────────────────────


def test_render_doc_emits_frontmatter_then_body() -> None:
    rendered = render_doc(_note())
    lines = rendered.splitlines()
    # ``---`` delimiters, then YAML, then ``---``, then ``# title``, then body.
    assert lines[0] == "---"
    assert "lithos_id: doc-1" in rendered
    assert "lithos_version: 12" in rendered
    # Find the closing ``---``; everything after the next blank line is body.
    closing = lines.index("---", 1)
    assert lines[closing + 1] == "# Lithos Loom"
    assert "Body content goes here." in rendered


def test_render_doc_frontmatter_key_order_is_stable() -> None:
    """D25 pins frontmatter key order: id → version → updated_at →
    slug → status → tags. Stable byte output is load-bearing for the
    projection's per-doc hash dedup."""
    rendered = render_doc(_note())
    # Extract just the YAML block, scan for keys in order.
    yaml_start = rendered.index("---\n") + 4
    yaml_end = rendered.index("\n---\n", yaml_start)
    yaml_block = rendered[yaml_start:yaml_end]
    keys_in_order = [
        line.split(":", 1)[0]
        for line in yaml_block.splitlines()
        if ":" in line and not line.startswith(" ") and not line.startswith("-")
    ]
    assert keys_in_order == [
        "lithos_id",
        "lithos_version",
        "lithos_updated_at",
        "slug",
        "status",
        "tags",
    ]


def test_render_doc_is_byte_stable() -> None:
    """Same input → same bytes. Pins the dedup invariant — two
    consecutive renders of the same note must hash identically."""
    n = _note()
    assert render_doc(n) == render_doc(n)


def test_render_doc_omits_optional_fields_when_absent() -> None:
    """``status`` and ``tags`` are omitted (not emitted as
    ``status: null`` / ``tags: []``) when the note has none. Keeps
    the rendered file readable for the operator and avoids forcing
    Dataview queries to filter on null."""
    n = _note(status=None, tags=())
    rendered = render_doc(n)
    assert "status:" not in rendered
    assert "tags:" not in rendered


def test_render_doc_omits_updated_at_when_none() -> None:
    n = _note(updated_at=None)
    rendered = render_doc(n)
    assert "lithos_updated_at" not in rendered


def test_render_doc_uses_iso_format_for_updated_at() -> None:
    n = _note(updated_at=datetime(2026, 5, 24, 14, 30, 45, tzinfo=UTC))
    rendered = render_doc(n)
    assert "lithos_updated_at: '2026-05-24T14:30:45+00:00'" in rendered


def test_render_doc_handles_unicode_title_and_body() -> None:
    """``allow_unicode=True`` on yaml.safe_dump + UTF-8 body must
    preserve non-ASCII glyphs round-trip."""
    n = _note(title="Project — Lithium 锂", body="Body with emoji 🎯 here.")
    rendered = render_doc(n)
    assert "Project — Lithium 锂" in rendered
    assert "🎯" in rendered


def test_render_doc_handles_empty_body() -> None:
    """An empty body still produces a valid file with the H1 title —
    the title is in the body, not just frontmatter, so queries can
    find it textually."""
    n = _note(body="")
    rendered = render_doc(n)
    assert "# Lithos Loom" in rendered


def test_render_doc_collapses_trailing_whitespace_in_body() -> None:
    """Trailing newlines/spaces in the body are collapsed to a single
    trailing newline so byte-stability holds even if Lithos sends the
    body with trailing whitespace artifacts."""
    n = _note(body="text\n\n\n")
    rendered = render_doc(n)
    # File ends with exactly one trailing newline (after the body line).
    assert rendered.endswith("text\n")
    assert not rendered.endswith("\n\n")


# ── extract_frontmatter ─────────────────────────────────────────────────


def test_extract_frontmatter_round_trips_render_doc() -> None:
    """The renderer and parser are inverses under valid inputs.
    Pins the contract Slice 5's dir-watcher relies on."""
    n = _note()
    rendered = render_doc(n)
    fm, body = extract_frontmatter(rendered)
    assert fm["lithos_id"] == "doc-1"
    assert fm["lithos_version"] == 12
    assert fm["slug"] == "lithos-loom"
    assert fm["status"] == "active"
    assert fm["tags"] == ["project-context", "track-1"]
    # Body half contains the ``# title`` line + body, not just the body.
    assert body.startswith("# Lithos Loom")
    assert "Body content goes here." in body


def test_extract_frontmatter_returns_empty_dict_when_no_frontmatter() -> None:
    """A plain markdown file without frontmatter parses to
    ``({}, original_text)`` — operators can drop ad-hoc notes under
    ``_lithos/projects/`` and the dir-watcher won't crash."""
    text = "# Just a markdown file\n\nSome content."
    fm, body = extract_frontmatter(text)
    assert fm == {}
    assert body == text


def test_extract_frontmatter_returns_empty_dict_on_malformed_yaml() -> None:
    """Malformed YAML degrades gracefully — warn-logged, treated as
    no-frontmatter. The dir-watcher needs to keep going through
    occasional bad files; the note-push will degrade to "no
    version" and the conflict path catches actual divergence."""
    text = "---\n  - bad: : indent\n---\nbody"
    fm, body = extract_frontmatter(text)
    assert fm == {}
    assert body == text


def test_extract_frontmatter_treats_non_dict_yaml_as_no_frontmatter() -> None:
    """``---\n42\n---\nbody`` parses to int 42; treat as
    no-frontmatter rather than crashing the consumer."""
    text = "---\n42\n---\nbody"
    fm, body = extract_frontmatter(text)
    assert fm == {}


def test_extract_frontmatter_ignores_mid_document_dashes() -> None:
    """A ``---`` Markdown horizontal rule mid-body must NOT match
    as a frontmatter delimiter — the regex is anchored at start of
    string."""
    text = "Some text\n\n---\n\nMore text"
    fm, body = extract_frontmatter(text)
    assert fm == {}
    assert body == text


# ── compute_body_hash ───────────────────────────────────────────────────


def test_compute_body_hash_excludes_frontmatter() -> None:
    """D28 invariant: changing only frontmatter must not change the
    body hash. Slice 5's dir-watcher uses this to ignore frontmatter
    edits."""
    n = _note()
    rendered_v1 = render_doc(n)
    # Same body, different version (frontmatter change only).
    rendered_v2 = render_doc(_note(version=13))
    assert compute_body_hash(rendered_v1) == compute_body_hash(rendered_v2)


def test_compute_body_hash_changes_when_body_changes() -> None:
    """Inverse: changing the body must change the hash, even when
    frontmatter is identical (here: same version + same metadata)."""
    n1 = render_doc(_note(body="version A"))
    n2 = render_doc(_note(body="version B"))
    assert compute_body_hash(n1) != compute_body_hash(n2)


def test_compute_body_hash_returns_bytes() -> None:
    """Caller stores in ``sync_state.note_content_hashes: dict[str, bytes]``
    — raw bytes, not hex, so byte-compare is direct."""
    h = compute_body_hash(render_doc(_note()))
    assert isinstance(h, bytes)
    assert len(h) == 32  # SHA-256


def test_compute_body_hash_handles_no_frontmatter() -> None:
    """A file without frontmatter hashes its entire content (there's
    nothing to exclude). Pins the no-frontmatter path."""
    h1 = compute_body_hash("# Just a markdown file\n\nSome content.")
    h2 = compute_body_hash("# Just a markdown file\n\nDifferent content.")
    assert h1 != h2
