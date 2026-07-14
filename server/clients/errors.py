"""Shared error shaping so every client surfaces failures as a structured
dict — consistent with the existing `_not_found()` pattern in main.py —
instead of letting httpx/network exceptions bubble up as opaque MCP tool
errors.
"""

from __future__ import annotations

import httpx


class NotConfiguredError(Exception):
    """Raised by a client when the env vars it needs aren't set."""


def not_configured(integration: str, missing: list[str]) -> dict:
    return {
        "error": f"{integration} is not configured (missing: {', '.join(missing)})",
        "found": False,
    }


def from_exception(exc: Exception, integration: str) -> dict:
    if isinstance(exc, NotConfiguredError):
        return {"error": str(exc), "found": False}
    if isinstance(exc, httpx.HTTPStatusError):
        return {
            "error": f"{integration} API error: {exc.response.status_code} {exc.response.text[:500]}",
            "status_code": exc.response.status_code,
            "found": False,
        }
    if isinstance(exc, httpx.RequestError):
        return {"error": f"{integration} request failed: {exc}", "found": False}
    return {"error": f"{integration} error: {exc}", "found": False}
