"""Async HTTP client over the Lithos MCP surface.

Stub — implements ``docs/prd/mvp.md`` US-2:

* task_list, task_status, task_claim, task_renew, task_release,
  task_complete, task_update, finding_post, read, write, agent_register
* Surfaces ``{status: "error", code, message}`` envelopes as raised exceptions
  with ``code`` preserved for callers to switch on
* Configurable base URL via ``LITHOS_URL`` env var or TOML
  ``orchestrator.lithos_url``
"""

from __future__ import annotations


class LithosClient:
    """Stub client. Implement per docs/prd/mvp.md US-2."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    async def __aenter__(self) -> LithosClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None
