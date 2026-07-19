"""Shared error shaping so every client surfaces failures as a structured
dict — consistent with the existing `_not_found()` pattern in main.py —
instead of letting httpx/network exceptions bubble up as opaque MCP tool
errors.
"""

from __future__ import annotations

import functools

import httpx


class NotConfiguredError(Exception):
    """Raised by a client when the env vars it needs aren't set."""


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


def guarded(integration: str):
    """Decorator for @mcp.tool() functions: turns any exception raised by a
    client call into the same structured error dict from_exception() would
    produce, instead of letting it surface as an opaque MCP tool error."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                return from_exception(exc, integration)

        return wrapper

    return decorator
