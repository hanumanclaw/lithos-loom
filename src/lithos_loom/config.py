"""Configuration loading for lithos-loom.

Loom is configured by a TOML file (typically ``~/.config/lithos-loom/config.toml``).
This module defines the in-memory representation of that file, validates it on load,
and applies environment-variable overrides so that env beats file beats default.

Per-environment configs are supported via ``LITHOS_LOOM_ENVIRONMENT``: when set to
e.g. ``dev`` or ``prod``, the loader looks for ``config.<env>.toml`` first before
falling back to plain ``config.toml``. This lets a single workstation host multiple
Loom configurations (e.g. one targeting a production Lithos, one targeting a
local-development Lithos) and switch between them by exporting one env var.

The TOML schema follows ``docs/prd/mvp.md`` US-4:

    [orchestrator]
    agent_id = "lithos-orchestrator-<host>"
    lithos_url = "http://localhost:8765"
    poll_interval_seconds = 30
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast, overload

from dotenv import load_dotenv

from lithos_loom.errors import ConfigError

__all__ = [
    "DEFAULT_CONFIG_FILENAME",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_WORK_DIR",
    "LogLevel",
    "LoomConfig",
    "OrchestratorConfig",
    "ProjectConfig",
    "RouteConfig",
    "RouteMatch",
    "find_config_path",
    "load_config",
    "parse_log_level",
]

# ── Literal types + validators ─────────────────────────────────────────

LogLevel = Literal["debug", "info", "warning", "error"]

_VALID_LOG_LEVEL: set[str] = {"debug", "info", "warning", "error"}


# ── Defaults ───────────────────────────────────────────────────────────

DEFAULT_CONFIG_FILENAME = "config.toml"
DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_WORK_DIR = Path("/tmp/lithos-loom")
DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_LOG_LEVEL: LogLevel = "info"


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
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
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
    max_runtime_seconds: int | None = None  # US-34


@dataclass(frozen=True)
class LoomConfig:
    orchestrator: OrchestratorConfig
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    routes: tuple[RouteConfig, ...] = ()
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

    cfg = LoomConfig(
        orchestrator=orchestrator,
        projects=projects,
        routes=routes,
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
    poll_interval = _optional_int(
        data,
        "poll_interval_seconds",
        DEFAULT_POLL_INTERVAL_SECONDS,
        config_path,
        "orchestrator",
    )
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
        poll_interval_seconds=poll_interval,
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
        routes.append(
            RouteConfig(
                name=name,
                command=command,
                match=RouteMatch(tags=tuple(tags_raw)),
                max_runtime_seconds=max_runtime,
            )
        )
    return tuple(routes)


def _apply_env_overrides(cfg: LoomConfig) -> LoomConfig:
    """Apply env-var overrides for the small set of always-overridable fields."""
    url = os.environ.get("LITHOS_URL", "")
    if not url:
        return cfg
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id=cfg.orchestrator.agent_id,
            lithos_url=url,
            poll_interval_seconds=cfg.orchestrator.poll_interval_seconds,
            work_dir=cfg.orchestrator.work_dir,
            max_concurrency=cfg.orchestrator.max_concurrency,
            log_level=cfg.orchestrator.log_level,
            retain_failed_workdirs=cfg.orchestrator.retain_failed_workdirs,
        ),
        projects=cfg.projects,
        routes=cfg.routes,
        source_path=cfg.source_path,
        environment=cfg.environment,
    )


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
