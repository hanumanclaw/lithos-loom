"""Exception hierarchy for lithos-loom.

All package-raised exceptions derive from :class:`LithosLoomError` so callers
can catch a single base type. Exceptions surface task failures via Lithos
findings and structured outcome summaries rather than crashing the daemon.
"""

from __future__ import annotations


class LithosLoomError(Exception):
    """Base class for all lithos-loom exceptions."""


class ConfigError(LithosLoomError):
    """Raised when configuration is missing or invalid."""


class LithosUnreachableError(LithosLoomError):
    """Raised when the configured Lithos server cannot be reached."""


class ProjectNotRegisteredError(LithosLoomError):
    """Raised when a task's ``metadata.project`` does not match any project entry."""


class DependencyCycleError(LithosLoomError):
    """Raised when ``metadata.depends_on`` graph contains a cycle (US-9)."""


class PluginContractError(LithosLoomError):
    """Raised when a plugin's ``result.json`` violates the schema (US-3)."""


class ClaimError(LithosLoomError):
    """Raised when a Lithos task claim attempt fails."""


class LithosClientError(LithosLoomError):
    """Raised when a Lithos MCP tool returns a ``{status: "error", ...}`` envelope.

    ``code`` mirrors the canonical ``code`` discriminator on the envelope
    (e.g. ``"task_not_found"``, ``"invalid_input"``, ``"claim_failed"``).
    ``message`` carries the human-readable explanation.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
