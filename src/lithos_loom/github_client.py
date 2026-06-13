"""Thin async wrapper over the GitHub REST API for the issue watcher.

Scope is limited to what Slice 7.1 of ``docs/prd/github-issue-watcher.md``
needs:

- list open issues in a repo updated since a cursor,
- fetch a single issue (for closure-state reconciliation),
- update an issue body (to write the ``<!-- lithos:<task_id> -->`` marker),
- parse + apply that marker.

Auth is bearer-token via ``gh auth token`` at startup; this reuses the
operator's existing ``gh`` login so the watcher needs no extra env vars
(the capture macro already requires ``gh`` on the PATH).

Pull requests are filtered at parse time (D53): GitHub's ``/issues``
endpoint returns both issues and PRs, distinguished by the presence of
a ``pull_request`` field on the row.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .errors import LithosLoomError

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"
_USER_AGENT = "lithos-loom-github-watcher"

# Regex matches both canonical and operator-edited shapes:
#   <!-- lithos:abc-123 -->  → canonical
#   <!-- LITHOS:ABC-123 -->  → case-insensitive tolerated
# Captured group 1 is the task id.
_MARKER_RE = re.compile(r"<!--\s*lithos:\s*([A-Za-z0-9_-]+)\s*-->", re.IGNORECASE)


class GitHubError(LithosLoomError):
    """Base for GitHub-watcher errors. Subclasses carry actionable context."""


class GitHubAuthError(GitHubError):
    """Raised when GitHub auth fails (401, 403, or missing/broken gh CLI)."""


class GitHubRepoNotFoundError(GitHubError):
    """Raised when a watched repo returns 404.

    The watcher drops the repo from the cursor map and continues.
    """

    def __init__(self, repo: str) -> None:
        super().__init__(f"GitHub repo not found: {repo}")
        self.repo = repo


class GitHubRateLimitError(GitHubError):
    """Raised when a rate-limit retry exhausts (currently only on a second 403)."""


@dataclass(frozen=True)
class Issue:
    """The slice of GitHub's issue payload the watcher cares about."""

    repo: str
    number: int
    title: str
    body: str
    state: str  # "open" | "closed"
    state_reason: str | None  # "completed" | "not_planned" | None
    labels: tuple[str, ...]
    author: str
    updated_at: datetime
    html_url: str


@dataclass(frozen=True)
class PullRequest:
    """The slice of GitHub's pull-request payload the PR-merge watcher cares about.

    ``merged`` is a top-level boolean on the single-PR endpoint
    (``GET /pulls/{n}``) — reliable there, unlike the list endpoint where it
    is absent. ``merged_at`` / ``merge_commit_sha`` are populated only once the
    PR has actually merged.
    """

    repo: str
    number: int
    state: str  # "open" | "closed"
    merged: bool
    merged_at: datetime | None
    merge_commit_sha: str | None


# ── Pure helpers ──────────────────────────────────────────────────────


def _parse_iso(s: str) -> datetime:
    """GitHub stamps timestamps as ``2026-05-29T12:00:00Z``. Make them tz-aware."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _parse_issues_response(payload: list[dict[str, Any]], *, repo: str) -> list[Issue]:
    """Convert a GitHub ``/issues`` response into typed Issues, dropping PRs.

    Pull requests appear in the same endpoint with a ``pull_request`` field
    set. D53 requires they be filtered out so the subscription handler never
    sees them.
    """
    issues: list[Issue] = []
    for row in payload:
        if "pull_request" in row:
            continue
        issues.append(
            Issue(
                repo=repo,
                number=int(row["number"]),
                title=str(row["title"]),
                body=str(row.get("body") or ""),
                state=str(row["state"]),
                state_reason=row.get("state_reason"),
                labels=tuple(lbl["name"] for lbl in row.get("labels") or ()),
                author=str((row.get("user") or {}).get("login", "")),
                updated_at=_parse_iso(str(row["updated_at"])),
                html_url=str(row.get("html_url", "")),
            )
        )
    return issues


def _parse_pull_request(row: dict[str, Any], *, repo: str) -> PullRequest:
    """Convert a GitHub ``GET /pulls/{n}`` response row into a typed PullRequest."""
    merged_at_raw = row.get("merged_at")
    sha = row.get("merge_commit_sha")
    return PullRequest(
        repo=repo,
        number=int(row["number"]),
        state=str(row["state"]),
        merged=bool(row.get("merged", False)),
        merged_at=_parse_iso(str(merged_at_raw)) if merged_at_raw else None,
        merge_commit_sha=str(sha) if sha else None,
    )


def parse_marker(body: str | None) -> str | None:
    """Extract the task id from a ``<!-- lithos:<id> -->`` marker, if present.

    Tolerant of placement (top/bottom of body) and case (the writer emits
    canonical lowercase ``lithos:`` but the parser accepts both).
    """
    if not body:
        return None
    match = _MARKER_RE.search(body)
    if match is None:
        return None
    return match.group(1)


def apply_marker(body: str | None, task_id: str) -> str:
    """Return ``body`` with a canonical marker appended at the end.

    If a marker is already present (anywhere), it is removed first so the
    canonical form lands at the body's tail. This both fixes operator
    placement drift over time and prevents duplicate markers.
    """
    text = body or ""
    text = _MARKER_RE.sub("", text).rstrip()
    canonical = f"<!-- lithos:{task_id} -->"
    if not text:
        return canonical
    return f"{text}\n\n{canonical}"


def strip_marker(body: str | None) -> str:
    """Return ``body`` with any ``<!-- lithos:<id> -->`` marker removed.

    Slice 7.2 mirrors GH issue body → Lithos task description. The Loom-
    managed marker is bookkeeping noise from the operator's perspective
    and must not bleed into the projected task surface, so it is stripped
    before comparison + write.
    """
    if not body:
        return ""
    return _MARKER_RE.sub("", body).strip()


# ── gh auth token resolver ────────────────────────────────────────────


async def _resolve_gh_token() -> str:
    """Shell out to ``gh auth token`` to obtain the operator's GitHub token.

    Raises :class:`GitHubAuthError` if ``gh`` is missing from PATH, if the
    operator is not logged in, or if the subprocess exits non-zero.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "auth",
            "token",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise GitHubAuthError(
            "gh CLI not found on PATH — install GitHub CLI and run `gh auth login`"
        ) from exc
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace").strip()
        raise GitHubAuthError(f"gh auth token failed: {msg or 'unknown gh auth error'}")
    token = stdout.decode("utf-8", errors="replace").strip()
    if not token:
        raise GitHubAuthError("gh auth token returned empty output")
    return token


# ── HTTP client ───────────────────────────────────────────────────────


@dataclass
class GitHubClient:
    """Async client for the slice-7.1 subset of the GitHub REST API.

    Constructor takes an injected ``httpx.AsyncClient`` so tests can wire
    in respx mocks without monkeypatching globals. Production callers build
    one via :meth:`create`.
    """

    http: httpx.AsyncClient
    token: str
    base_url: str = GITHUB_API_BASE
    # Capped retry to avoid infinite spin on a misconfigured stub or a
    # genuinely permanent rate-limit reset (clock skew).
    _max_rate_limit_retries: int = 2

    @classmethod
    async def create(cls, *, http: httpx.AsyncClient | None = None) -> GitHubClient:
        """Resolve the bearer token via ``gh auth token`` and construct a client.

        Callers that already own an ``httpx.AsyncClient`` (e.g. the child's
        shared HTTP session) can pass it in; otherwise a fresh one is created
        and the caller is responsible for closing it.
        """
        token = await _resolve_gh_token()
        client = http if http is not None else httpx.AsyncClient(timeout=30.0)
        return cls(http=client, token=token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": _USER_AGENT,
        }

    async def _get(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        return await self._get_url(f"{self.base_url}{path}", params=params)

    async def _get_url(
        self, url: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        """GET an already-absolute URL with rate-limit retry.

        Used for pagination: GitHub's ``Link: rel="next"`` URLs are
        absolute and carry their own query string, so they bypass the
        ``base_url`` + ``path`` concatenation.
        """
        return await self._request_with_rate_limit_retry(
            "GET", url, params=params, json=None
        )

    async def _patch(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
        url = f"{self.base_url}{path}"
        return await self._request_with_rate_limit_retry(
            "PATCH", url, params=None, json=json
        )

    async def _request_with_rate_limit_retry(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        """Issue a request, sleeping until ``X-RateLimit-Reset`` on 403+remaining=0.

        Shared by GET (issue listing, single-issue fetch) and PATCH
        (marker write, title update, close mirror) because PRD #70
        requires graceful backoff for *all* GitHub operations — the
        earlier code only retried GETs and converted rate-limited
        PATCHes into dropped auth-style failures (PR-review finding 5,
        2026-05-30).
        """
        attempts = 0
        while True:
            attempts += 1
            response = await self.http.request(
                method,
                url,
                params=params,
                json=json,
                headers=self._headers(),
            )
            if response.status_code != 403:
                return response
            # 403 is overloaded: rate-limit signal vs permission-denied. Distinguish
            # by the X-RateLimit-Remaining header (0 == rate-limited).
            if not _is_rate_limited(response):
                return response
            if attempts > self._max_rate_limit_retries:
                raise GitHubRateLimitError(f"rate limit retries exhausted for {url}")
            await _wait_for_rate_limit_reset(response)

    async def list_issues_since(
        self,
        repo: str,
        *,
        since: datetime | None,
        state: str = "all",
    ) -> list[Issue]:
        """Fetch issues updated at-or-after ``since``, paginating until exhausted.

        Drains every page by following the ``Link: rel="next"`` header.
        Without this, a repo with more than 100 issues in scope would
        return only the oldest 100 per call — and because the endpoint
        sorts ``updated asc``, the watcher would burn one poll interval
        per page before reaching live state (PR-review finding on Slice
        7.1: cold start on a repo with hundreds of historical issues
        could spend many minutes crawling closed history before
        importing current open ones).

        ``state="all"`` (default) surfaces close transitions so the
        handler's close-mirror branches fire. Bootstrap callers
        intentionally pass ``state="open"`` to skip closed history on
        first run; subsequent incremental polls (with a cursor) use
        ``state="all"`` so closes on previously-seen open issues
        surface.

        Pull requests are filtered out at parse time. The endpoint is
        sorted ``updated asc`` so the watcher can advance its cursor to
        the max ``updated_at`` seen this call.
        """
        params: dict[str, Any] = {
            "state": state,
            "sort": "updated",
            "direction": "asc",
            "per_page": 100,
        }
        if since is not None:
            params["since"] = _isoformat_utc(since)

        all_issues: list[Issue] = []
        url: str | None = f"{self.base_url}/repos/{repo}/issues"
        page_params: dict[str, Any] | None = params
        while url is not None:
            response = await self._get_url(url, params=page_params)
            _raise_for_status(response, repo=repo)
            all_issues.extend(_parse_issues_response(response.json(), repo=repo))
            url = _parse_next_link(response.headers.get("Link"))
            # Pagination URLs already encode the query string, so subsequent
            # pages must not double-up the params.
            page_params = None
        return all_issues

    async def get_issue(self, repo: str, number: int) -> Issue | None:
        """Fetch a single issue. Returns ``None`` if the issue was deleted (404)."""
        response = await self._get(f"/repos/{repo}/issues/{number}")
        if response.status_code == 404:
            return None
        _raise_for_status(response, repo=repo)
        parsed = _parse_issues_response([response.json()], repo=repo)
        return parsed[0] if parsed else None

    async def get_pull_request(self, repo: str, number: int) -> PullRequest | None:
        """Fetch a single pull request. Returns ``None`` if it was deleted (404).

        Used by the PR-merge watcher (#87) to detect when a story-develop-
        delivered PR has merged (so the non-issue-linked Lithos task can close)
        or closed-without-merging. Mirrors :meth:`get_issue`.
        """
        response = await self._get(f"/repos/{repo}/pulls/{number}")
        if response.status_code == 404:
            return None
        _raise_for_status(response, repo=repo)
        return _parse_pull_request(response.json(), repo=repo)

    async def update_issue_body(self, repo: str, number: int, body: str) -> None:
        """Replace the issue body. Used to write/refresh the linkage marker.

        GitHub's ``PATCH /issues/{n}`` is full-body replacement with no
        optimistic locking. The race window vs an operator edit is narrow
        but real — documented in the PRD's Risks section.
        """
        response = await self._patch(
            f"/repos/{repo}/issues/{number}", json={"body": body}
        )
        _raise_for_status(response, repo=repo)

    async def update_issue_fields(
        self,
        repo: str,
        number: int,
        *,
        title: str | None = None,
        state: str | None = None,
        state_reason: str | None = None,
    ) -> Issue | None:
        """PATCH the issue with only the non-None fields. Slice-7.2 surface.

        Used by the Lithos→GH push handler:

        - title push (operator renamed the Lithos task) → ``title=...``
        - close mirror (Lithos task completed / cancelled) →
          ``state="closed"`` + ``state_reason="completed"|"not_planned"``

        Returns the post-PATCH Issue (so callers can verify state), or
        ``None`` when no fields were supplied (defensive no-op so handlers
        that pre-compute "nothing changed" don't burn a request).

        ``body`` updates remain on :meth:`update_issue_body` — that path
        also re-fetches before writing to dodge clobbering operator edits
        of the linkage marker (D46). Title / state writes touch
        independent fields and have no marker-collision risk.
        """
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if state is not None:
            payload["state"] = state
        if state_reason is not None:
            payload["state_reason"] = state_reason
        if not payload:
            return None
        response = await self._patch(f"/repos/{repo}/issues/{number}", json=payload)
        _raise_for_status(response, repo=repo)
        parsed = _parse_issues_response([response.json()], repo=repo)
        return parsed[0] if parsed else None


# ── Response error handling ───────────────────────────────────────────


def _raise_for_status(response: httpx.Response, *, repo: str) -> None:
    if response.is_success:
        return
    if response.status_code == 404:
        raise GitHubRepoNotFoundError(repo)
    if response.status_code in (401, 403):
        message = _safe_message(response) or "GitHub auth/permission denied"
        raise GitHubAuthError(f"{response.status_code} for {repo}: {message}")
    detail = _safe_message(response) or response.text[:200]
    raise GitHubError(f"GitHub {response.status_code} for {repo}: {detail}")


def _safe_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return ""
    if isinstance(data, dict):
        msg = data.get("message")
        if isinstance(msg, str):
            return msg
    return ""


def _is_rate_limited(response: httpx.Response) -> bool:
    """A 403 is a rate-limit signal only when ``X-RateLimit-Remaining`` is 0.

    GitHub returns 403 for permission errors too; distinguishing them
    prevents an infinite retry loop on a permanent denial.
    """
    remaining = response.headers.get("x-ratelimit-remaining")
    return remaining == "0"


async def _wait_for_rate_limit_reset(response: httpx.Response) -> None:
    """Sleep until the ``X-RateLimit-Reset`` epoch, with a small grace pad."""
    reset_header = response.headers.get("x-ratelimit-reset")
    now = datetime.now(UTC).timestamp()
    try:
        reset_epoch = float(reset_header) if reset_header else now + 60.0
    except ValueError:
        reset_epoch = now + 60.0
    # 2s grace so we wake up after the window has actually rolled over.
    delay = max(1.0, reset_epoch - now + 2.0)
    logger.info("github rate limit hit; sleeping %.0fs until reset", delay)
    await asyncio.sleep(delay)


def _isoformat_utc(dt: datetime) -> str:
    """Render a datetime as ISO-8601 in UTC, preserving timezone info."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


# RFC 5988-style Link header values look like:
#   <https://api.github.com/...?page=2>; rel="next", <...>; rel="last"
# We only care about the next-page URL — the iteration loop in
# ``list_issues_since`` doesn't need ``last`` because it stops when
# ``next`` is absent.
_LINK_RE = re.compile(r"<([^>]+)>;\s*rel=\"([^\"]+)\"")


def _parse_next_link(header_value: str | None) -> str | None:
    """Extract the ``rel="next"`` URL from a GitHub ``Link`` header.

    Returns None when the header is missing or has no next link
    (i.e. we've reached the last page).
    """
    if not header_value:
        return None
    for match in _LINK_RE.finditer(header_value):
        if match.group(2) == "next":
            return match.group(1)
    return None
