"""Configuration loading for lithos-loom.

Loom is configured by a TOML file (typically ``~/.config/lithos-loom/config.toml``).
This module defines the in-memory representation of that file, validates it on load,
and applies environment-variable overrides so that env beats file beats default.

Per-environment configs are supported via ``LITHOS_LOOM_ENVIRONMENT``: when set to
e.g. ``dev`` or ``prod``, the loader looks for ``config.<env>.toml`` first before
falling back to plain ``config.toml``. This lets a single workstation host multiple
Loom configurations (e.g. one targeting a production Lithos, one targeting a
local-development Lithos) and switch between them by exporting one env var.

The TOML schema is documented in ``docs/SPECIFICATION.md`` §3.1; the shape is:

    [orchestrator]
    agent_id = "lithos-orchestrator-<host>"
    lithos_url = "http://localhost:8765"
    work_dir = "/tmp/lithos-loom"
    max_concurrency = 4

    [projects.<name>]
    repo = "/path/to/local/repo"
    claude_config = "/path/to/.claude-lithos"
    codex_config = "/path/to/.codex-lithos"

    [[routes]]
    name = "<route-name>"
    command = "<plugin invocation>"
    [routes.match]
    tags = ["<tag>", ...]
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast, overload

from dotenv import load_dotenv

from lithos_loom.errors import ConfigError

__all__ = [
    "DEFAULT_CONFIG_FILENAME",
    "DEFAULT_GITHUB_WATCHER_COORD_DOC",
    "DEFAULT_GITHUB_WATCHER_POLL_INTERVAL",
    "DEFAULT_GITHUB_WATCHER_RECONCILE_INTERVAL_MINUTES",
    "DEFAULT_GITHUB_WATCHER_RESOLVED_REPLAY_DAYS",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_OBSIDIAN_PROJECTS_DIR",
    "DEFAULT_OBSIDIAN_RESOLVED_TTL_DAYS",
    "DEFAULT_OBSIDIAN_TASKS_FILE",
    "DEFAULT_WORK_DIR",
    "Backoff",
    "GitHubWatcherConfig",
    "LogLevel",
    "LoomConfig",
    "ObsidianSyncConfig",
    "OnPersistentFailure",
    "OrchestratorConfig",
    "ProjectConfig",
    "RetryPolicy",
    "RouteConfig",
    "RouteMatch",
    "SubscriptionConfig",
    "find_config_path",
    "load_config",
    "parse_log_level",
]

# ── Literal types + validators ─────────────────────────────────────────

LogLevel = Literal["debug", "info", "warning", "error"]
Backoff = Literal["exponential", "linear"]
OnPersistentFailure = Literal["friction", "ignore"]

_VALID_LOG_LEVEL: set[str] = {"debug", "info", "warning", "error"}
_VALID_BACKOFF: set[str] = {"exponential", "linear"}
_VALID_ON_PERSISTENT_FAILURE: set[str] = {"friction", "ignore"}


# ── Defaults ───────────────────────────────────────────────────────────

DEFAULT_CONFIG_FILENAME = "config.toml"
DEFAULT_WORK_DIR = Path("/tmp/lithos-loom")
DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_LOG_LEVEL: LogLevel = "info"
DEFAULT_OBSIDIAN_TASKS_FILE = Path("_lithos/tasks.md")
DEFAULT_OBSIDIAN_RESOLVED_TTL_DAYS = 7
DEFAULT_OBSIDIAN_PROJECTS_DIR = Path("_lithos/projects")
DEFAULT_GITHUB_WATCHER_POLL_INTERVAL = 60
DEFAULT_GITHUB_WATCHER_COORD_DOC = (
    "projects/_lithos-loom-internal/github-watcher-state.md"
)
DEFAULT_GITHUB_WATCHER_RESOLVED_REPLAY_DAYS = 7
DEFAULT_GITHUB_WATCHER_RECONCILE_INTERVAL_MINUTES = 60


def parse_log_level(value: str) -> LogLevel:
    """Validate and narrow a string to a ``LogLevel`` literal."""
    if value not in _VALID_LOG_LEVEL:
        raise ConfigError(
            f"Invalid log level {value!r}. Valid values: {sorted(_VALID_LOG_LEVEL)}"
        )
    return cast(LogLevel, value)


# ── Dataclasses ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OrchestratorConfig:
    agent_id: str
    lithos_url: str
    work_dir: Path = DEFAULT_WORK_DIR
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    log_level: LogLevel = DEFAULT_LOG_LEVEL
    retain_failed_workdirs: bool = True


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    repo: Path
    claude_config: Path | None = None
    codex_config: Path | None = None


@dataclass(frozen=True)
class RouteMatch:
    tags: tuple[str, ...]


@dataclass(frozen=True)
class RouteConfig:
    name: str
    command: str
    match: RouteMatch
    human_blocking: bool = False
    """Whether this route requires human action.

    Read by ``is_human_actionable``: a task whose tags match a route
    with ``human_blocking=True`` is projected into the operator's
    Obsidian view; routes with ``human_blocking=False`` are treated as
    autonomous (the daemon will handle them; hide from operator).
    """
    max_runtime_seconds: int | None = None


@dataclass(frozen=True)
class RetryPolicy:
    """Per-subscription retry shape.

    ``initial_delay_seconds`` and ``max_delay_seconds`` are the bounds for
    the chosen backoff curve. ``exponential`` doubles each attempt up to
    ``max_delay_seconds``; ``linear`` adds ``initial_delay_seconds``.
    """

    attempts: int = 5
    backoff: Backoff = "exponential"
    initial_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0


@dataclass(frozen=True)
class SubscriptionConfig:
    name: str
    event_types: tuple[str, ...]
    action: str
    match: Mapping[str, Any] | None = None
    where: str | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    on_persistent_failure: OnPersistentFailure = "friction"


@dataclass(frozen=True)
class ObsidianSyncConfig:
    """Vault-host configuration for the obsidian-sync child.

    Presence of this section on a host's TOML declares "this is the
    vault host." The supervisor uses ``cfg.obsidian_sync is not None``
    as its spawn gate; operators omit the section on headless hosts.

    ``tasks_file`` is stored as a relative path and joined with
    ``vault_path`` only at use time. Existence of the vault is not
    checked at parse time — that's ``lithos-loom doctor``.

    Projection filter knobs (``include_blocked``, ``exclude_tags``) are
    operator-level controls that ``is_human_actionable`` reads alongside
    the route-author's ``human_blocking`` flag.
    """

    vault_path: Path
    tasks_file: Path = field(default=DEFAULT_OBSIDIAN_TASKS_FILE)
    resolved_ttl_days: int = DEFAULT_OBSIDIAN_RESOLVED_TTL_DAYS
    include_blocked: bool = True
    """Project tasks whose ``metadata.depends_on`` is non-empty.
    Operators who don't want blocked work in their daily view can set
    this to ``false``."""
    exclude_tags: tuple[str, ...] = ()
    """Tags whose presence on a task suppresses projection. Generic
    operator-level denylist; matched against ``task.tags`` membership."""
    projects_dir: Path = field(default=DEFAULT_OBSIDIAN_PROJECTS_DIR)
    """Where the project-context projection writes per-project docs
    under the vault. Default ``_lithos/projects`` mirrors the Lithos-side
    ``knowledge/projects/<slug>/<filename>.md`` layout one-to-one so
    the slug + filename map straight across. Stored as a relative path;
    joined with ``vault_path`` only at use time (same shape as
    ``tasks_file``)."""


@dataclass(frozen=True)
class GitHubWatcherConfig:
    """Per-host gate for the github-issue-watcher child.

    Presence of this section with ``enabled = true`` declares "this host
    runs the watcher". Only one host should have it enabled at a time
    (D50); multi-host coordination is the operator's responsibility.

    ``coord_doc_path`` is the Lithos doc the watcher uses to persist its
    per-repo ``updated_at`` cursors. Defaults to a daemon-owned doc under
    ``projects/_lithos-loom-internal/`` so the project-context-projection
    picks it up read-only for visibility.
    """

    enabled: bool = False
    poll_interval_seconds: int = DEFAULT_GITHUB_WATCHER_POLL_INTERVAL
    coord_doc_path: str = DEFAULT_GITHUB_WATCHER_COORD_DOC
    resolved_replay_days: int = DEFAULT_GITHUB_WATCHER_RESOLVED_REPLAY_DAYS
    """How far back the LithosEventStream replays terminal task events at
    bootstrap. Closes a Lithos task while the watcher is down → the next
    daemon start replays the ``task.completed`` event and the push handler
    closes the corresponding GH issue. The handler is idempotent so a
    too-large window only costs extra harmless re-checks; the default
    (7 days) tracks the obsidian-sync TTL convention. Set to 0 to disable
    replay (push handler only fires for events that arrive while the
    watcher is live).
    """
    reconcile_interval_minutes: int = DEFAULT_GITHUB_WATCHER_RECONCILE_INTERVAL_MINUTES
    """Cadence of the periodic Lithos→GH reconciliation sweep.

    PR-review finding 4 (round 5, 2026-05-30): the push consumer's
    in-memory retry budget tops out at ~3 minutes (waits
    2/4/8/16/32/60/60 s ≈ 182 s). A GH outage longer than that drops
    the event entirely, with recovery only on next daemon restart
    inside ``resolved_replay_days``. The sweep closes that gap while
    the daemon keeps running: every interval (default 60 min), scan
    Lithos for open + recently-resolved tasks carrying
    ``metadata.github_issue_url`` and replay each one through the push
    handler. The handler is idempotent (re-fetches GH before PATCH)
    so the sweep is harmless when everything is already in sync. Set
    to 0 to disable the sweep entirely.
    """


@dataclass(frozen=True)
class LoomConfig:
    orchestrator: OrchestratorConfig
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    routes: tuple[RouteConfig, ...] = ()
    subscriptions: tuple[SubscriptionConfig, ...] = ()
    obsidian_sync: ObsidianSyncConfig | None = None
    github_watcher: GitHubWatcherConfig | None = None
    source_path: Path | None = None
    environment: str | None = None


# ── Discovery and loading ──────────────────────────────────────────────


def _config_dir() -> Path:
    """Return the user's lithos-loom config directory.

    Honours ``XDG_CONFIG_HOME`` per the XDG base-directory spec; falls back
    to ``~/.config/lithos-loom``.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "lithos-loom"


def _default_config_candidates(environment: str | None) -> list[Path]:
    """Return the filesystem candidates checked when ``LITHOS_LOOM_CONFIG`` is unset.

    Discovery order:

    1. ``./<config>.toml`` in CWD (project-local override)
    2. ``$XDG_CONFIG_HOME/lithos-loom/<config>.toml``

    where ``<config>`` is ``config.<environment>`` if ``environment`` is set,
    else ``config``. Per-environment configs let a single workstation host
    multiple Loom setups (dev / prod / etc) selectable via env var.
    """
    stem = f"config.{environment}" if environment else "config"
    filename = f"{stem}.toml"

    return [
        Path.cwd() / filename,
        _config_dir() / filename,
    ]


def find_config_path() -> Path:
    """Locate the active config file via env var, env-named lookup, or default name.

    Order:

    1. ``LITHOS_LOOM_CONFIG`` env var (explicit path)
    2. If ``LITHOS_LOOM_ENVIRONMENT`` is set, search for ``config.<env>.toml``
    3. Otherwise search for plain ``config.toml``
    """
    load_dotenv()
    explicit = os.environ.get("LITHOS_LOOM_CONFIG", "")
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise ConfigError(
                f"LITHOS_LOOM_CONFIG points at {p}, but no file exists there"
            )
        return p

    environment = os.environ.get("LITHOS_LOOM_ENVIRONMENT") or None
    candidates = _default_config_candidates(environment)
    for p in candidates:
        if p.exists():
            return p

    joined = "\n  ".join(str(p) for p in candidates)
    env_note = f" (LITHOS_LOOM_ENVIRONMENT={environment})" if environment else ""
    raise ConfigError(
        f"No lithos-loom config found{env_note}. "
        f"Set LITHOS_LOOM_CONFIG or create one of:\n  " + joined
    )


def load_config(path: Path | None = None) -> LoomConfig:
    """Load, validate, and return a :class:`LoomConfig`.

    When ``path`` is ``None`` the file is located via :func:`find_config_path`.
    Env-var overrides are applied after parsing.
    """
    load_dotenv()
    config_path = path if path is not None else find_config_path()
    environment = os.environ.get("LITHOS_LOOM_ENVIRONMENT") or None

    try:
        with config_path.open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"Could not read {config_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path}: invalid TOML: {exc}") from exc

    orchestrator = _parse_orchestrator(raw.get("orchestrator", {}), config_path)
    projects = _parse_projects(raw.get("projects", {}), config_path)
    routes = _parse_routes(raw.get("routes", []), config_path)
    subscriptions = _parse_subscriptions(raw.get("subscriptions", []), config_path)
    obsidian_sync = _parse_obsidian_sync(raw.get("obsidian_sync"), config_path)
    github_watcher = _parse_github_watcher(raw.get("github_watcher"), config_path)

    cfg = LoomConfig(
        orchestrator=orchestrator,
        projects=projects,
        routes=routes,
        subscriptions=subscriptions,
        obsidian_sync=obsidian_sync,
        github_watcher=github_watcher,
        source_path=config_path,
        environment=environment,
    )
    return _apply_env_overrides(cfg)


# ── Internal parsing helpers ───────────────────────────────────────────


def _parse_orchestrator(data: Any, config_path: Path) -> OrchestratorConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [orchestrator] must be a table")
    agent_id = _required_str(data, "agent_id", config_path, "orchestrator")
    lithos_url = _required_str(data, "lithos_url", config_path, "orchestrator")
    work_dir = _optional_path(
        data, "work_dir", DEFAULT_WORK_DIR, config_path, "orchestrator"
    )
    max_concurrency = _optional_int(
        data, "max_concurrency", DEFAULT_MAX_CONCURRENCY, config_path, "orchestrator"
    )
    log_level_raw = data.get("log_level", DEFAULT_LOG_LEVEL)
    if not isinstance(log_level_raw, str):
        raise ConfigError(f"{config_path}: orchestrator.log_level must be a string")
    log_level = parse_log_level(log_level_raw)
    retain_failed = _optional_bool(
        data, "retain_failed_workdirs", True, config_path, "orchestrator"
    )
    return OrchestratorConfig(
        agent_id=agent_id,
        lithos_url=lithos_url,
        work_dir=work_dir,
        max_concurrency=max_concurrency,
        log_level=log_level,
        retain_failed_workdirs=retain_failed,
    )


def _parse_projects(data: Any, config_path: Path) -> dict[str, ProjectConfig]:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [projects] must be a table")
    out: dict[str, ProjectConfig] = {}
    for name, entry in data.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"{config_path}: [projects.{name}] must be a table")
        repo = _required_path(entry, "repo", config_path, f"projects.{name}")
        claude_config = _optional_path(
            entry, "claude_config", None, config_path, f"projects.{name}"
        )
        codex_config = _optional_path(
            entry, "codex_config", None, config_path, f"projects.{name}"
        )
        out[name] = ProjectConfig(
            name=name,
            repo=repo,
            claude_config=claude_config,
            codex_config=codex_config,
        )
    return out


def _parse_routes(data: Any, config_path: Path) -> tuple[RouteConfig, ...]:
    if not isinstance(data, list):
        raise ConfigError(f"{config_path}: [[routes]] must be an array of tables")
    routes: list[RouteConfig] = []
    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ConfigError(f"{config_path}: routes[{idx}] must be a table")
        scope = f"routes[{idx}]"
        name = _required_str(entry, "name", config_path, scope)
        command = _required_str(entry, "command", config_path, scope)
        match_raw = entry.get("match", {})
        if not isinstance(match_raw, dict):
            raise ConfigError(f"{config_path}: {scope}.match must be a table")
        tags_raw = match_raw.get("tags", [])
        if not isinstance(tags_raw, list) or not all(
            isinstance(t, str) for t in tags_raw
        ):
            raise ConfigError(
                f"{config_path}: {scope}.match.tags must be a list of strings"
            )
        max_runtime = entry.get("max_runtime_seconds")
        if max_runtime is not None and not isinstance(max_runtime, int):
            raise ConfigError(
                f"{config_path}: {scope}.max_runtime_seconds must be an integer"
            )
        human_blocking = _optional_bool(
            entry, "human_blocking", False, config_path, scope
        )
        routes.append(
            RouteConfig(
                name=name,
                command=command,
                match=RouteMatch(tags=tuple(tags_raw)),
                human_blocking=human_blocking,
                max_runtime_seconds=max_runtime,
            )
        )
    return tuple(routes)


def _parse_subscriptions(
    data: Any, config_path: Path
) -> tuple[SubscriptionConfig, ...]:
    if not isinstance(data, list):
        raise ConfigError(
            f"{config_path}: [[subscriptions]] must be an array of tables"
        )
    out: list[SubscriptionConfig] = []
    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ConfigError(f"{config_path}: subscriptions[{idx}] must be a table")
        scope = f"subscriptions[{idx}]"
        name = _required_str(entry, "name", config_path, scope)
        action = _required_str(entry, "action", config_path, scope)
        on_raw = entry.get("on")
        if isinstance(on_raw, str):
            event_types: tuple[str, ...] = (on_raw,)
        elif isinstance(on_raw, list) and all(isinstance(x, str) for x in on_raw):
            event_types = tuple(on_raw)
        else:
            raise ConfigError(
                f"{config_path}: {scope}.on must be a string or a list of strings"
            )
        if not event_types:
            raise ConfigError(
                f"{config_path}: {scope}.on must list at least one event type"
            )

        match_raw = entry.get("match")
        if match_raw is not None and not isinstance(match_raw, dict):
            raise ConfigError(f"{config_path}: {scope}.match must be a table")
        match: Mapping[str, Any] | None = match_raw if match_raw else None

        where_raw = entry.get("where")
        if where_raw is not None and not isinstance(where_raw, str):
            raise ConfigError(f"{config_path}: {scope}.where must be a string")
        where: str | None = where_raw

        retry = _parse_retry_policy(entry.get("retry"), config_path, scope)

        opf_raw = entry.get("on_persistent_failure", "friction")
        if opf_raw not in _VALID_ON_PERSISTENT_FAILURE:
            raise ConfigError(
                f"{config_path}: {scope}.on_persistent_failure must be one of "
                f"{sorted(_VALID_ON_PERSISTENT_FAILURE)} (got {opf_raw!r})"
            )
        opf = cast(OnPersistentFailure, opf_raw)

        out.append(
            SubscriptionConfig(
                name=name,
                event_types=event_types,
                action=action,
                match=match,
                where=where,
                retry=retry,
                on_persistent_failure=opf,
            )
        )
    return tuple(out)


_OBSIDIAN_SYNC_KEYS: frozenset[str] = frozenset(
    {
        "vault_path",
        "tasks_file",
        "resolved_ttl_days",
        "include_blocked",
        "exclude_tags",
        "projects_dir",
    }
)


def _parse_obsidian_sync(data: Any, config_path: Path) -> ObsidianSyncConfig | None:
    """Parse the optional ``[obsidian_sync]`` section.

    Absence of the section is the supervisor's spawn gate: returning
    ``None`` means no obsidian-sync child is spawned on this host.
    """
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [obsidian_sync] must be a table")

    unknown = set(data.keys()) - _OBSIDIAN_SYNC_KEYS
    if unknown:
        raise ConfigError(
            f"{config_path}: [obsidian_sync] has unknown key(s) "
            f"{sorted(unknown)}; valid keys: {sorted(_OBSIDIAN_SYNC_KEYS)}"
        )

    vault_path = _required_path(data, "vault_path", config_path, "obsidian_sync")

    tasks_file_raw = data.get("tasks_file")
    if tasks_file_raw is None:
        tasks_file = DEFAULT_OBSIDIAN_TASKS_FILE
    else:
        if not isinstance(tasks_file_raw, str) or not tasks_file_raw:
            raise ConfigError(
                f"{config_path}: obsidian_sync.tasks_file must be a non-empty "
                f"path string"
            )
        tasks_file = Path(tasks_file_raw)
        if tasks_file.is_absolute() or any(part == ".." for part in tasks_file.parts):
            raise ConfigError(
                f"{config_path}: obsidian_sync.tasks_file must be relative to "
                f"vault_path and may not contain '..' (got {tasks_file_raw!r})"
            )

    resolved_ttl_days = _optional_int(
        data,
        "resolved_ttl_days",
        DEFAULT_OBSIDIAN_RESOLVED_TTL_DAYS,
        config_path,
        "obsidian_sync",
    )
    if resolved_ttl_days < 0:
        raise ConfigError(
            f"{config_path}: obsidian_sync.resolved_ttl_days must be >= 0 "
            f"(got {resolved_ttl_days})"
        )

    include_blocked = _optional_bool(
        data, "include_blocked", True, config_path, "obsidian_sync"
    )

    exclude_tags_raw = data.get("exclude_tags", [])
    if not isinstance(exclude_tags_raw, list) or not all(
        isinstance(t, str) for t in exclude_tags_raw
    ):
        raise ConfigError(
            f"{config_path}: obsidian_sync.exclude_tags must be a list of strings"
        )
    if any(not t for t in exclude_tags_raw):
        raise ConfigError(
            f"{config_path}: obsidian_sync.exclude_tags entries must be non-empty"
        )

    projects_dir_raw = data.get("projects_dir")
    if projects_dir_raw is None:
        projects_dir = DEFAULT_OBSIDIAN_PROJECTS_DIR
    else:
        if not isinstance(projects_dir_raw, str) or not projects_dir_raw:
            raise ConfigError(
                f"{config_path}: obsidian_sync.projects_dir must be a non-empty "
                f"path string"
            )
        projects_dir = Path(projects_dir_raw)
        if projects_dir.is_absolute() or any(
            part == ".." for part in projects_dir.parts
        ):
            raise ConfigError(
                f"{config_path}: obsidian_sync.projects_dir must be relative to "
                f"vault_path and may not contain '..' (got {projects_dir_raw!r})"
            )

    return ObsidianSyncConfig(
        vault_path=vault_path,
        tasks_file=tasks_file,
        resolved_ttl_days=resolved_ttl_days,
        include_blocked=include_blocked,
        exclude_tags=tuple(exclude_tags_raw),
        projects_dir=projects_dir,
    )


_GITHUB_WATCHER_KEYS: frozenset[str] = frozenset(
    {
        "enabled",
        "poll_interval_seconds",
        "coord_doc_path",
        "resolved_replay_days",
        "reconcile_interval_minutes",
    }
)


def _parse_github_watcher(data: Any, config_path: Path) -> GitHubWatcherConfig | None:
    """Parse the optional ``[github_watcher]`` section.

    Absence of the section means no watcher child is spawned on this host.
    The supervisor's spawn gate further requires ``enabled = true`` so an
    operator can park the section in the file while turned off.
    """
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [github_watcher] must be a table")

    unknown = set(data.keys()) - _GITHUB_WATCHER_KEYS
    if unknown:
        raise ConfigError(
            f"{config_path}: [github_watcher] has unknown key(s) "
            f"{sorted(unknown)}; valid keys: {sorted(_GITHUB_WATCHER_KEYS)}"
        )

    enabled = _optional_bool(data, "enabled", False, config_path, "github_watcher")

    poll_interval = _optional_int(
        data,
        "poll_interval_seconds",
        DEFAULT_GITHUB_WATCHER_POLL_INTERVAL,
        config_path,
        "github_watcher",
    )
    if poll_interval < 1:
        raise ConfigError(
            f"{config_path}: github_watcher.poll_interval_seconds must be >= 1 "
            f"(got {poll_interval})"
        )

    coord_doc_raw = data.get("coord_doc_path", DEFAULT_GITHUB_WATCHER_COORD_DOC)
    if not isinstance(coord_doc_raw, str) or not coord_doc_raw:
        raise ConfigError(
            f"{config_path}: github_watcher.coord_doc_path must be a non-empty string"
        )
    # The coord doc lives in Lithos under projects/<...>/<file>.md; reject
    # paths with absolute prefixes or '..' parts that wouldn't address a
    # Lithos doc.
    coord_doc_path = Path(coord_doc_raw)
    has_dotdot = any(part == ".." for part in coord_doc_path.parts)
    if coord_doc_path.is_absolute() or has_dotdot:
        raise ConfigError(
            f"{config_path}: github_watcher.coord_doc_path must be a relative "
            f"Lithos doc path and may not contain '..' (got {coord_doc_raw!r})"
        )

    resolved_replay_days = _optional_int(
        data,
        "resolved_replay_days",
        DEFAULT_GITHUB_WATCHER_RESOLVED_REPLAY_DAYS,
        config_path,
        "github_watcher",
    )
    if resolved_replay_days < 0:
        raise ConfigError(
            f"{config_path}: github_watcher.resolved_replay_days must be >= 0 "
            f"(got {resolved_replay_days})"
        )

    reconcile_interval = _optional_int(
        data,
        "reconcile_interval_minutes",
        DEFAULT_GITHUB_WATCHER_RECONCILE_INTERVAL_MINUTES,
        config_path,
        "github_watcher",
    )
    if reconcile_interval < 0:
        raise ConfigError(
            f"{config_path}: github_watcher.reconcile_interval_minutes must be "
            f">= 0 (got {reconcile_interval})"
        )

    return GitHubWatcherConfig(
        enabled=enabled,
        poll_interval_seconds=poll_interval,
        coord_doc_path=coord_doc_raw,
        resolved_replay_days=resolved_replay_days,
        reconcile_interval_minutes=reconcile_interval,
    )


def _parse_retry_policy(data: Any, config_path: Path, scope: str) -> RetryPolicy:
    if data is None:
        return RetryPolicy()
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: {scope}.retry must be a table")
    inner_scope = f"{scope}.retry"
    attempts = _optional_int(data, "attempts", 5, config_path, inner_scope)
    if attempts < 1:
        raise ConfigError(f"{config_path}: {inner_scope}.attempts must be >= 1")
    backoff_raw = data.get("backoff", "exponential")
    if backoff_raw not in _VALID_BACKOFF:
        raise ConfigError(
            f"{config_path}: {inner_scope}.backoff must be one of "
            f"{sorted(_VALID_BACKOFF)} (got {backoff_raw!r})"
        )
    backoff = cast(Backoff, backoff_raw)
    initial = _optional_float(
        data, "initial_delay_seconds", 0.5, config_path, inner_scope
    )
    if initial < 0:
        raise ConfigError(
            f"{config_path}: {inner_scope}.initial_delay_seconds must be >= 0 "
            f"(got {initial})"
        )
    max_delay = _optional_float(
        data, "max_delay_seconds", 30.0, config_path, inner_scope
    )
    if max_delay < 0:
        raise ConfigError(
            f"{config_path}: {inner_scope}.max_delay_seconds must be >= 0 "
            f"(got {max_delay})"
        )
    if max_delay < initial:
        raise ConfigError(
            f"{config_path}: {inner_scope}.max_delay_seconds ({max_delay}) "
            f"must be >= initial_delay_seconds ({initial})"
        )
    return RetryPolicy(
        attempts=attempts,
        backoff=backoff,
        initial_delay_seconds=initial,
        max_delay_seconds=max_delay,
    )


def _optional_float(
    d: dict[str, Any], key: str, default: float, path: Path, scope: str
) -> float:
    raw = d.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConfigError(f"{path}: {scope}.{key} must be a number")
    return float(raw)


def _apply_env_overrides(cfg: LoomConfig) -> LoomConfig:
    """Apply env-var overrides for the small set of always-overridable fields.

    Uses ``dataclasses.replace`` rather than re-constructing both dataclasses
    explicitly so a future field added to ``LoomConfig`` or
    ``OrchestratorConfig`` can't be silently dropped here.
    """
    url = os.environ.get("LITHOS_URL", "")
    if not url:
        return cfg
    return replace(cfg, orchestrator=replace(cfg.orchestrator, lithos_url=url))


def _required_str(d: dict[str, Any], key: str, path: Path, scope: str) -> str:
    value = d.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{path}: {scope}.{key} must be a non-empty string")
    return value


def _required_path(d: dict[str, Any], key: str, path: Path, scope: str) -> Path:
    raw = d.get(key)
    if not isinstance(raw, str) or not raw:
        raise ConfigError(f"{path}: {scope}.{key} must be a non-empty path string")
    return Path(raw).expanduser()


def _optional_int(
    d: dict[str, Any], key: str, default: int, path: Path, scope: str
) -> int:
    raw = d.get(key, default)
    if not isinstance(raw, int):
        raise ConfigError(f"{path}: {scope}.{key} must be an integer")
    return raw


def _optional_bool(
    d: dict[str, Any], key: str, default: bool, path: Path, scope: str
) -> bool:
    raw = d.get(key, default)
    if not isinstance(raw, bool):
        raise ConfigError(f"{path}: {scope}.{key} must be a boolean")
    return raw


@overload
def _optional_path(
    d: dict[str, Any], key: str, default: Path, path: Path, scope: str
) -> Path: ...


@overload
def _optional_path(
    d: dict[str, Any], key: str, default: None, path: Path, scope: str
) -> Path | None: ...


def _optional_path(
    d: dict[str, Any],
    key: str,
    default: Path | None,
    path: Path,
    scope: str,
) -> Path | None:
    raw = d.get(key)
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw:
        raise ConfigError(f"{path}: {scope}.{key} must be a non-empty path string")
    return Path(raw).expanduser()
