"""MCP server exposing the fixed developer-assistant tool set (Jira, GitHub,
Slack, Gmail, Calendar) backed by the mock dataset in data/*.json.

Runs over stdio by default (for a single agent SDK spawning it as a local
subprocess) or over network transports (sse / streamable-http) so multiple
agent SDKs — Claude Agent SDK, OpenAI Agents SDK, Mastra, Agno, etc. — can all
connect to one running instance. Controlled via env vars: MCP_TRANSPORT
("stdio" | "sse" | "streamable-http", default "stdio"), MCP_HOST (default
"0.0.0.0"), MCP_PORT (default "8081"), MCP_ALLOWED_HOSTS (see below).

Write actions (update_jira_ticket, post_slack_message, send_email) are gated
with a two-step draft/confirm pattern, matching the sibling e-commerce-support
MCP server built for the same bake-off: calling one of them only drafts the
change and returns a pending_action_id — nothing is applied until a human
approves and the caller invokes confirm_action(pending_action_id=...). This
keeps the approval gate enforced at the tool layer, independent of which agent
SDK is driving the conversation.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .data_store import get_store

INSTRUCTIONS = (
    "Tools for a daily-assistant agent covering one developer's Jira tickets, "
    "GitHub PRs/commits, Slack messages, Gmail threads, and Calendar — all backed "
    "by a frozen mock dataset. Call get_user_profile() to see whose data this is; "
    "'today'/'yesterday'/'this_week' in filters resolve against the dataset's "
    "frozen reference date, not the wall clock. Every factual claim must trace "
    "to a tool result — never invent ticket IDs, PR numbers, meeting times, or "
    "Slack messages; if something isn't found, say so or call escalate_to_user. "
    "update_jira_ticket, post_slack_message, and send_email only DRAFT a change "
    "and return a pending_action_id — nothing is applied until a human approves "
    "and you call confirm_action(pending_action_id=...). Never tell the user an "
    "action is done until confirm_action has actually returned status='confirmed'."
)

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8081"))
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")

mcp = FastMCP(
    "pulse-daily-assistant",
    instructions=INSTRUCTIONS,
    host=MCP_HOST,
    port=MCP_PORT,
)

if MCP_TRANSPORT != "stdio":
    # Network transports need to be reachable from other containers/hosts (the
    # whole point of this change). FastMCP only auto-enables DNS-rebinding
    # protection when host is a localhost variant; with MCP_HOST=0.0.0.0 (our
    # default for non-stdio transports) it already leaves transport_security
    # as None, which the security middleware treats as disabled — fine for
    # this mock bake-off sandbox. Set MCP_ALLOWED_HOSTS to a comma-separated
    # list of "host:port" / "host:*" values if you want the check back on
    # with an explicit allow-list instead.
    allowed_hosts_env = os.environ.get("MCP_ALLOWED_HOSTS")
    if allowed_hosts_env:
        hosts = [h.strip() for h in allowed_hosts_env.split(",") if h.strip()]
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=hosts,
        )


def _not_found(kind: str, ident: str) -> dict:
    return {"error": f"{kind} '{ident}' not found", "found": False}


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------


@mcp.tool()
def get_jira_tickets(
    assignee: str | None = None,
    status: list[str] | None = None,
    sprint: str | None = None,
    project: str | None = None,
    labels: list[str] | None = None,
    updated_since: str | None = None,
) -> list[dict]:
    """Get Jira tickets, optionally filtered by assignee, status, sprint, project,
    labels, or updated_since ('today'/'yesterday'/'this_week' or an ISO date)."""
    store = get_store()
    return store.query_tickets(
        assignee=assignee, status=status, sprint=sprint,
        project=project, labels=labels, updated_since=updated_since,
    )


@mcp.tool()
def get_jira_ticket_detail(ticket_id: str) -> dict:
    """Full detail for one Jira ticket: description, comments, status history, linked items."""
    store = get_store()
    ticket = store.find_ticket(ticket_id)
    if ticket is None:
        return _not_found("Jira ticket", ticket_id)
    return ticket


@mcp.tool()
def update_jira_ticket(ticket_id: str, fields: dict) -> dict:
    """Draft an update to ticket fields (status, assignee, story_points,
    completed_date, ...). Gated write — this only drafts the change and
    returns a pending_action_id; nothing is applied until a human approves
    and you call confirm_action(pending_action_id=...)."""
    store = get_store()
    ticket = store.find_ticket(ticket_id)
    if ticket is None:
        return _not_found("Jira ticket", ticket_id)
    record = store.draft_action("update_jira_ticket", {"ticket_id": ticket_id, "fields": fields})
    return {
        "status": "pending_approval",
        "pending_action_id": record["id"],
        "action": "update_jira_ticket",
        "ticket_id": ticket_id,
        "proposed_fields": fields,
        "current_values": {k: ticket.get(k) for k in fields},
        "message": (
            "Awaiting human approval before applying this update. Do NOT report it "
            f"as done. Call confirm_action(pending_action_id='{record['id']}') once approved."
        ),
    }


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


@mcp.tool()
def get_github_prs(
    author: str | None = None,
    reviewer: str | None = None,
    status: str | None = None,
    repo: str | None = None,
    updated_since: str | None = None,
) -> list[dict]:
    """Get GitHub PRs, optionally filtered by author, reviewer, status (open/merged/closed),
    repo, or updated_since."""
    store = get_store()
    return store.query_prs(
        author=author, reviewer=reviewer, status=status,
        repo=repo, updated_since=updated_since,
    )


@mcp.tool()
def get_github_pr_detail(pr_id: str) -> dict:
    """Full detail for one PR: diff stats, review comments, CI status, linked Jira ticket.
    Accepts either the full id (e.g. 'pulse-api#136') or a bare PR number."""
    store = get_store()
    pr = store.find_pr(pr_id)
    if pr is None:
        return _not_found("GitHub PR", pr_id)
    return pr


@mcp.tool()
def get_github_commits(
    repo: str | None = None,
    author: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Get recent commits, optionally filtered by repo, author, or since."""
    store = get_store()
    return store.query_commits(repo=repo, author=author, since=since)


@mcp.tool()
def link_jira_to_github(ticket_id: str, pr_url: str | None = None) -> dict:
    """Find the GitHub PR(s) linked to a Jira ticket, via commit messages
    (e.g. 'PROJ-101: ...') and the PR's linked_jira_ticket field. If pr_url is
    given, also checks whether that specific PR is one of the linked ones."""
    store = get_store()
    ticket = store.find_ticket(ticket_id)
    if ticket is None:
        return _not_found("Jira ticket", ticket_id)
    prs = store.prs_for_ticket(ticket_id)
    linked = [{**p, "url": store.pr_web_url(p)} for p in prs]
    result = {
        "ticket_id": ticket_id,
        "linked_prs": linked,
        "message": None if linked else "No linked GitHub PR found for this ticket.",
    }
    if pr_url is not None:
        target = store.find_pr_by_url(pr_url)
        result["pr_url"] = pr_url
        result["pr_url_matches_ticket"] = target is not None and any(p["id"] == target["id"] for p in prs)
    return result


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


@mcp.tool()
def get_slack_messages(
    channel: str | None = None,
    user: str | None = None,
    mentions: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Get recent Slack messages, optionally filtered by channel, user (author or DM
    participant), mentions (username mentioned), or since."""
    store = get_store()
    return store.query_slack_messages(channel=channel, user=user, mentions=mentions, since=since)


@mcp.tool()
def search_slack(
    query: str,
    channel: str | None = None,
    user: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Full-text search over Slack messages and DMs (keyword, optionally scoped by
    channel, user, or since)."""
    store = get_store()
    return store.search_slack(query, channel=channel, user=user, since=since)


@mcp.tool()
def post_slack_message(channel: str, message: str) -> dict:
    """Draft a Slack channel post. Gated write — this only drafts the message
    and returns a pending_action_id; nothing is posted until a human approves
    and you call confirm_action(pending_action_id=...)."""
    store = get_store()
    sender = store.current_user()["username"]
    record = store.draft_action("post_slack_message", {"channel": channel, "message": message, "user": sender})
    return {
        "status": "pending_approval",
        "pending_action_id": record["id"],
        "action": "post_slack_message",
        "channel": channel,
        "message": message,
        "prompt": f"Post this to #{channel} on Slack?",
        "next_step": f"Call confirm_action(pending_action_id='{record['id']}') once the human approves.",
    }


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


@mcp.tool()
def get_calendar_events(
    start_date: str,
    end_date: str,
    event_type: str | None = None,
    participant: str | None = None,
) -> list[dict]:
    """Get calendar events between start_date and end_date (each may be an ISO date
    or 'today'/'yesterday'/'tomorrow'/'this_week'/'next_week'/'last_week'),
    optionally filtered by event_type or participant username."""
    store = get_store()
    return store.query_calendar_events(
        start_date=start_date, end_date=end_date,
        event_type=event_type, participant=participant,
    )


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


@mcp.tool()
def get_gmail_threads(
    sender: str | None = None,
    subject_contains: str | None = None,
    label: str | None = None,
    unread: bool | None = None,
    since: str | None = None,
) -> list[dict]:
    """Get Gmail threads, optionally filtered by sender email, subject substring,
    label, unread status, or since."""
    store = get_store()
    return store.query_gmail_threads(
        sender=sender, subject_contains=subject_contains,
        label=label, unread=unread, since=since,
    )


@mcp.tool()
def get_gmail_thread_detail(thread_id: str) -> dict:
    """Full detail for one email thread: all messages, participants, labels."""
    store = get_store()
    thread = store.find_gmail_thread(thread_id)
    if thread is None:
        return _not_found("Gmail thread", thread_id)
    return thread


@mcp.tool()
def send_email(to: list[str], subject: str, body: str) -> dict:
    """Draft an email. Gated write — this only drafts it and returns a
    pending_action_id; nothing is sent until a human approves and you call
    confirm_action(pending_action_id=...)."""
    store = get_store()
    sender = store.current_user()["email"]
    record = store.draft_action("send_email", {"to": to, "subject": subject, "body": body, "sender": sender})
    return {
        "status": "pending_approval",
        "pending_action_id": record["id"],
        "action": "send_email",
        "to": to,
        "subject": subject,
        "body": body,
        "prompt": f"Send this email to {', '.join(to)}?",
        "next_step": f"Call confirm_action(pending_action_id='{record['id']}') once the human approves.",
    }


# ---------------------------------------------------------------------------
# Approval / user / escalation
# ---------------------------------------------------------------------------


@mcp.tool()
def confirm_action(pending_action_id: str) -> dict:
    """Execute a previously-drafted write action (from update_jira_ticket,
    post_slack_message, or send_email) after a human has approved it. This is
    the only way those drafted actions actually take effect — call it only
    after receiving explicit human approval for that pending_action_id."""
    store = get_store()
    return store.confirm_action(pending_action_id)


@mcp.tool()
def get_user_profile() -> dict:
    """Get the current developer's profile (name, team, role, working hours, timezone)."""
    store = get_store()
    return store.current_user()


@mcp.tool()
def escalate_to_user(reason: str) -> dict:
    """Notify the user that manual intervention or a decision is needed —
    call this when data is missing, permissions are lacking, or the request
    is out of scope for the available tools."""
    store = get_store()
    entry = {"type": "escalation", "reason": reason}
    store.sent_log.append(entry)
    return {"status": "escalated", "reason": reason}


def main() -> None:
    mcp.run(transport=MCP_TRANSPORT)


if __name__ == "__main__":
    main()
