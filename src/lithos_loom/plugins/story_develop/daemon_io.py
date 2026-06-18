"""Daemon-mode plumbing for ``story-develop`` (T10, PRD Phase 3).

Three concerns, all pure-ish and unit-testable:

* :func:`read_task_payload` — parse the runner's ``task.json``
  (``{"task": {...event payload...}}``) into the same
  :class:`~.lithos_io.TaskContext` the standalone ``--task-id`` path uses,
  so the rest of the plugin cannot tell the modes apart.
* :func:`resolve_project_settings` — the PRD "Daemon config lookup
  contract": a daemon-mode run loads its reviewer config ITSELF from the
  project-context doc's metadata (``develop_reviewers`` /
  ``develop_default_reviewers`` / ``develop_coder`` /
  ``develop_fallback_chain`` / ceilings), because ``--task-json`` carries
  the task, not resolved project config. Every miss degrades to the
  built-in default (a single ``code-quality`` reviewer) plus a
  ``[Friction]`` breadcrumb — a missing or stale link must never block
  development.
* :func:`build_result_payload` — map a :class:`~.develop.DevelopResult`
  onto the runner's ``result.json`` contract (``docs/result-schema.json``):
  ``approved`` → ``succeeded``; ``interrupted`` → ``interrupted`` with an
  ``error.category="usage_limited"`` and a ``resume`` block carrying
  ``resume_after`` + session ids (the runner schedules a re-dispatch);
  everything else → ``failed``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...lithos_client import LithosClient
from .config import (
    DEFAULT_CODER_TOOL,
    DEFAULT_REVIEWER_NAME,
    ReviewerSpec,
    parse_effort,
    parse_model,
    parse_reviewer_entry,
)
from .lithos_io import AGENT_ID, TaskContext

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from .develop import DevelopResult

logger = logging.getLogger(__name__)

# Exit codes per the result.json contract (docs/result-schema.json):
# 0=succeeded, 1=generic failure, 20=bad input/config (do not retry),
# 30=interrupted.
EXIT_SUCCEEDED = 0
EXIT_FAILED = 1
EXIT_BAD_INPUT = 20
EXIT_INTERRUPTED = 30

BUILTIN_REVIEWERS: tuple[ReviewerSpec, ...] = (
    ReviewerSpec(name=DEFAULT_REVIEWER_NAME),
)


def read_task_payload(path: Path) -> TaskContext:
    """Parse the runner's ``task.json`` into a :class:`TaskContext`.

    Raises :class:`ValueError` on a malformed file — the plugin exits
    ``EXIT_BAD_INPUT`` without a result file, which the runner surfaces as
    a contract violation (correct: there is no task id to report against).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read task json {path}: {exc}") from exc
    task = data.get("task") if isinstance(data, dict) else None
    if not isinstance(task, dict):
        raise ValueError(f"{path}: expected a top-level 'task' object")
    task_id = str(task.get("id") or "")
    if not task_id:
        raise ValueError(f"{path}: task has no id")
    title = str(task.get("title") or "")
    if not title:
        raise ValueError(f"{path}: task {task_id} has no title")
    metadata = task.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    ac = metadata.get("acceptance_criteria")
    return TaskContext(
        task_id=task_id,
        title=title,
        description=str(task.get("description") or ""),
        acceptance_criteria=ac if isinstance(ac, str) and ac.strip() else None,
        metadata=dict(metadata),
    )


# --- project-context config lookup ------------------------------------------


@dataclass(frozen=True)
class ProjectDevelopSettings:
    """The resolved per-project develop config for one daemon-mode run.

    ``frictions`` carries operator breadcrumbs accumulated during
    resolution (missing slug/doc, unknown reviewer names, …) for the
    caller to post as ``[Friction]`` findings — resolution itself never
    fails the run.
    """

    reviewers: tuple[ReviewerSpec, ...] = BUILTIN_REVIEWERS
    coder: str = DEFAULT_CODER_TOOL
    coder_model: str | None = None
    coder_effort: str | None = None
    fallback_chain: tuple[str, ...] = ()
    max_rounds: int | None = None
    max_cost_usd: float | None = None
    frictions: tuple[str, ...] = ()


def _context_doc_path(slug: str) -> str:
    return f"projects/{slug}/{slug}-project-context.md"


async def _fetch_context_metadata(
    client: LithosClient, slug: str
) -> Mapping[str, Any] | None:
    """The project-context doc's metadata, or ``None`` when no doc exists.

    Mirrors the importer's resolution: the canonical path first, then the
    lexicographically-smallest ``project-context``-tagged doc under
    ``projects/<slug>/``.
    """
    note = await client.note_read(path=_context_doc_path(slug))
    if note is not None:
        return note.metadata
    candidates = await client.note_list(
        path_prefix=f"projects/{slug}/", tags=["project-context"]
    )
    if not candidates:
        return None
    fallback = min(candidates, key=lambda n: n.path)
    return fallback.metadata


def _parse_pool(
    meta: Mapping[str, Any], frictions: list[str]
) -> dict[str, ReviewerSpec]:
    """``develop_reviewers`` → name-keyed pool; invalid entries are skipped."""
    raw = meta.get("develop_reviewers")
    if raw is None:
        return {}
    if not isinstance(raw, list):
        frictions.append("develop_reviewers is not a list; ignoring the pool")
        return {}
    pool: dict[str, ReviewerSpec] = {}
    for i, entry in enumerate(raw, start=1):
        try:
            spec = parse_reviewer_entry(entry, where=f"develop_reviewers[{i}]")
        except ValueError as exc:
            frictions.append(f"skipping invalid reviewer entry: {exc}")
            continue
        if spec.name in pool:
            frictions.append(f"duplicate reviewer {spec.name!r} in pool; keeping first")
            continue
        pool[spec.name] = spec
    return pool


def _select_reviewers(
    pool: dict[str, ReviewerSpec],
    meta: Mapping[str, Any],
    task_metadata: Mapping[str, Any],
    frictions: list[str],
) -> tuple[ReviewerSpec, ...]:
    """PRD contract steps 4–5: per-task override > project default > built-in.

    A populated pool WITHOUT a selection still resolves to the built-in
    single reviewer — opting a reviewer into the pool does not auto-run it.
    Unknown names are skipped with friction; an empty effective selection
    falls back to the built-in default.
    """
    raw = task_metadata.get("reviewers")
    source = "task metadata.reviewers"
    if not isinstance(raw, list) or not raw:
        raw = meta.get("develop_default_reviewers")
        source = "develop_default_reviewers"
    if not isinstance(raw, list) or not raw:
        return BUILTIN_REVIEWERS
    selected: list[ReviewerSpec] = []
    for name in raw:
        spec = pool.get(name) if isinstance(name, str) else None
        if spec is None:
            frictions.append(f"{source} names unknown reviewer {name!r}; skipping")
            continue
        if spec not in selected:
            selected.append(spec)
    if not selected:
        frictions.append(f"{source} resolved to no known reviewers; using built-in")
        return BUILTIN_REVIEWERS
    return tuple(selected)


def resolve_project_settings(
    url: str, task_metadata: Mapping[str, Any]
) -> ProjectDevelopSettings:
    """Resolve the daemon-mode run's develop config (PRD lookup contract).

    Never raises: every failure mode — no project slug, no context doc,
    Lithos unreachable, malformed metadata — degrades to the built-in
    defaults with a friction breadcrumb for the caller to post.
    """
    frictions: list[str] = []
    slug = task_metadata.get("project")
    if not isinstance(slug, str) or not slug.strip():
        frictions.append(
            "task has no metadata.project slug; using built-in develop defaults"
        )
        return ProjectDevelopSettings(frictions=tuple(frictions))

    async def _fetch() -> Mapping[str, Any] | None:
        async with LithosClient(url, agent_id=AGENT_ID) as client:
            return await _fetch_context_metadata(client, slug)

    try:
        meta = asyncio.run(_fetch())
    except Exception as exc:
        frictions.append(
            f"cannot read project-context doc for {slug!r} ({exc}); "
            "using built-in develop defaults"
        )
        return ProjectDevelopSettings(frictions=tuple(frictions))
    if meta is None:
        frictions.append(
            f"no project-context doc for {slug!r}; using built-in develop defaults"
        )
        return ProjectDevelopSettings(frictions=tuple(frictions))

    pool = _parse_pool(meta, frictions)
    reviewers = _select_reviewers(pool, meta, task_metadata, frictions)

    coder = DEFAULT_CODER_TOOL
    coder_model: str | None = None
    coder_effort: str | None = None
    raw_coder = meta.get("develop_coder")
    if isinstance(raw_coder, dict):
        raw_tool = raw_coder.get("tool")
        if isinstance(raw_tool, str):
            coder = raw_tool
        elif raw_tool is not None:
            frictions.append("develop_coder.tool must be a string; using default")
        # model/effort are optional within develop_coder (#93); each is
        # validated independently so one bad value doesn't drop the other.
        try:
            coder_model = parse_model(
                raw_coder.get("model"), where="develop_coder.model"
            )
        except ValueError as exc:
            frictions.append(f"{exc}; ignoring")
        try:
            coder_effort = parse_effort(
                raw_coder.get("effort"), where="develop_coder.effort"
            )
        except ValueError as exc:
            frictions.append(f"{exc}; ignoring")
    elif raw_coder is not None:
        frictions.append(
            "develop_coder must be an object with optional tool/model/effort; ignoring"
        )

    # Per-task override (#93): a task flags "this one is cheap / needs deep
    # reasoning" by pinning the CODER's model/effort. Reviewer models stay
    # project policy (per-reviewer in develop_reviewers) — a blanket per-task
    # downgrade must never silently weaken a strict security reviewer.
    if task_metadata.get("develop_model") is not None:
        try:
            coder_model = parse_model(
                task_metadata["develop_model"], where="task metadata.develop_model"
            )
        except ValueError as exc:
            frictions.append(f"{exc}; keeping project default")
    if task_metadata.get("develop_effort") is not None:
        try:
            coder_effort = parse_effort(
                task_metadata["develop_effort"],
                where="task metadata.develop_effort",
            )
        except ValueError as exc:
            frictions.append(f"{exc}; keeping project default")

    raw_chain = meta.get("develop_fallback_chain")
    chain: tuple[str, ...] = ()
    if isinstance(raw_chain, list) and all(isinstance(t, str) for t in raw_chain):
        chain = tuple(raw_chain)
    elif raw_chain is not None:
        frictions.append("develop_fallback_chain must be a list of strings; ignoring")

    max_rounds = meta.get("develop_max_rounds")
    if max_rounds is not None and (not isinstance(max_rounds, int) or max_rounds < 1):
        frictions.append(f"develop_max_rounds {max_rounds!r} invalid; ignoring")
        max_rounds = None

    max_cost = meta.get("develop_max_cost_usd")
    if max_cost is not None and (
        not isinstance(max_cost, (int, float)) or max_cost <= 0
    ):
        frictions.append(f"develop_max_cost_usd {max_cost!r} invalid; ignoring")
        max_cost = None

    return ProjectDevelopSettings(
        reviewers=reviewers,
        coder=coder,
        coder_model=coder_model,
        coder_effort=coder_effort,
        fallback_chain=chain,
        max_rounds=max_rounds,
        max_cost_usd=float(max_cost) if max_cost is not None else None,
        frictions=tuple(frictions),
    )


def apply_cli_fallbacks(
    settings: ProjectDevelopSettings,
    *,
    coder_model: str | None,
    coder_effort: str | None,
    reviewer_model: str | None,
    reviewer_effort: str | None,
) -> ProjectDevelopSettings:
    """Layer route-level CLI model/effort flags UNDER the resolved settings.

    Daemon mode has no per-agent CLI surface (``--reviewer`` / ``--develop-config``
    are rejected), so these flags are blanket route-level fallbacks (#93): project
    metadata always wins, and a flag fills only what metadata left unset — the
    coder's model/effort, and each reviewer's. A bad flag value drops with a
    ``[Friction]`` breadcrumb (never errors — daemon config resolution must not
    fail the run, nor flow an invalid value through to the agent). Returns a new
    settings object with the merged frictions.
    """
    frictions = list(settings.frictions)

    def _validate(raw: object, parser, where: str):  # type: ignore[no-untyped-def]
        if raw is None:
            return None
        try:
            return parser(raw, where=where)
        except ValueError as exc:
            frictions.append(f"{exc}; ignoring the route fallback")
            return None

    # Validate EVERY provided fallback flag up front so a malformed route value
    # is surfaced with a [Friction] even when metadata already supplies that
    # field — a route-config typo (`--coder-model opuss`, `--reviewer-effort
    # hgh`) must not stay silently masked until metadata changes later. A valid
    # fallback is then APPLIED only where metadata left the field unset.
    v_coder_m = _validate(coder_model, parse_model, "--coder-model")
    v_coder_e = _validate(coder_effort, parse_effort, "--coder-effort")
    v_rev_m = _validate(reviewer_model, parse_model, "--reviewer-model")
    v_rev_e = _validate(reviewer_effort, parse_effort, "--reviewer-effort")

    coder_m = settings.coder_model if settings.coder_model is not None else v_coder_m
    coder_e = settings.coder_effort if settings.coder_effort is not None else v_coder_e

    reviewers = settings.reviewers
    if v_rev_m is not None or v_rev_e is not None:
        reviewers = tuple(
            replace(
                spec,
                model=spec.model if spec.model is not None else v_rev_m,
                effort=spec.effort if spec.effort is not None else v_rev_e,
            )
            for spec in reviewers
        )

    return replace(
        settings,
        coder_model=coder_m,
        coder_effort=coder_e,
        reviewers=reviewers,
        frictions=tuple(frictions),
    )


def load_tool_default_models() -> tuple[dict[str, str], tuple[str, ...]]:
    """The host loom config's ``[story_develop].default_models`` (best-effort).

    Re-reads the same loom config the daemon runs under — the plugin subprocess
    inherits the daemon's env + CWD, so :func:`~lithos_loom.config.find_config_path`
    resolves the identical file. Never raises: an unreadable / missing config
    degrades to an empty mapping with a friction breadcrumb so the run proceeds
    on agent defaults. A config that simply has no ``[story_develop]`` section
    is the normal case and yields no friction.
    """
    from ...config import load_config

    try:
        cfg = load_config()
    except Exception as exc:
        return {}, (
            f"cannot load loom config for per-tool default models ({exc}); "
            "using agent defaults",
        )
    if cfg.story_develop is None:
        return {}, ()
    return dict(cfg.story_develop.default_models), ()


def apply_tool_default_models(
    settings: ProjectDevelopSettings, default_models: Mapping[str, str]
) -> ProjectDevelopSettings:
    """Fill each agent's model from the per-tool global default where still unset.

    The lowest-priority model layer (the loom TOML ``[story_develop]`` section):
    applied AFTER project metadata, per-task overrides, and the route-level
    ``--coder-model`` / ``--reviewer-model`` fallbacks, and keyed by each
    agent's RESOLVED tool — so a heterogeneous panel (#94) has the coder and
    every reviewer pick up the default for *their* own tool, and a tool with no
    configured default leaves that agent on its CLI default. A no-op when
    *default_models* is empty.
    """
    if not default_models:
        return settings
    coder_model = settings.coder_model
    if coder_model is None:
        coder_model = default_models.get(settings.coder)
    reviewers = tuple(
        replace(
            spec,
            model=(
                spec.model if spec.model is not None else default_models.get(spec.tool)
            ),
        )
        for spec in settings.reviewers
    )
    return replace(settings, coder_model=coder_model, reviewers=reviewers)


def post_frictions(url: str, task_id: str, frictions: tuple[str, ...]) -> None:
    """Post config-resolution breadcrumbs as one ``[Friction]`` finding.

    Best-effort: a posting failure is logged, never raised — the breadcrumbs
    also land in the daemon log either way.
    """
    if not frictions:
        return
    summary = "[Friction] story-develop config resolution:\n" + "\n".join(
        f"- {f}" for f in frictions
    )

    async def _post() -> None:
        async with LithosClient(url, agent_id=AGENT_ID) as client:
            await client.finding_post(task_id=task_id, summary=summary)

    try:
        asyncio.run(_post())
    except Exception as exc:
        logger.warning(
            "story-develop: posting friction finding to task %s failed: %s",
            task_id,
            exc,
        )


# --- result.json construction ------------------------------------------------


def _reviewer_sessions(run_dir: Path) -> dict[str, str]:
    """Reviewer session ids from the run's ``state.json`` (empty on any miss)."""
    try:
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw = state.get("reviewers")
    if not isinstance(raw, dict):
        return {}
    return {
        name: entry["session"]
        for name, entry in raw.items()
        if isinstance(entry, dict) and isinstance(entry.get("session"), str)
    }


def build_result_payload(
    result: DevelopResult,
    *,
    task_id: str,
    started_at: datetime,
    finished_at: datetime,
    run_dir: Path,
) -> tuple[dict[str, Any], int]:
    """Map a :class:`DevelopResult` onto the result.json contract.

    Returns ``(payload, exit_code)``. ``approved`` is the only success —
    the runner completes the task on ``succeeded``. ``interrupted`` carries
    the ``resume`` block (the runner schedules a re-dispatch at
    ``resume_after``); every other stop (``max_rounds`` / ``stalled`` /
    ``disputed`` / ``cost_exceeded`` / ``failed``) maps to ``failed`` —
    they all need a human to look before another run is worth its spend.
    """
    if result.approved:
        status, exit_code = "succeeded", EXIT_SUCCEEDED
        error: dict[str, Any] | None = None
    elif result.status == "interrupted":
        status, exit_code = "interrupted", EXIT_INTERRUPTED
        error = {
            "category": "usage_limited",
            "message": result.message,
            "retriable": True,
        }
    else:
        status, exit_code = "failed", EXIT_FAILED
        error = {"category": "agent", "message": result.message}

    payload: dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id,
        "status": status,
        "exit_code": exit_code,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "worktree": str(result.worktree),
        "commits": list(result.commits),
        "error": error,
    }
    if result.conversation_log is not None:
        payload["artifacts"] = {"conversation_log": str(result.conversation_log)}
    if status == "interrupted" and result.resume_after is not None:
        resume: dict[str, Any] = {
            "resume_after": result.resume_after.isoformat(timespec="seconds"),
            "run_id": result.run_id,
        }
        if result.coder_session:
            resume["coder_session"] = result.coder_session
        sessions = _reviewer_sessions(run_dir)
        if sessions:
            resume["reviewer_sessions"] = sessions
        payload["resume"] = resume
    return payload, exit_code
