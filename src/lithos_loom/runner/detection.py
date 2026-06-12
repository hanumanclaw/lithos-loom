"""Auto-detection of test commands for common project types.

Salvaged from Ralph++ ``ralph_pp/detection.py`` with one deliberate divergence:
Ralph++ returned a single first-match command, but story-develop runs the test
gate inside a sandbox container whose image may lack a given tool (e.g. no
``make``), so this version returns an **ordered candidate list with
fallbacks** — Makefile target first, then language-ecosystem commands — and the
gate picks the first candidate whose tool exists in the image.

The Python branch prefers ``uv run pytest`` when a ``uv.lock`` is present:
``uv run`` bootstraps the project venv (dev group included) inside a fresh
container, whereas bare ``pytest`` is rarely on PATH.
"""

from __future__ import annotations

from pathlib import Path


def _makefile_has_test_target(repo_path: Path) -> bool:
    makefile = repo_path / "Makefile"
    if not makefile.is_file():
        return False
    try:
        text = makefile.read_text()
    except OSError:
        return False
    return "\ntest:" in text or "\ntest :" in text or text.startswith("test:")


def _python_test_command(repo_path: Path) -> str:
    """``uv run pytest`` for uv-managed projects, bare ``pytest`` otherwise."""
    if (repo_path / "uv.lock").is_file():
        return "uv run pytest"
    return "pytest"


def detect_test_commands(repo_path: Path) -> list[str]:
    """Detect candidate test commands for *repo_path*, best first.

    Returns an ordered list: a Makefile ``test`` target first (the project's
    own entrypoint), then the first matching language-ecosystem command as a
    fallback for environments without ``make``. Returns an empty list when
    nothing is found.
    """
    candidates: list[str] = []

    if _makefile_has_test_target(repo_path):
        candidates.append("make test")

    # Language-specific detectors in priority order; first match wins.
    detectors: list[tuple[Path, str | None, str]] = [
        # (marker file, extra-content check substring, command)
        (repo_path / "pytest.ini", None, _python_test_command(repo_path)),
        (repo_path / "setup.cfg", None, _python_test_command(repo_path)),
        (repo_path / "pyproject.toml", "[tool.pytest", _python_test_command(repo_path)),
        (repo_path / "package.json", None, "npm test"),
        (repo_path / "Cargo.toml", None, "cargo test"),
        (repo_path / "go.mod", None, "go test ./..."),
    ]
    for marker, content_check, cmd in detectors:
        if not marker.is_file():
            continue
        if content_check is not None:
            try:
                if content_check not in marker.read_text():
                    continue
            except OSError:
                continue
        if cmd not in candidates:
            candidates.append(cmd)
        break  # first ecosystem match only (avoid polyglot noise)

    return candidates
