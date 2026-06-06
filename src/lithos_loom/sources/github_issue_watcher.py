"""GitHubIssueWatcher — polling source for the github-issue-watcher feature.

Slice 7.1 of ``docs/prd/github-issue-watcher.md``. Polls watched GitHub
repos on a timer, emits one ``github.issue.seen`` event per issue onto
the in-process bus, and persists per-repo ``updated_at`` cursors in a
Lithos coord doc so daemon restart doesn't re-walk every open issue.

Architecture mirrors :class:`LithosNoteStream` for the reconnect /
backoff shape but the work loop is poll-driven rather than SSE-driven:
there's no GitHub server-push surface that doesn't require a public
ingress (the PRD risk notes call this out — webhooks are deferred).

The watcher's watch list (which Lithos slugs map to which repos) is
derived from project-context metadata:

- ``github_watch_enabled = true`` on a project-context doc enables
  watching for that project.
- ``github_repos`` (a list of ``owner/name`` strings) carries the repo
  mappings — a project may track several repos. The CLI subcommands
  (``add-github-repo`` / ``remove-github-repo`` / ``enable-github`` /
  ``disable-github``) manage this metadata.

Mid-run, the watcher subscribes to ``lithos.note.{created,updated}``
on the in-process bus so an operator who runs ``project enable-github
<slug>`` doesn't have to restart the daemon for the watch list to pick
up the change.

Cursor persistence:

The coord doc's body is human-readable text — one line per repo, of
the form ``<owner>/<name> <iso-timestamp>``. Plain text avoids
introducing a YAML/JSON parser at this layer and the doc stays
operator-readable in the vault when the project-context-projection
picks it up. The coord doc is created lazily on first poll.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.cli._github_metadata import (
    GITHUB_WATCH_KEY,
    extract_exclude_authors,
    extract_exclude_labels,
    extract_github_repos,
)
from lithos_loom.errors import LithosClientError
from lithos_loom.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubRepoNotFoundError,
    Issue,
)
from lithos_loom.lithos_client import Note, NoteSummary, WriteResult

__all__ = [
    "GITHUB_ISSUE_EVENT_TYPE",
    "GitHubIssueWatcher",
    "WatchedRepo",
    "WatcherLithosClient",
    "format_cursors",
    "parse_cursors",
]


@dataclass(frozen=True)
class WatchedRepo:
    """Per-project watcher state derived from the project-context doc.

    ``repos`` is the project's ``owner/name`` mappings (one or more — a
    project may track several repos). ``exclude_labels`` and
    ``exclude_authors`` are the metadata-derived import filters that the
    sync handler uses to drop noise from automated issue creators
    (dependabot, renovate) before ``task_create``; they apply to every
    repo the project tracks. The filters are immutable per refresh cycle
    so a concurrent watch-list rebuild can't reshape them under a
    running poll.
    """

    repos: tuple[str, ...]
    exclude_labels: tuple[str, ...] = ()
    exclude_authors: tuple[str, ...] = ()


logger = logging.getLogger(__name__)

GITHUB_ISSUE_EVENT_TYPE = "github.issue.seen"
"""Bus event type emitted for every issue seen during a poll.

One event per issue per poll — the subscription handler decides whether
to create/update/close the corresponding Lithos task based on the
linkage marker + the issue's own state. Funnelling create+update through
one type means the source doesn't have to look up prior state."""

_COORD_DOC_TITLE = "GitHub Watcher State"
_COORD_DOC_BODY_HEADER = (
    "Daemon-owned coordination doc. Do not edit by hand —\n"
    "the github-issue-watcher overwrites this file on every successful poll.\n\n"
    "Format: one line per watched repo, '<owner>/<name> <ISO-8601 cursor>'.\n"
)
_BUS_QUEUE_SIZE = 256
_MAX_COORD_DOC_CAS_ATTEMPTS = 3

# GitHub's ``since=`` filter is inclusive (>=). We persist the
# observed-max ``updated_at`` verbatim and accept that the boundary
# issue is re-fetched on the next poll: the sync handler is idempotent
# (marker → open-task path no-ops, drift compare short-circuits) so a
# bounded replay costs at most one extra task_list call per repo per
# poll. The earlier ``+1 second`` nudge avoided that cost but
# silently dropped any *other* issue updated within the same wall
# second as the boundary — the wrong tradeoff for a correctness-
# critical inbound mirror (PR-review finding 3).


class WatcherLithosClient(Protocol):
    """Minimum Lithos surface the watcher source depends on.

    Pulled out as a Protocol so tests can pass a stub without
    constructing a real ``LithosClient`` + transport.
    """

    async def note_list(
        self,
        *,
        path_prefix: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
        metadata_match: dict[str, Any] | None = None,
    ) -> list[NoteSummary]: ...

    async def note_read(
        self, *, id: str | None = None, path: str | None = None
    ) -> Note | None: ...

    async def note_write(
        self,
        *,
        agent: str | None = None,
        title: str,
        content: str,
        tags: list[str] | None = None,
        note_type: str = "concept",
        path: str | None = None,
        id: str | None = None,
        expected_version: int | None = None,
        status: str | None = None,
    ) -> WriteResult: ...


# ── Cursor doc parser ──────────────────────────────────────────────────


_STUCK_PREFIX = "stuck:"


def parse_cursors(body: str) -> dict[str, datetime]:
    """Parse the coord doc body into a ``{repo: cursor}`` map.

    Tolerates blank lines, comment lines (anything not matching the
    ``owner/name <iso>`` shape is skipped with a debug log) and either
    UTC ``Z`` or explicit ``+00:00`` timezone suffixes.

    Stuck-issue rows (``stuck:owner/name#42``) are recognised but
    skipped here — parse them with :func:`parse_stuck`.

    Returns an empty dict for a fresh / unparseable doc — that's
    indistinguishable from "first poll" and the caller falls through
    to a full re-walk per repo.
    """
    out: dict[str, datetime] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("Daemon"):
            continue
        if line.startswith(_STUCK_PREFIX):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        repo, cursor_raw = parts
        if "/" not in repo:
            continue
        try:
            cursor = _parse_iso(cursor_raw)
        except ValueError:
            logger.debug("github-watcher: ignoring unparseable cursor line %r", line)
            continue
        out[repo] = cursor
    return out


def parse_stuck(body: str) -> dict[str, set[int]]:
    """Parse ``stuck:owner/name#<number>`` rows out of the coord doc.

    PR-review finding 3 (round 5, 2026-05-30): the stuck-issue set is
    persisted alongside cursors so a daemon restart between an
    incomplete reconciliation (e.g. ``task_create`` succeeded, marker
    PATCH failed) and the next retry doesn't lose the repair record.
    """
    out: dict[str, set[int]] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith(_STUCK_PREFIX):
            continue
        rest = line[len(_STUCK_PREFIX) :]
        if "#" not in rest:
            continue
        repo, num_str = rest.rsplit("#", 1)
        if "/" not in repo:
            continue
        try:
            number = int(num_str)
        except ValueError:
            continue
        out.setdefault(repo, set()).add(number)
    return out


def format_cursors(
    cursors: dict[str, datetime],
    stuck: dict[str, set[int]] | None = None,
) -> str:
    """Render the coord doc body.

    Cursors render as ``owner/name <iso>`` rows; stuck-issue entries
    (optional) render as ``stuck:owner/name#<number>`` rows beneath
    them. Sorted output keeps diffs minimal across writes.
    """
    lines = [_COORD_DOC_BODY_HEADER]
    for repo in sorted(cursors):
        lines.append(f"{repo} {_isoformat(cursors[repo])}")
    if stuck:
        for repo in sorted(stuck):
            for number in sorted(stuck[repo]):
                lines.append(f"{_STUCK_PREFIX}{repo}#{number}")
    lines.append("")  # trailing newline
    return "\n".join(lines)


def _parse_iso(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _isoformat(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _copy_stuck(stuck: dict[str, set[int]]) -> dict[str, set[int]]:
    """Deep-copy a stuck-issue map so the persisted snapshot doesn't
    alias the live one.

    PR-review finding 3 (round 5, 2026-05-30): the unchanged-content
    short-circuit compares the current map against the persisted
    snapshot. A shallow ``dict(stuck)`` would let live ``stuck[repo]``
    mutations leak into the snapshot via shared ``set`` references —
    next persist would then see "unchanged" and skip the write even
    when an issue was added or drained.
    """
    return {repo: set(numbers) for repo, numbers in stuck.items()}


# ── Source ─────────────────────────────────────────────────────────────


@dataclass
class GitHubIssueWatcher:
    """Polling source: bus → bootstrap → poll-loop → cursor persistence."""

    github: GitHubClient
    lithos: WatcherLithosClient
    bus: EventBus
    poll_interval_seconds: int
    coord_doc_path: str
    agent_id: str
    # Inline dispatcher for the GH→Lithos sync handler. When set, the
    # watcher calls it per issue and ties cursor advancement to the
    # dispatcher's success — see ``_poll_one_repo`` and PR-review
    # finding 1 (2026-05-30). When ``None`` the watcher falls back to
    # publishing on the bus only (legacy path, used by tests that assert
    # on queue contents). Production wiring always injects a real
    # dispatcher; without one, a cursor advance gets out ahead of any
    # downstream reconciliation and dropped-queue events strand
    # permanently.
    dispatch: Callable[[Event], Awaitable[None]] | None = None
    # Backoff used after a polling-loop iteration that raised. Mirrors
    # :class:`LithosNoteStream` shape.
    reconnect_backoff_seconds: float = 1.0
    max_reconnect_backoff_seconds: float = 60.0
    # Seam for tests so they don't sleep for real.
    _sleep: Any = field(default=asyncio.sleep)

    # State derived at bootstrap.
    _watch_list: dict[str, WatchedRepo] = field(default_factory=dict)
    """``{slug: WatchedRepo}`` — the repos the watcher polls plus their
    import-time exclude filters.

    Rebuilt at bootstrap and on every relevant bus event so an operator
    toggling ``github_watch_enabled`` on a project doc takes effect
    within a poll interval at worst.
    """
    _cursors: dict[str, datetime] = field(default_factory=dict)
    """``{owner/name: updated_at}`` — most-recent issue updated-at seen
    for each repo. Used as the GitHub ``since=`` param for incremental
    polls."""
    _last_persisted_cursors: dict[str, datetime] = field(default_factory=dict)
    """Snapshot of the cursor map at the time of the last successful
    coord-doc write (or coord-doc load on startup). Without this, every
    poll cycle re-wrote the coord doc even when no cursor had advanced —
    a Lithos write per minute, two SSE note.updated events per minute,
    and a steady version-counter creep that operators saw in soak."""
    _stuck_issues: dict[str, set[int]] = field(default_factory=dict)
    """``{repo: {issue_number, ...}}`` — per-repo set of issues whose
    inline dispatch raised during a recent poll.

    PR-review finding 2 (round 4, 2026-05-30): the bootstrap path uses
    ``state="open"``. If the first issue in a bootstrap walk fails to
    dispatch (e.g. marker PATCH 5xx after task_create succeeded) the
    cursor stays ``None``; the GH issue can close before the retry,
    after which the next bootstrap walk no longer surfaces it and the
    linked Lithos task is permanently orphaned. Tracking the failure
    by number lets the next poll retry via ``get_issue`` directly,
    independent of the cursor / state-filter combination.

    PR-review finding 3 (round 5, 2026-05-30): persisted in the coord
    doc as ``stuck:<owner>/<name>#<number>`` rows so daemon restart
    preserves the repair record. Drained on successful retry, on GH
    404 (issue deleted), or never on permanent auth — credentials
    might be rotated."""
    _last_persisted_stuck: dict[str, set[int]] = field(default_factory=dict)
    """Snapshot of :attr:`_stuck_issues` at the time of the last
    successful coord-doc write. Paired with
    :attr:`_last_persisted_cursors` for the unchanged-content
    short-circuit so a steady-state poll cycle that observed no new
    stuck entries doesn't keep re-writing the same body."""
    _coord_doc_id: str | None = None
    _coord_doc_version: int | None = None
    _coord_doc_subscription: Subscription | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Bootstrap once, then run the poll + refresh loops concurrently.

        Cancellable: ``asyncio.CancelledError`` propagates out, the
        gather call cancels its children, and the supervisor's
        shutdown drain finishes cleanly.
        """
        await self._bootstrap()
        try:
            await asyncio.gather(
                self._poll_loop(),
                self._refresh_loop(),
            )
        except asyncio.CancelledError:
            raise

    async def _bootstrap(self) -> None:
        """One-shot startup: load watch list + open the bus subscription.

        Logs the initial watch list size at INFO so an operator who set
        ``enabled = true`` but hasn't yet tagged any project can see the
        "watching nothing" state explicitly — without this they'd see
        the startup banner, the coord-doc check, and then nothing for
        every poll cycle, which reads identically to a stuck daemon.
        Subsequent transitions are covered by ``_refresh_watch_list``'s
        added/removed log.
        """
        await self._refresh_watch_list()
        if self._watch_list:
            logger.info(
                "github-watcher: watching %d repo(s): %s",
                sum(len(w.repos) for w in self._watch_list.values()),
                sorted(repo for w in self._watch_list.values() for repo in w.repos),
            )
        else:
            logger.info(
                "github-watcher: no watched repos configured — map a repo with "
                "`lithos-loom project add-github-repo <slug> owner/name` and turn "
                "it on with `enable-github <slug>`"
            )
        await self._load_cursors_from_coord_doc()
        # Subscribe BEFORE the first poll so any project-doc changes
        # during the first cycle don't get missed.
        self._coord_doc_subscription = self.bus.subscribe(
            event_types=("lithos.note.created", "lithos.note.updated"),
            name="github-watcher-refresh",
            queue_size=_BUS_QUEUE_SIZE,
        )

    # ── Watch-list management ─────────────────────────────────────────

    async def _refresh_watch_list(self) -> None:
        """Query Lithos for project docs with watching enabled.

        Selects project-context docs whose ``github_watch_enabled``
        metadata is ``True``; each carries a ``github_repos`` list (one
        or more ``owner/name`` strings) plus optional exclude filters,
        stashed in :attr:`_watch_list`. Docs with the flag on but an
        empty repo list are skipped (the CLI prevents this, but operator
        drift could).
        """
        try:
            summaries = await self.lithos.note_list(
                path_prefix="projects/",
                metadata_match={GITHUB_WATCH_KEY: True},
            )
        except (OSError, LithosClientError) as exc:
            logger.warning(
                "github-watcher: refresh failed (%s: %s); keeping previous "
                "watch list of %d project(s)",
                type(exc).__name__,
                exc,
                len(self._watch_list),
            )
            return

        new_list: dict[str, WatchedRepo] = {}
        for summary in summaries:
            slug = summary.slug
            if not slug:
                continue
            meta = dict(summary.metadata)
            repos = extract_github_repos(meta)
            if not repos:
                logger.info(
                    "github-watcher: project %s has %s but no github_repos "
                    "— skipping until a repo is added",
                    slug,
                    GITHUB_WATCH_KEY,
                )
                continue
            new_list[slug] = WatchedRepo(
                repos=tuple(repos),
                exclude_labels=tuple(extract_exclude_labels(meta)),
                exclude_authors=tuple(extract_exclude_authors(meta)),
            )
        added = set(new_list) - set(self._watch_list)
        removed = set(self._watch_list) - set(new_list)
        # Config drift on existing slugs (operator added a repo or an
        # exclude filter) — surface it at INFO so it's visible when the
        # next poll starts honouring it.
        changed = {
            slug
            for slug in new_list.keys() & self._watch_list.keys()
            if new_list[slug] != self._watch_list[slug]
        }
        if added or removed or changed:
            logger.info(
                "github-watcher: watch list refresh — added=%s removed=%s changed=%s",
                sorted(added),
                sorted(removed),
                sorted(changed),
            )
        # Cursors are keyed by repo (owner/name), so reset is computed
        # per repo, not per slug — adding a sibling repo to a project
        # must NOT reset the cursors of the repos it already tracks.
        # A repo's cursor is dropped when it is newly watched (so a
        # disable → re-enable cycle re-surfaces issues created while
        # paused, rather than re-loading a stale cursor), when its
        # project's exclude filters change (so re-included issues
        # surface), or when it leaves the watch set (cleanup).
        cursor_reset_repos: set[str] = set()
        for slug in added | removed | changed:
            old_entry = self._watch_list.get(slug)
            new_entry = new_list.get(slug)
            old_repos = set(old_entry.repos) if old_entry else set()
            new_repos = set(new_entry.repos) if new_entry else set()
            filters_changed = bool(
                old_entry
                and new_entry
                and (old_entry.exclude_labels, old_entry.exclude_authors)
                != (new_entry.exclude_labels, new_entry.exclude_authors)
            )
            if filters_changed:
                cursor_reset_repos |= new_repos
            else:
                cursor_reset_repos |= new_repos - old_repos
            cursor_reset_repos |= old_repos - new_repos
        for repo in cursor_reset_repos:
            if repo in self._cursors:
                logger.info(
                    "github-watcher: resetting cursor for %s after watch-list "
                    "change so re-included issues surface",
                    repo,
                )
                self._cursors.pop(repo, None)
        self._watch_list = new_list

    # ── Coord doc cursors ─────────────────────────────────────────────

    async def _load_cursors_from_coord_doc(self) -> None:
        """Read the coord doc; populate :attr:`_cursors` from its body.

        A missing doc is the bootstrap signal — leave cursors empty so
        the first poll walks every open issue (US-56).
        """
        try:
            note = await self.lithos.note_read(path=self.coord_doc_path)
        except (OSError, LithosClientError) as exc:
            logger.warning(
                "github-watcher: failed to read coord doc %s (%s); "
                "treating as first-run",
                self.coord_doc_path,
                exc,
            )
            return
        if note is None:
            logger.info(
                "github-watcher: coord doc %s not yet present; first-run mode",
                self.coord_doc_path,
            )
            return
        self._coord_doc_id = note.id
        self._coord_doc_version = note.version
        self._cursors = parse_cursors(note.body)
        # PR-review finding 3 (round 5, 2026-05-30): also reload the
        # persisted stuck-issue set so a daemon restart between a partial
        # task_create + marker_write and the next retry pass still
        # surfaces the stuck issue by-number on the first poll after
        # boot. Without this, restart loses the in-memory set and a
        # closed-before-restart issue stays orphaned forever.
        self._stuck_issues = parse_stuck(note.body)
        # The remote already holds what we just loaded — track it as
        # "already persisted" so the first poll-cycle's write is skipped
        # when no cursor advanced.
        self._last_persisted_cursors = dict(self._cursors)
        self._last_persisted_stuck = _copy_stuck(self._stuck_issues)
        logger.info(
            "github-watcher: loaded %d cursor(s) and %d stuck issue(s) from "
            "coord doc v%d",
            len(self._cursors),
            sum(len(s) for s in self._stuck_issues.values()),
            note.version,
        )

    async def _persist_cursors(self) -> None:
        """CAS-write the coord doc with the current cursor map.

        Short-circuits when no cursor has advanced since the last
        successful write — otherwise every poll cycle would re-write
        the same content, bumping the Lithos version and firing two
        SSE note.updated events per minute even when GitHub returned
        nothing new (soak observation: coord doc climbed to v60+ in
        under an hour with no GH activity).

        On version_conflict, merge our pending advances with the
        remote's cursors per-repo (latest timestamp wins) and retry
        with the fresh version. A handful of retries are allowed before
        giving up so a noisy concurrent writer doesn't block forever —
        the poll loop will retry whole-pass next interval anyway.

        The unchanged-cursors check runs at the TOP of every CAS
        iteration (not just at entry) so the conflict-then-merge path
        also short-circuits when the remote already held what we would
        have written — otherwise ``continue`` would bypass the entry
        guard and pointlessly bump the coord-doc version on the retry
        (PR-review finding round 2).
        """
        # PR-review finding 1 (round 5, 2026-05-30): track which repos we
        # *intend to delete* this persist call so the version_conflict
        # reload-then-merge path can re-apply the deletions. Without
        # tombstones, a refresh that popped a cursor would lose that
        # intent on conflict because reload re-populates ``_cursors``
        # from the remote (which still contains the row we wanted gone).
        # Snapshot the intended deletions BEFORE the first write so
        # subsequent reload+merge cycles can replay them deterministically.
        deletions = set(self._last_persisted_cursors) - set(self._cursors)
        # PR-review finding 3 (round 6, 2026-05-30): same pattern for
        # stuck-issue rows. A stuck entry drained locally (issue's
        # by-number retry succeeded, or GH returned 404) was getting
        # resurrected when a CAS conflict reloaded the remote stuck set
        # and merged pending entries — the remote row was preserved
        # because we only union, never subtract. Capture per-repo
        # number-level tombstones at entry and apply them after the
        # reload+merge.
        stuck_deletions: dict[str, set[int]] = {}
        for repo, numbers in self._last_persisted_stuck.items():
            current = self._stuck_issues.get(repo, set())
            removed = numbers - current
            if removed:
                stuck_deletions[repo] = removed
        attempts = 0
        while True:
            cursors_unchanged = self._cursors == self._last_persisted_cursors
            stuck_unchanged = self._stuck_issues == self._last_persisted_stuck
            if cursors_unchanged and stuck_unchanged:
                logger.debug(
                    "github-watcher: coord doc write skipped — cursors and "
                    "stuck-set unchanged"
                )
                return
            attempts += 1
            body = format_cursors(self._cursors, self._stuck_issues)
            try:
                result = await self.lithos.note_write(
                    agent=self.agent_id,
                    id=self._coord_doc_id,
                    path=self.coord_doc_path if self._coord_doc_id is None else None,
                    title=_COORD_DOC_TITLE,
                    content=body,
                    expected_version=self._coord_doc_version,
                    note_type="concept",
                    tags=["lithos-loom-internal", "github-watcher-state"],
                )
            except (OSError, LithosClientError) as exc:
                logger.warning(
                    "github-watcher: coord doc write failed (%s: %s); "
                    "cursors will retry next poll",
                    type(exc).__name__,
                    exc,
                )
                return
            if result.status in ("created", "updated") and result.note is not None:
                self._coord_doc_id = result.note.id
                self._coord_doc_version = result.note.version
                self._last_persisted_cursors = dict(self._cursors)
                self._last_persisted_stuck = _copy_stuck(self._stuck_issues)
                stuck_count = sum(len(s) for s in self._stuck_issues.values())
                logger.info(
                    "github-watcher: coord doc %s → v%d (%d cursor(s), %d stuck)",
                    result.status,
                    result.note.version,
                    len(self._cursors),
                    stuck_count,
                )
                return
            if result.status == "version_conflict":
                if attempts >= _MAX_COORD_DOC_CAS_ATTEMPTS:
                    logger.warning(
                        "github-watcher: coord doc CAS exhausted after %d "
                        "version_conflicts; pending cursor advances will "
                        "retry next poll",
                        attempts,
                    )
                    return
                logger.info(
                    "github-watcher: coord doc version_conflict; merging + retry "
                    "(attempt %d/%d)",
                    attempts,
                    _MAX_COORD_DOC_CAS_ATTEMPTS,
                )
                # Hold our pending advances + stuck-issue set; the load
                # step will replace ``_cursors`` and ``_stuck_issues``
                # with the remote view, then we re-merge so the just-
                # observed advances aren't lost.
                pending = dict(self._cursors)
                pending_stuck = _copy_stuck(self._stuck_issues)
                await self._load_cursors_from_coord_doc()
                for repo, ts in pending.items():
                    remote_ts = self._cursors.get(repo)
                    if remote_ts is None or ts > remote_ts:
                        self._cursors[repo] = ts
                # Merge pending stuck entries: union per repo. Remote may
                # have stuck entries from another writer we want to keep,
                # and we may have new ones from this poll.
                for repo, numbers in pending_stuck.items():
                    self._stuck_issues.setdefault(repo, set()).update(numbers)
                # Re-apply intended deletions captured at function entry
                # (PR-review finding 1, round 5, 2026-05-30). Without
                # this, a cursor we explicitly popped is restored by
                # reload and silently lives on in the next write.
                for repo in deletions:
                    self._cursors.pop(repo, None)
                # Same tombstone re-application for stuck entries —
                # PR-review finding 3, round 6, 2026-05-30. Without
                # this, draining a row locally and then hitting a CAS
                # conflict resurrects the row from the remote view.
                for repo, numbers in stuck_deletions.items():
                    remote_set = self._stuck_issues.get(repo)
                    if remote_set is None:
                        continue
                    remote_set.difference_update(numbers)
                    if not remote_set:
                        self._stuck_issues.pop(repo, None)
                continue
            logger.warning(
                "github-watcher: unexpected coord doc write status %r: %s",
                result.status,
                result.message,
            )
            return

    # ── Refresh loop (bus subscriber) ─────────────────────────────────

    async def _refresh_loop(self) -> None:
        """Drain the bus subscription and refresh the watch list on relevance.

        Reacts to ``lithos.note.{created,updated}`` events whose
        ``path`` starts with ``projects/``. The lookup is path-prefix
        based because the event payload (per
        :class:`LithosNoteStream._publish`) carries ``{id, title, path}``
        — no metadata — so we can't filter by ``github_watch_enabled``
        directly and have to refresh on any project-doc change.
        Refreshes are cheap.
        """
        assert self._coord_doc_subscription is not None
        sub = self._coord_doc_subscription
        while True:
            event = await sub.queue.get()
            path = event.payload.get("path")
            if not isinstance(path, str) or not path.startswith("projects/"):
                continue
            # Avoid refresh-storms on writes to the coord doc itself.
            if path == self.coord_doc_path:
                continue
            await self._refresh_watch_list()

    # ── Polling loop ──────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Forever: poll every watched repo, advance cursors, sleep.

        A whole-pass error doesn't kill the source — it backs off and
        retries on the next iteration. Per-repo errors are absorbed
        inside :meth:`_poll_one_repo` so a single misconfigured repo
        doesn't block others.
        """
        backoff = self.reconnect_backoff_seconds
        while True:
            try:
                await self._poll_all_repos()
                # Single coord doc write after the full pass — one round-
                # trip per poll, not one per repo. PR-review finding 1
                # (round 4, 2026-05-30): always call through, even with
                # an empty cursor map. The previous `if self._cursors:`
                # guard short-circuited persistence after every slug got
                # removed, leaving stale rows in the coord doc; on
                # restart the daemon then resumed from those stale
                # cursors and could miss issues created during the
                # disabled window. _persist_cursors itself short-circuits
                # via the unchanged-cursors check, so an empty map that's
                # already on disk stays a no-op.
                await self._persist_cursors()
                backoff = self.reconnect_backoff_seconds
                await self._sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "github-watcher: poll cycle failed (%s: %s); backing off %.1fs",
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                await self._sleep(backoff)
                backoff = min(backoff * 2, self.max_reconnect_backoff_seconds)

    async def _poll_all_repos(self) -> None:
        items = list(self._watch_list.items())
        if not items:
            logger.debug("github-watcher: poll cycle skipped; watch list empty")
            return
        # A project may map several repos; flatten to (slug, repo) pairs.
        pairs = [(slug, repo) for slug, watched in items for repo in watched.repos]
        logger.info(
            "github-watcher: poll cycle starting (%d project(s), %d repo(s))",
            len(items),
            len(pairs),
        )
        for slug, repo in pairs:
            await self._poll_one_repo(slug=slug, repo=repo)

    def _drop_repo(self, *, slug: str, repo: str) -> None:
        """Remove a single repo from a project's watch entry (e.g. on a
        404) without disturbing the project's other repos.

        A project may map several repos; a 404 on one must not stop
        polling the siblings. Drops the repo from the entry's ``repos``
        tuple (removing the slug entirely only when it was the last
        repo) and clears that repo's cursor + stuck state. The next
        ``_refresh_watch_list`` re-reads the canonical metadata, so a
        repo that 404s but is still mapped will be re-added and
        re-attempted — same transient-drop behaviour as before, now
        scoped to the offending repo.
        """
        watched = self._watch_list.get(slug)
        if watched is not None:
            remaining = tuple(r for r in watched.repos if r != repo)
            if remaining:
                self._watch_list[slug] = replace(watched, repos=remaining)
            else:
                self._watch_list.pop(slug, None)
        self._cursors.pop(repo, None)
        self._stuck_issues.pop(repo, None)

    async def _retry_stuck_issues(self, *, slug: str, repo: str) -> bool:
        """Retry issues whose dispatch failed in a previous poll.

        Returns ``True`` if every stuck issue dispatched cleanly (or was
        deleted on GH), ``False`` if anything is still stuck. Callers
        defer the normal cursor-based fetch on ``False`` so a persistent
        failure doesn't accumulate fresh entries on top of the unresolved
        ones.

        Each issue is re-fetched via ``get_issue`` instead of relying on
        the cursor + state filter — the bootstrap path (state="open")
        wouldn't surface an issue that closed since the previous
        attempt, but the per-issue PATCH-equivalent ``GET`` does.
        """
        stuck = self._stuck_issues.get(repo)
        if not stuck:
            return True
        for number in list(stuck):
            try:
                issue = await self.github.get_issue(repo, number)
            except GitHubError as exc:
                # PR-review finding 2 (round 5, 2026-05-30): auth errors
                # used to drop the entry here. They aren't actually
                # permanent — the operator might rotate `gh auth` and
                # come back. Keep the entry; only ``None`` (issue
                # genuinely deleted on GH, returned by ``get_issue``
                # short-circuit on 404) or a successful dispatch retires
                # the entry. ``get_issue`` itself never raises
                # ``GitHubRepoNotFoundError`` — the 404 short-circuits
                # to ``None`` — so all subclasses here are effectively
                # transient.
                logger.warning(
                    "github-watcher: re-fetch of stuck %s/#%d transient (%s: %s); "
                    "leaving in stuck set",
                    repo,
                    number,
                    type(exc).__name__,
                    exc,
                )
                return False
            if issue is None:
                # GH issue deleted in the interim — nothing to reconcile.
                logger.info(
                    "github-watcher: stuck %s/#%d gone on GH; dropping from stuck set",
                    repo,
                    number,
                )
                stuck.discard(number)
                continue
            try:
                await self._publish_issue(slug=slug, issue=issue)
            except Exception as exc:
                logger.warning(
                    "[Friction] github-watcher: stuck %s/#%d dispatch still "
                    "fails (%s: %s); will retry next poll",
                    repo,
                    number,
                    type(exc).__name__,
                    exc,
                )
                return False
            stuck.discard(number)
        if not stuck:
            self._stuck_issues.pop(repo, None)
        return True

    async def _poll_one_repo(self, *, slug: str, repo: str) -> None:
        """Fetch issues for one repo, emit events, advance the cursor.

        Two distinct paths:

        - **Bootstrap** (no cursor yet for this repo): walks every open
          issue with ``state="open"``, fully paginated. This matches
          PRD US-56's "walk every open issue on daemon start" guarantee
          and avoids burning through closed history one 100-issue page
          per poll interval on a repo with hundreds of resolved issues.
        - **Incremental** (cursor present): uses ``state="all"`` since
          the cursor, fully paginated. State transitions (open → closed)
          surface alongside fresh opens because GH advances
          ``updated_at`` on close, so the cursor-based delta catches
          them.

        Errors are absorbed: a 404 drops the repo from the watch list
        (the project doc still owns the mapping; next refresh will
        re-add it if the operator fixes the typo). Auth/rate-limit
        errors are logged but don't propagate — the next pass retries.

        PR-review finding 2 (round 4, 2026-05-30): before the regular
        fetch, retry any issues that failed dispatch in a previous poll
        via ``get_issue`` directly. The bootstrap path uses
        ``state="open"`` and would lose a closed-before-retry issue
        otherwise; retrying by number is cursor-independent and survives
        the close transition.
        """
        if not await self._retry_stuck_issues(slug=slug, repo=repo):
            # A stuck issue still failed; defer the new-fetch this poll
            # so we don't keep racking up additional stuck entries while
            # the underlying problem persists.
            return
        since = self._cursors.get(repo)
        state = "open" if since is None else "all"
        try:
            issues = await self.github.list_issues_since(repo, since=since, state=state)
        except GitHubRepoNotFoundError:
            logger.warning(
                "[Friction] github-watcher: repo %s not found; "
                "drop from watch list (slug=%s)",
                repo,
                slug,
            )
            self._drop_repo(slug=slug, repo=repo)
            return
        except GitHubAuthError as exc:
            logger.warning(
                "[Friction] github-watcher: auth/permission denied on %s: %s",
                repo,
                exc,
            )
            return
        except GitHubError as exc:
            logger.warning(
                "github-watcher: %s on %s: %s",
                type(exc).__name__,
                repo,
                exc,
            )
            return

        prior_cursor = since
        # GitHub returns issues sorted by ``updated_at`` ascending
        # (``sort=updated&direction=asc`` in list_issues_since). Walk in
        # order so a mid-batch dispatch failure leaves the cursor at the
        # latest *successfully reconciled* issue rather than skipping
        # ahead — PR-review finding 1 (2026-05-30) was that the prior
        # max-after-the-loop pattern allowed bus drops AND handler
        # failures to permanently strand events.
        max_committed: datetime | None = None
        dispatch_failed_at: datetime | None = None
        for issue in issues:
            try:
                await self._publish_issue(slug=slug, issue=issue)
            except Exception as exc:
                dispatch_failed_at = issue.updated_at
                self._stuck_issues.setdefault(repo, set()).add(issue.number)
                logger.warning(
                    "[Friction] github-watcher: dispatch for %s/#%d failed "
                    "(%s: %s); holding cursor at %s and tagging issue for "
                    "by-number retry next poll",
                    repo,
                    issue.number,
                    type(exc).__name__,
                    exc,
                    _isoformat(max_committed) if max_committed else "<unchanged>",
                )
                break
            max_committed = issue.updated_at

        if max_committed is not None:
            self._cursors[repo] = max_committed
            logger.info(
                "github-watcher: %s — %d issue(s) %s (state=%s, cursor %s → %s)",
                repo,
                len(issues),
                "bootstrapped" if prior_cursor is None else "delta",
                state,
                _isoformat(prior_cursor) if prior_cursor is not None else "<first-run>",
                _isoformat(max_committed),
            )
        elif issues and dispatch_failed_at is not None:
            # First issue failed — cursor unchanged so the next poll
            # re-fetches and retries.
            logger.info(
                "github-watcher: %s — first dispatch failed at %s; "
                "cursor unchanged (will retry next poll)",
                repo,
                _isoformat(dispatch_failed_at),
            )
        else:
            logger.info(
                "github-watcher: %s — no changes (state=%s, since=%s)",
                repo,
                state,
                _isoformat(prior_cursor) if prior_cursor is not None else "<first-run>",
            )

    async def _publish_issue(self, *, slug: str, issue: Issue) -> None:
        """Build the event for ``issue`` and dispatch.

        When :attr:`dispatch` is injected (production), call it inline
        and propagate any exception so the caller can hold the cursor at
        the prior successful issue. When ``None`` (legacy / tests that
        assert on bus queue contents), publish onto the in-process bus
        which silently drops on queue-full — *not* a path the production
        wiring should rely on for correctness.
        """
        watched = self._watch_list.get(slug)
        # The slug being absent here is a defensive guard — _poll_all_repos
        # iterates the watch list, so a race with refresh is the only way
        # to land here. Treat as "no filters" rather than crashing.
        exclude_labels = list(watched.exclude_labels) if watched else []
        exclude_authors = list(watched.exclude_authors) if watched else []
        event = Event(
            type=GITHUB_ISSUE_EVENT_TYPE,
            timestamp=issue.updated_at,
            payload={
                "slug": slug,
                "repo": issue.repo,
                "number": issue.number,
                "title": issue.title,
                "body": issue.body,
                "state": issue.state,
                "state_reason": issue.state_reason,
                "labels": list(issue.labels),
                "author": issue.author,
                "html_url": issue.html_url,
                "updated_at": _isoformat(issue.updated_at),
                "exclude_labels": exclude_labels,
                "exclude_authors": exclude_authors,
            },
        )
        if self.dispatch is not None:
            await self.dispatch(event)
            return
        await self.bus.publish(event)
