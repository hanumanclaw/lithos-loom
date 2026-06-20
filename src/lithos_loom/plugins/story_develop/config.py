"""Resolved configuration + paths for a single ``story-develop`` run.

:class:`DevelopConfig` carries the coder, the reviewer panel, per-reviewer
severity thresholds, and usage-limit fallback chains — see
``docs/prd/archive/story-develop.md`` and SPECIFICATION.md §5.5.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

# A reviewer name becomes a Docker container name, a host dir, and a handoff
# filename, so it must be a safe slug (lowercase alphanumerics + hyphens,
# starting alphanumeric). This rejects spaces ("code quality") and path
# separators ("security/appsec") before they create invalid names / nested dirs.
_REVIEWER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def is_valid_reviewer_name(name: str) -> bool:
    """True if *name* is a safe slug for container / path / filename use."""
    return bool(_REVIEWER_NAME_RE.fullmatch(name))


# Image + container constants (ralph-sandbox; see ADR 0002 / feasibility gate).
DEFAULT_CODER_TOOL = "claude"
DEFAULT_REVIEWER_TOOL = "claude"
DEFAULT_REVIEWER_NAME = "code-quality"
DEFAULT_BLOCK_THRESHOLD = "major"  # findings below this don't block (see handoff.py)
DEFAULT_MAX_ROUNDS = 5  # T3 loop bound; stall/dispute/cost guards arrive with T7
DEFAULT_TEST_TIMEOUT = 900  # seconds for one test-gate container run (T4)
DEFAULT_MAX_PAUSE_MINUTES = 120  # T5: total usage-limit pause budget per run
DEFAULT_PAUSE_POLL_MINUTES = 5  # T5: retry cadence when the reset time is unknown
DEFAULT_IMAGE = "ralph-sandbox:latest"
# Open-file-descriptor limit for every story-develop container. runc's default
# soft RLIMIT_NOFILE is 1024; an FD-heavy test suite (e.g. one that loads an ML
# model per fixture) crosses that mid-run and every later fixture errors with
# EMFILE — a false RED unrelated to the code under test (#117). 65536 is ~50x the
# observed need (failure begins right at 1024) and stays under typical
# docker-daemon hard caps for portability. Passed as ``--ulimit nofile=soft:hard``.
CONTAINER_NOFILE_ULIMIT = "65536:65536"
WORKSPACE_MOUNT = "/workspace"
CLAUDE_CONFIG_MOUNT = "/claude_config"
# Codex (#94): the per-run config/transcript dir is `CODEX_HOME` (NOT
# `CODEX_CONFIG_DIR`, which codex ignores — feasibility gate). Mounted under the
# work-dir, never `/tmp`. Auth is a single `auth.json` (the codex analogue of
# claude's `.credentials.json`), bind-mounted RW for token refresh.
CODEX_CONFIG_MOUNT = "/codex_home"
# The single auth file bind-mounted from the operator's real config (RW, so the
# OAuth token refresh propagates) — never the whole ~/.claude, and NOT
# ``.claude.json`` (that is mutable user state, not auth; mounting the real one
# RW would let the container pollute the operator's live config). See the PRD
# "Run-state & session durability" section.
CLAUDE_AUTH_FILES = (".credentials.json",)
CODEX_AUTH_FILES = ("auth.json",)
HANDOFF_DIRNAME = ".handoff"


def _short_run_id() -> str:
    """8 hex chars; unique enough to namespace a run's tmux/containers/state."""
    return secrets.token_hex(4)


@dataclass(frozen=True)
class ReviewerSpec:
    """One named reviewer: its persona, strictness, and tooling (T6).

    ``block_threshold`` is per-reviewer — security typically blocks at
    ``minor`` while code-quality blocks at ``major`` (PRD decision #7).
    ``system_prompt`` is an optional focus brief injected into the reviewer's
    prompts. ``fallback_chain`` lists alternate tools tried when this
    reviewer's tool is usage-limited (T5).

    ``model`` / ``effort`` are per-reviewer (#93): a strong reviewer can run a
    more capable model + higher reasoning effort than a lenient one. ``None``
    means "inherit the agent's default" — see :class:`DevelopConfig` for why we
    do not hard-pin a model string. ``effort`` is one of :data:`VALID_EFFORTS`.
    """

    name: str
    tool: str = DEFAULT_REVIEWER_TOOL
    block_threshold: str = DEFAULT_BLOCK_THRESHOLD
    system_prompt: str | None = None
    fallback_chain: tuple[str, ...] = ()
    model: str | None = None
    effort: str | None = None


_VALID_THRESHOLDS = ("critical", "major", "minor")

_REVIEWER_ENTRY_KEYS = {
    "name",
    "tool",
    "block_threshold",
    "system_prompt",
    "fallback_chain",
    "model",
    "effort",
}


def parse_model(value: object, *, where: str) -> str | None:
    """Validate + normalise a ``model`` value: a non-empty string, or ``None``.

    Shared by the reviewer-entry parser, the standalone CLI, and the
    daemon-mode coder lookup so every surface rejects the same garbage
    (empty / non-string) identically. The returned string is **stripped** —
    validating on ``.strip()`` but returning the raw value would let
    ``" opus "`` pass and then reach the CLI as an invalid model id. Raises
    :class:`ValueError`.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: model must be a non-empty string (got {value!r})")
    return value.strip()


def parse_image(value: object, *, where: str) -> str | None:
    """Validate + normalise a sandbox container ``image`` value, or ``None``.

    Mirrors :func:`parse_model`: a non-empty string (stripped) or ``None``
    (meaning "inherit the route-level ``--image`` / built-in default"). Like a
    model id, an image reference (e.g. ``ralph-sandbox:latest``,
    ``ghcr.io/acme/dev@sha256:…``) is not validated against a catalogue — it
    just has to be a non-empty string; a bad ref surfaces when ``docker run``
    fails. Shared by the project-context metadata loader and the per-task
    override so both surfaces reject the same garbage identically. Raises
    :class:`ValueError`.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: image must be a non-empty string (got {value!r})")
    return value.strip()


def parse_test_command(value: object, *, where: str) -> str | None:
    """Validate + normalise a gate ``test_command`` override, or ``None``.

    Mirrors :func:`parse_image`: a non-empty string (stripped), or ``None``
    (meaning "inherit the route-level ``--test-command`` / auto-detection").
    The command is **trusted as-is** by the gate (no parsing, no tool-probe —
    see ``_resolve_test_command``), so the only validation is non-empty-string;
    a bad command surfaces when the gate container runs it. Shared by the
    project-metadata loader and the per-task override so both reject the same
    garbage identically. Raises :class:`ValueError`.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{where}: test_command must be a non-empty string (got {value!r})"
        )
    return value.strip()


def parse_bool_setting(value: object, *, where: str) -> bool | None:
    """Validate a boolean develop setting (``develop_test_gate`` etc.), or ``None``.

    Accepts a real ``bool`` or ``None``; **rejects ints and strings** so a
    mistyped flag (TOML ``1`` / ``"true"`` are NOT booleans) frictions rather
    than silently coercing — ``isinstance(1, bool)`` is ``False``, so only
    literal ``true`` / ``false`` pass. Raises :class:`ValueError`.
    """
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{where}: must be a boolean true/false (got {value!r})")
    return value


# Reasoning-effort levels. There is no universal cross-tool effort vocabulary:
# Claude's `--effort` is low/medium/high/xhigh/max; Codex has NO effort flag
# (depth is implicit in model choice — o3 vs gpt-4o); OpenCode's `--variant` is
# high/max/minimal. So Loom adopts CLAUDE'S levels as canonical (Claude is the
# only wired agent today). When other tools land (#94), each tool's
# `build_exec_command` maps this canonical level onto that tool's mechanism
# (Codex: pick the model; OpenCode: map to a `--variant`), coercing as needed.
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def parse_effort(value: object, *, where: str) -> str | None:
    """Validate + normalise a reasoning-effort level, or ``None``.

    Accepts one of :data:`VALID_EFFORTS` (case-insensitive, whitespace
    stripped); ``None`` means "inherit the agent/tool's own default effort"
    (which is model-dependent and may drift — we don't pin it). Raises
    :class:`ValueError`.
    """
    if value is None:
        return None
    norm = value.strip().lower() if isinstance(value, str) else value
    if norm not in VALID_EFFORTS:
        raise ValueError(
            f"{where}: effort must be one of {VALID_EFFORTS} (got {value!r})"
        )
    return norm  # type: ignore[return-value]  # membership check proves it's str


def parse_reviewer_entry(entry: object, *, where: str) -> ReviewerSpec:
    """Validate one reviewer mapping into a :class:`ReviewerSpec`.

    Shared by the ``--develop-config`` TOML loader and the daemon-mode
    project-context metadata loader (T10) so both surfaces enforce the
    identical schema. *where* labels the entry in error messages
    (e.g. ``"config.toml: reviewers[2]"``). Raises :class:`ValueError`.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"{where} is not a table/object")
    unknown = set(entry) - _REVIEWER_ENTRY_KEYS
    if unknown:
        raise ValueError(f"{where} has unknown keys {sorted(unknown)}")
    name = entry.get("name", "")
    if not isinstance(name, str) or not is_valid_reviewer_name(name):
        raise ValueError(
            f"{where}: name {name!r} must be a lowercase alphanumeric-and-hyphens slug"
        )
    threshold = entry.get("block_threshold", DEFAULT_BLOCK_THRESHOLD)
    if threshold not in _VALID_THRESHOLDS:
        raise ValueError(
            f"{where}: block_threshold must be one of "
            f"{_VALID_THRESHOLDS} (got {threshold!r})"
        )
    chain = entry.get("fallback_chain", [])
    if not isinstance(chain, list) or not all(isinstance(t, str) for t in chain):
        raise ValueError(f"{where}: fallback_chain must be a list of strings")
    system_prompt = entry.get("system_prompt")
    if system_prompt is not None and not isinstance(system_prompt, str):
        raise ValueError(f"{where}: system_prompt must be a string")
    tool = entry.get("tool", DEFAULT_REVIEWER_TOOL)
    if not isinstance(tool, str):
        raise ValueError(f"{where}: tool must be a string")
    # Field-qualify the location so a bad value points at the exact key
    # (e.g. ``develop_reviewers[1].model``), matching the coder-path breadcrumbs.
    model = parse_model(entry.get("model"), where=f"{where}.model")
    effort = parse_effort(entry.get("effort"), where=f"{where}.effort")
    return ReviewerSpec(
        name=name,
        tool=tool,
        block_threshold=threshold,
        system_prompt=system_prompt,
        fallback_chain=tuple(chain),
        model=model,
        effort=effort,
    )


def load_develop_config(path: Path) -> tuple[ReviewerSpec, ...]:
    """Parse a ``--develop-config`` TOML file into reviewer specs.

    Schema::

        [[reviewers]]
        name = "code-quality"          # required, safe slug
        block_threshold = "major"      # optional
        tool = "claude"                # optional
        system_prompt = "Focus on..."  # optional
        fallback_chain = ["codex"]     # optional

    Raises :class:`ValueError` with an operator-actionable message on any
    schema problem — never half-loads.
    """
    import tomllib

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read develop config {path}: {exc}") from exc

    raw = data.get("reviewers")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: expected at least one [[reviewers]] table")
    specs: list[ReviewerSpec] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw, start=1):
        spec = parse_reviewer_entry(entry, where=f"{path}: reviewers[{i}]")
        if spec.name in seen:
            raise ValueError(f"{path}: duplicate reviewer name {spec.name!r}")
        seen.add(spec.name)
        specs.append(spec)
    return tuple(specs)


@dataclass(frozen=True)
class DevelopConfig:
    """Everything ``develop()`` needs for one run.

    Paths under ``work_dir`` are derived lazily so the dataclass stays a plain
    value object: ``run_dir``/``coder_config_dir``/``worktree_parent``.
    """

    repo: Path
    description: str
    work_dir: Path
    coder: str = DEFAULT_CODER_TOOL
    # Coder model + reasoning effort (#93). ``None`` = inherit the agent's
    # default. We deliberately do NOT hard-pin a model string here: a pin
    # chosen today goes stale and couples the plugin to a model's lifecycle
    # (an upgrade would need a code release). Reproducibility is instead served
    # by letting the operator pin via project metadata / CLI and by recording
    # the resolved choice with the run. Per-reviewer model/effort live on
    # ``ReviewerSpec``; this pair is the coder's. ``effort`` is a level string
    # (:data:`VALID_EFFORTS`, Claude's canonical levels), not a token budget;
    # each tool's ``build_exec_command`` translates it to that tool's mechanism
    # when other tools land (#94) — see VALID_EFFORTS for why it's not universal.
    coder_model: str | None = None
    coder_effort: str | None = None
    image: str = DEFAULT_IMAGE
    base_branch: str = "main"
    # Single-reviewer convenience fields (the T2-era surface; still the
    # default path). T6: `reviewers` holds full multi-reviewer specs and,
    # when non-empty, takes precedence — see `effective_reviewers`.
    reviewer: str = DEFAULT_REVIEWER_NAME
    reviewer_tool: str = DEFAULT_REVIEWER_TOOL
    block_threshold: str = DEFAULT_BLOCK_THRESHOLD
    reviewers: tuple[ReviewerSpec, ...] = ()
    # T3: how many implement→review→fix rounds before we stop unapproved.
    max_rounds: int = DEFAULT_MAX_ROUNDS
    # T4 / #131: deterministic gate per round commit — an ordered check-set run
    # in throwaway containers; the default set is the single `test` check.
    test_gate: bool = True  # #131/ADR §10: scopes the `test` check (False = exclude it)
    test_command: str | None = None  # explicit `test`-check command; beats detection
    block_on_red: bool = (
        False  # ADR §10: the `test` check's block flag (RED blocks + feeds coder)
    )
    test_timeout: int = DEFAULT_TEST_TIMEOUT
    # T5: usage-limit reaction. The pause budget is shared across the run;
    # the fallback chain lists ALTERNATE reviewer tools tried in order when
    # the current one is usage-limited (empty = no alternate -> pause).
    max_pause_minutes: int = DEFAULT_MAX_PAUSE_MINUTES
    pause_poll_minutes: int = DEFAULT_PAUSE_POLL_MINUTES
    reviewer_fallback_chain: tuple[str, ...] = ()
    # T7: total agent-spend ceiling for the run (None = unlimited).
    max_cost_usd: float | None = None
    acceptance_criteria: str | None = None
    run_id: str = field(default_factory=_short_run_id)
    # #113: GitHub login to request as a reviewer (or assign, when they authored
    # the PR) on delivery, so native notifications fire. None → Copilot only.
    notify_github_login: str | None = None
    # Host path to the operator's claude config dir (source of the auth file).
    claude_config_dir: Path = field(default_factory=lambda: Path.home() / ".claude")
    # Host path to the operator's codex config dir (source of `auth.json`, #94).
    codex_config_dir: Path = field(default_factory=lambda: Path.home() / ".codex")

    @property
    def effective_reviewers(self) -> tuple[ReviewerSpec, ...]:
        """The run's reviewer panel.

        Explicit ``reviewers`` specs win; otherwise the single-reviewer
        convenience fields are folded into one spec (the T2-era behaviour).
        """
        if self.reviewers:
            return self.reviewers
        return (
            ReviewerSpec(
                name=self.reviewer,
                tool=self.reviewer_tool,
                block_threshold=self.block_threshold,
                fallback_chain=self.reviewer_fallback_chain,
            ),
        )

    @property
    def effective_acceptance_criteria(self) -> str:
        """The "definition of done" shown to the reviewer.

        T2 falls back to the task description; an explicit ``--acceptance-criteria``
        surface is wired in T8/T12.
        """
        return self.acceptance_criteria or self.description

    @property
    def run_dir(self) -> Path:
        """Per-run state root: ``<work_dir>/<run_id>``."""
        return self.work_dir / self.run_id

    @property
    def coder_config_dir(self) -> Path:
        """Per-run coder config dir (CLAUDE_CONFIG_DIR target; holds transcript)."""
        return self.run_dir / "agents" / "coder" / "claude_config"

    def reviewer_config_dir(self, name: str) -> Path:
        """Per-run, per-reviewer config dir (its own CLAUDE_CONFIG_DIR / transcript)."""
        return self.run_dir / "agents" / f"review-{name}" / "claude_config"

    @property
    def worktree_parent(self) -> Path:
        """Where the run's worktree directory is created."""
        return self.run_dir / "worktree"

    @property
    def handoff_dir(self) -> Path:
        """Per-run handoff dir, mounted into the container at ``/workspace/.handoff``.

        Lives *outside* the git worktree so the worktree stays clean (the
        handoff is a separate artifact, not part of the deliverable branch).
        """
        return self.run_dir / "handoff"

    @property
    def gate_dir(self) -> Path:
        """Per-run root for test-gate state (exported trees, output, cache)."""
        return self.run_dir / "test_gate"

    @property
    def failures_dir(self) -> Path:
        """Per-run dir of failed-turn fixtures (the G4 capture harness)."""
        return self.run_dir / "failures"

    @property
    def operator_skills_dir(self) -> Path | None:
        """Operator's ``~/.claude/skills`` if present (mounted read-only).

        Restores the feasibility-gate G2 behaviour: operator-installed skills
        are available to the agent inside the per-run ``CLAUDE_CONFIG_DIR``.
        Claude-only — codex has no skill concept (it honours the worktree
        ``AGENTS.md`` instead), so codex agents pass ``skills_dir=None``.
        """
        skills = self.claude_config_dir / "skills"
        return skills if skills.is_dir() else None

    def auth_source_dir(self, tool: str) -> Path:
        """Operator config dir that holds *tool*'s auth file (#94).

        ``codex`` reads ``auth.json`` from :attr:`codex_config_dir`; every other
        tool (claude) reads from :attr:`claude_config_dir`.
        """
        return self.codex_config_dir if tool == "codex" else self.claude_config_dir
