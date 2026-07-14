"""Jira Cloud REST API v3 client — replaces the jira_tickets.json mock.

Uses the "enhanced JQL search" endpoint (POST /rest/api/3/search/jql), which
is what Jira Cloud now expects (the old GET/POST /rest/api/3/search endpoint
is deprecated/removed on Cloud). If you're pointing this at Jira Server/Data
Center instead of Cloud, swap JQL_SEARCH_PATH back to "/rest/api/2/search".

`sprint` is a real JQL clause (`sprint = "..."`) even though it's backed by a
custom field, so filtering by sprint needs no configuration. Reading the
sprint/story-points *value* back off an issue does need the custom field's
id (Jira returns custom fields keyed by `customfield_XXXXX`, not a friendly
name) — set JIRA_SPRINT_FIELD / JIRA_STORY_POINTS_FIELD (see
`/rest/api/3/field` to find them) or those keys come back None.
"""

from __future__ import annotations

from typing import Any

import httpx

from .. import time_utils
from ..config import settings
from .errors import NotConfiguredError, from_exception

JQL_SEARCH_PATH = "/rest/api/3/search/jql"


def _require_configured() -> None:
    if not settings.jira.configured:
        raise NotConfiguredError(
            "Jira is not configured (need JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN)"
        )


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.jira.base_url,
        auth=(settings.jira.email, settings.jira.api_token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=20.0,
    )


def _jql_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_jql(
    assignee: str | None = None,
    status: list[str] | None = None,
    sprint: str | None = None,
    project: str | None = None,
    labels: list[str] | None = None,
    updated_since: str | None = None,
) -> str:
    """Pure function: turns the tool's filter args into a JQL string. Kept
    separate from the network call so it's unit-testable without a live
    Jira instance."""
    clauses: list[str] = []
    if assignee:
        clauses.append(f"assignee = {_jql_quote(assignee)}")
    if status:
        clauses.append("status in (" + ", ".join(_jql_quote(s) for s in status) + ")")
    if sprint:
        clauses.append(f"sprint = {_jql_quote(sprint)}")
    if project:
        clauses.append(f"project = {_jql_quote(project)}")
    if labels:
        clauses.append("labels in (" + ", ".join(_jql_quote(l) for l in labels) + ")")
    if updated_since:
        bound = time_utils.resolve_since(updated_since)
        clauses.append(f"updated >= {_jql_quote(bound.strftime('%Y-%m-%d %H:%M'))}")
    return " AND ".join(clauses) if clauses else "order by updated desc"


def _adf_to_text(node: Any) -> str:
    """Flattens Atlassian Document Format (used for description/comment
    bodies) down to plain text — good enough for an agent to read, not a
    faithful re-render of rich formatting."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(n) for n in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = [_adf_to_text(child) for child in node.get("content", [])]
        text = "".join(parts)
        if node.get("type") in ("paragraph", "heading"):
            return text + "\n"
        return text
    return ""


def _normalize_issue(issue: dict) -> dict:
    fields = issue.get("fields", {})
    assignee = fields.get("assignee") or {}
    project = fields.get("project") or {}
    labels = fields.get("labels") or []

    comments = [
        {
            "author": (c.get("author") or {}).get("displayName"),
            "body": _adf_to_text(c.get("body")).strip(),
            "created": c.get("created"),
        }
        for c in (fields.get("comment") or {}).get("comments", [])
    ]

    status_history = []
    for history in issue.get("changelog", {}).get("histories", []):
        for item in history.get("items", []):
            if item.get("field") == "status":
                status_history.append(
                    {
                        "from": item.get("fromString"),
                        "to": item.get("toString"),
                        "at": history.get("created"),
                        "by": (history.get("author") or {}).get("displayName"),
                    }
                )

    linked_tickets = []
    for link in fields.get("issuelinks", []):
        other = link.get("outwardIssue") or link.get("inwardIssue")
        if other:
            linked_tickets.append(
                {
                    "id": other.get("key"),
                    "summary": (other.get("fields") or {}).get("summary"),
                    "status": ((other.get("fields") or {}).get("status") or {}).get("name"),
                    "relationship": (link.get("type") or {}).get(
                        "outward" if "outwardIssue" in link else "inward"
                    ),
                }
            )

    return {
        "id": issue.get("key"),
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "assignee": assignee.get("displayName") or assignee.get("emailAddress"),
        "project": project.get("key"),
        "labels": labels,
        "sprint": (
            _sprint_value(fields.get(settings.jira.sprint_field))
            if settings.jira.sprint_field
            else None
        ),
        "story_points": fields.get(settings.jira.story_points_field)
        if settings.jira.story_points_field
        else None,
        "updated": fields.get("updated"),
        "created": fields.get("created"),
        "description": _adf_to_text(fields.get("description")).strip(),
        "comments": comments,
        "status_history": status_history,
        "linked_tickets": linked_tickets,
    }


def _sprint_value(raw: Any) -> str | None:
    # Jira Software returns the "Sprint" custom field as a list of sprint
    # objects (or greenhorn-style strings on very old instances).
    if not raw:
        return None
    latest = raw[-1] if isinstance(raw, list) else raw
    if isinstance(latest, dict):
        return latest.get("name")
    return str(latest)


def query_tickets(
    assignee: str | None = None,
    status: list[str] | None = None,
    sprint: str | None = None,
    project: str | None = None,
    labels: list[str] | None = None,
    updated_since: str | None = None,
) -> list[dict]:
    _require_configured()
    jql = build_jql(assignee, status, sprint, project, labels, updated_since)
    with _client() as client:
        issues: list[dict] = []
        next_page_token: str | None = None
        while True:
            body: dict[str, Any] = {
                "jql": jql,
                "fields": ["*all"],
                "maxResults": 100,
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token
            resp = client.post(JQL_SEARCH_PATH, json=body)
            resp.raise_for_status()
            data = resp.json()
            issues.extend(data.get("issues", []))
            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break
    return [_normalize_issue(issue) for issue in issues]


def get_issue(ticket_id: str) -> dict | None:
    _require_configured()
    with _client() as client:
        resp = client.get(
            f"/rest/api/3/issue/{ticket_id}",
            params={"expand": "changelog", "fields": "*all,comment"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _normalize_issue(resp.json())


def update_issue(ticket_id: str, fields: dict) -> dict:
    """Only reached when WRITES_ENABLED=true. Note: a `status` key here won't
    actually move the issue — Jira requires the separate transitions API
    (`POST /issue/{key}/transitions` with a transition id, not a fields PUT)
    for status changes. Whoever re-enables writes should special-case it."""
    _require_configured()
    with _client() as client:
        resp = client.put(f"/rest/api/3/issue/{ticket_id}", json={"fields": fields})
        resp.raise_for_status()
    return get_issue(ticket_id) or {"id": ticket_id, "updated_fields": fields}
