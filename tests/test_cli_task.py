"""Tests for ``lithos-loom task create`` (Slice 3 US24+).

The CLI calls ``LithosClient.task_create`` via an async context
manager. We patch ``LithosClient`` in :mod:`lithos_loom.cli.task`
with a small async-context-manager stub that records its
invocations so we can assert on the wire-shape that hits Lithos.

The renderer's correctness is covered by ``tests/test_render.py``;
these tests verify CLI plumbing: argv → LithosClient args, line
formatting via the shared renderer, target-file write semantics,
input validation, and exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from lithos_loom.cli import task as cli_task_mod
from lithos_loom.lithos_client import NoteSummary
from lithos_loom.main import app

runner = CliRunner()


# ── Helpers ────────────────────────────────────────────────────────────


class _StubLithosClient:
    """Async-context-manager stand-in for :class:`LithosClient`.

    Records every ``task_create`` invocation on a class-level list so
    tests can assert on the args. ``task_create_returns`` controls the
    task_id the stub returns (default: ``"new-1"``)."""

    task_create_calls: ClassVar[list[dict[str, Any]]] = []
    task_create_returns: ClassVar[str] = "new-1"
    task_create_side_effect: ClassVar[Exception | None] = None
    # Canonical Lithos project-context summaries returned by note_list
    # (the project-validation source). Default empty → only TOML
    # [projects] slugs validate, unless a test seeds Lithos-side slugs.
    note_list_returns: ClassVar[list[Any]] = []
    note_list_side_effect: ClassVar[Exception | None] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.init_args = args
        # The real LithosClient takes ``agent_id`` kwarg and injects
        # it into per-call args when the caller doesn't pass an
        # explicit ``agent``. Mirror that so tests can assert on the
        # final agent that hits Lithos, not the un-injected None.
        self._agent_id = kwargs.get("agent_id")

    async def __aenter__(self) -> _StubLithosClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def task_create(
        self,
        *,
        title: str,
        agent: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        # Mirror LithosClient.task_create's agent injection so
        # recorded calls show the final agent_id rather than ``None``.
        effective_agent = agent if agent is not None else self._agent_id
        type(self).task_create_calls.append(
            {
                "title": title,
                "agent": effective_agent,
                "description": description,
                "tags": tags,
                "metadata": metadata,
            }
        )
        cls = type(self)
        if cls.task_create_side_effect is not None:
            raise cls.task_create_side_effect
        return cls.task_create_returns

    async def note_list(
        self,
        *,
        path_prefix: str | None = None,
        tags: list[str] | None = None,
        **_: Any,
    ) -> list[Any]:
        cls = type(self)
        if cls.note_list_side_effect is not None:
            raise cls.note_list_side_effect
        return list(cls.note_list_returns)


@pytest.fixture(autouse=True)
def _reset_stub() -> None:
    """Clear class-level stub state between tests so cross-test
    leakage can't make an assertion accidentally pass."""
    _StubLithosClient.task_create_calls.clear()
    _StubLithosClient.task_create_returns = "new-1"
    _StubLithosClient.task_create_side_effect = None
    _StubLithosClient.note_list_returns = []
    _StubLithosClient.note_list_side_effect = None


@pytest.fixture
def patched_lithos(monkeypatch: pytest.MonkeyPatch) -> type[_StubLithosClient]:
    """Patch ``LithosClient`` inside :mod:`lithos_loom.cli.task`."""
    monkeypatch.setattr(cli_task_mod, "LithosClient", _StubLithosClient)
    return _StubLithosClient


def _write_config(
    tmp_path: Path, *, projects: tuple[str, ...] = ("lithos-loom",)
) -> Path:
    """Write a config with the given project slugs."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    project_blocks = "\n".join(
        f'[projects.{slug}]\nrepo = "{repo}"\n' for slug in projects
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            f"""
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            {project_blocks}
            """
        )
    )
    return config_path


def _project_summary(slug: str) -> NoteSummary:
    """A Lithos project-context doc summary as note_list would return."""
    return NoteSummary(
        id=f"doc-{slug}",
        title=slug,
        version=1,
        updated_at=None,
        tags=("project-context",),
        status="active",
        note_type="project_context",
        path=f"projects/{slug}/{slug}-project-context.md",
        slug=slug,
    )


# ── Happy path ─────────────────────────────────────────────────────────


def test_task_create_minimum_args_prints_projected_line(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """``--project`` + ``--title`` → calls task_create with title +
    metadata.project, prints the projected line via the shared
    renderer."""
    config_path = _write_config(tmp_path)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Review PR",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    # LithosClient.task_create was called with the right shape.
    assert len(patched_lithos.task_create_calls) == 1
    call = patched_lithos.task_create_calls[0]
    assert call["title"] == "Review PR"
    assert call["agent"] == "lithos-orchestrator-test"
    assert call["description"] is None
    assert call["tags"] is None
    assert call["metadata"] == {"project": "lithos-loom"}

    # Output is the projected line.
    assert result.stdout.strip() == (
        "- [ ] Review PR 🆔 lithos:new-1 #project/lithos-loom"
    )


def test_task_create_full_form_passes_all_fields(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """All optional flags → forwarded to task_create + reflected in
    the rendered line."""
    config_path = _write_config(tmp_path)
    patched_lithos.task_create_returns = "task-7"

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Full form",
            "--brief",
            "Some context",
            "--scheduled",
            "2026-06-01",
            "--priority",
            "high",
            "--tags",
            "code-review, urgent",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    call = patched_lithos.task_create_calls[0]
    assert call["description"] == "Some context"
    assert call["tags"] == ["code-review", "urgent"]
    assert call["metadata"] == {
        "project": "lithos-loom",
        "priority": "high",
        "scheduled_for": "2026-06-01",
    }

    # Line carries priority emoji + scheduled date + project tag.
    line = result.stdout.strip()
    assert "⏫" in line
    assert "🆔 lithos:task-7" in line
    assert "📅 2026-06-01" in line
    assert "#project/lithos-loom" in line


def test_task_create_target_file_appends_line(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """``--target-file PATH`` writes the line to the file and prints
    nothing to stdout (US27 composable-with-daily-notes flow)."""
    config_path = _write_config(tmp_path)
    target = tmp_path / "daily" / "2026-05-22.md"

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Captured",
            "--target-file",
            str(target),
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip() == ""

    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert content.endswith("\n")
    assert "- [ ] Captured 🆔 lithos:new-1 #project/lithos-loom" in content


def test_task_create_no_insert_prints_task_id_only(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """``--no-insert`` (US27) prints just the task_id and discards the
    projected line. Scripted callers use this to capture the id from
    stdout without dealing with the line."""
    config_path = _write_config(tmp_path)
    patched_lithos.task_create_returns = "task-99"

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Scripted create",
            "--no-insert",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    # Task was still created upstream.
    assert len(patched_lithos.task_create_calls) == 1

    # Stdout is just the task_id — no projected-line markers.
    assert result.stdout.strip() == "task-99"
    assert "🆔" not in result.stdout
    assert "#project/" not in result.stdout


def test_task_create_no_insert_mutually_exclusive_with_target_file(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """Passing both ``--no-insert`` and ``--target-file`` is a usage
    error (exit 2); the CLI rejects before reaching Lithos so the
    operator's intent is unambiguous."""
    config_path = _write_config(tmp_path)
    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "x",
            "--no-insert",
            "--target-file",
            str(tmp_path / "out.md"),
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr
    assert patched_lithos.task_create_calls == []


def test_task_create_target_file_appends_to_existing(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """Existing target file is appended to, not overwritten."""
    config_path = _write_config(tmp_path)
    target = tmp_path / "inbox.md"
    target.write_text("existing line\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Appended",
            "--target-file",
            str(target),
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0
    content = target.read_text(encoding="utf-8")
    assert content.startswith("existing line\n")
    assert "Appended" in content


def test_task_create_strips_whitespace_from_tags(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """``"a, , b"`` → ``["a", "b"]``. Empty entries are dropped so
    operators don't accidentally tag tasks with empty strings."""
    config_path = _write_config(tmp_path)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Tag whitespace",
            "--tags",
            "  alpha , , beta ",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0
    call = patched_lithos.task_create_calls[0]
    assert call["tags"] == ["alpha", "beta"]


@pytest.mark.parametrize("enum_value", ["highest", "high", "medium", "low", "lowest"])
def test_task_create_all_priority_values_pass_through(
    tmp_path: Path,
    patched_lithos: type[_StubLithosClient],
    enum_value: str,
) -> None:
    """Every D18 enum value is accepted and forwarded verbatim."""
    config_path = _write_config(tmp_path)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "Priority probe",
            "--priority",
            enum_value,
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0
    call = patched_lithos.task_create_calls[0]
    assert call["metadata"]["priority"] == enum_value


# ── Validation errors ──────────────────────────────────────────────────


def test_task_create_unknown_project_exits_two(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """Slug in neither Lithos (note_list) nor TOML → exit 2, error names
    the known projects, no task created."""
    config_path = _write_config(tmp_path, projects=("lithos-loom",))
    # No Lithos project-context docs; only the TOML slug is known.

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "nonexistent",
            "--title",
            "x",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 2
    assert "unknown project 'nonexistent'" in result.stderr
    assert "lithos-loom" in result.stderr
    assert patched_lithos.task_create_calls == []


def test_task_create_accepts_lithos_only_project(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """The bug regression guard: a project created via the macro exists
    in Lithos but NOT in TOML [projects] — task create must accept it."""
    # TOML has a different project; the macro-created one is Lithos-only.
    config_path = _write_config(tmp_path, projects=("other-project",))
    patched_lithos.note_list_returns = [_project_summary("macro-made")]

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "macro-made",
            "--title",
            "Do the thing",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert len(patched_lithos.task_create_calls) == 1
    assert patched_lithos.task_create_calls[0]["metadata"] == {"project": "macro-made"}


def test_task_create_accepts_toml_only_project(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """Union leniency: a slug configured in TOML but with no Lithos
    project-context doc still validates (offline / local overlay)."""
    config_path = _write_config(tmp_path, projects=("local-only",))
    patched_lithos.note_list_returns = []  # nothing in Lithos

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "local-only",
            "--title",
            "x",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert len(patched_lithos.task_create_calls) == 1


def test_task_create_unknown_priority_exits_two(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """Non-D18 priority → exit 2 with a list of the valid enum values."""
    config_path = _write_config(tmp_path)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "x",
            "--priority",
            "urgent",  # not in PRIORITY_EMOJI
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 2
    assert "unknown priority 'urgent'" in result.stderr
    assert patched_lithos.task_create_calls == []


def test_task_create_missing_required_project(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """Typer's normal "missing required option" error path.

    Asserts on exit code (Typer uses 2 for usage errors) + the
    absence of a Lithos call rather than the text of Typer's error
    message — the latter gets word-wrapped to the terminal width on
    CI's narrow runner, where the option name doesn't make it into
    the rendered panel."""
    config_path = _write_config(tmp_path)
    result = runner.invoke(
        app,
        ["task", "create", "--title", "x", "--config", str(config_path)],
    )
    assert result.exit_code == 2
    assert patched_lithos.task_create_calls == []


def test_task_create_missing_required_title(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """Typer's normal "missing required option" error path.

    See :func:`test_task_create_missing_required_project` for the
    rationale behind asserting on exit code + no-Lithos-call rather
    than Typer's error text."""
    config_path = _write_config(tmp_path)
    result = runner.invoke(
        app,
        ["task", "create", "--project", "lithos-loom", "--config", str(config_path)],
    )
    assert result.exit_code == 2
    assert patched_lithos.task_create_calls == []


# ── Lithos / I/O failure surfacing ─────────────────────────────────────


def test_task_create_lithos_error_exits_one(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """A LithosClientError from task_create surfaces with exit 1 and
    a structured stderr message the macro can display."""
    from lithos_loom.errors import LithosClientError

    config_path = _write_config(tmp_path)
    patched_lithos.task_create_side_effect = LithosClientError(
        "invalid_input", "title cannot be empty"
    )

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "x",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 1
    assert "task_create failed" in result.stderr
    assert "title cannot be empty" in result.stderr


def test_task_create_connection_failure_exits_one(
    tmp_path: Path, patched_lithos: type[_StubLithosClient]
) -> None:
    """``OSError`` (e.g. Lithos daemon down) exits 1 with a connection-
    error message naming the configured URL."""
    config_path = _write_config(tmp_path)
    patched_lithos.task_create_side_effect = OSError("Connection refused")

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "x",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 1
    assert "could not reach Lithos" in result.stderr
    assert "http://localhost:8765" in result.stderr


def test_task_create_target_file_write_failure_exits_one(
    tmp_path: Path,
    patched_lithos: type[_StubLithosClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write failure on the target file exits 1 with a clear
    message. Simulated by patching ``_append_line`` to raise."""
    config_path = _write_config(tmp_path)

    def _boom(target: Path, line: str) -> None:
        raise PermissionError(f"denied: {target}")

    monkeypatch.setattr(cli_task_mod, "_append_line", _boom)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "--project",
            "lithos-loom",
            "--title",
            "x",
            "--target-file",
            str(tmp_path / "out.md"),
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 1
    assert "could not write to" in result.stderr


# ── JSON-format invariants (used by the Templater macro) ───────────────


def test_project_list_json_is_machine_parseable(tmp_path: Path) -> None:
    """``lithos-loom project list --format json`` returns clean JSON
    the macro can ``JSON.parse``. Tested here (CLI-level) so a future
    formatting change has to break this loudly.

    Pinned via ``--source toml`` because Slice 4 (D23) flipped the
    default to ``--source lithos`` (which requires a live Lithos
    connection). The macro itself can use either source — the JSON
    shape is invariant (array of slugs); this test just locks the
    machine-parseable contract."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            f"""
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            [projects.alpha]
            repo = "{repo}"
            """
        )
    )

    result = runner.invoke(
        app,
        [
            "project",
            "list",
            "--config",
            str(config_path),
            "--source",
            "toml",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed == ["alpha"]
