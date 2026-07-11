# agent-sdk-bakeoff-mcp-server

MCP server + mock dataset (Jira, GitHub, Slack, Gmail, Calendar) for the AI Agent Toolkit
Bake-off's Daily Assistant agent — the shared, framework-agnostic tool layer every candidate
SDK (Claude Agent SDK, OpenAI Agents SDK, Agno, Mastra, DeepAgents, Hermes) plugs into.

## 1. Running it

**Start the server (Docker, recommended — one shared instance every SDK connects to):**

```bash
docker compose up --build
# → SSE endpoint at http://localhost:8081/sse
```

`data/` is mounted into the container, so editing the JSON on the host is picked up live —
no rebuild or restart needed.

**Connect an agent SDK to it** (example: Claude Agent SDK / Claude Code):

```json
mcpServers: {"pulse-assistant": {"type": "sse", "url": "http://localhost:8081/sse"}}
```

Or run it locally without Docker, over stdio (single SDK spawns it directly):
```bash
uv run pulse-assistant-mcp
# mcpServers config: {"command": "uv", "args": ["--directory", "/path/to/repo", "run", "pulse-assistant-mcp"]}
```

**Start the visualization UI:**

```bash
./viewer/serve.sh
# then open http://localhost:8765/viewer/ in a browser
```

Browses the dataset with search/filters and cross-source links; auto-refreshes from
`data/*.json`, so edits show up on the next refresh.

## 2. The dataset

All fictitious — company "Nimbus Labs", product "Pulse", current user **Aisha Khan** (a
backend engineer; `get_user_profile()` returns her). Frozen reference date `today =
2025-06-18` so "today"/"yesterday"/"this_week" filters stay reproducible.

| File | Contents |
|---|---|
| `jira_tickets.json` | 30 tickets across `PROJ`/`INFRA`/`BUG`, full status workflow, comments, status history, linked tickets, due/completed dates |
| `github_prs.json` + `github_commits.json` | 30 PRs across 3 repos + ~33 commits (last 7 days); commit messages follow `"TICKET-ID: ..."` so tickets resolve to PRs the way GitHub itself would |
| `slack_data.json` | 4 channels + DMs, 30 conversations (67 messages), all within a 3-day window |
| `gmail_threads.json` | 30 threads with independent `unread`/`flagged` booleans |
| `calendar_events.json` | 30 events — standups, sprint planning, 1:1s, code reviews, a real double-booking conflict |

Everything is cross-linked on purpose (ticket → PR → Slack thread → email, sprint → tickets →
PRs → standup chatter), including deliberate negative cases — some tickets have no linked PR
and vice versa — so grounded/no-hallucination behavior has something real to be tested against.

## 3. Tools

| Tool | Type | Description |
|---|---|---|
| `get_jira_tickets(assignee, status, sprint, project, labels, updated_since)` | read | List Jira tickets, filtered any combination of the above |
| `get_jira_ticket_detail(ticket_id)` | read | Full ticket: description, comments, status history, linked tickets |
| `update_jira_ticket(ticket_id, fields)` | write — gated | Draft a field update (status, assignee, story_points, ...); returns a `pending_action_id` |
| `get_github_prs(author, reviewer, status, repo, updated_since)` | read | List PRs, filtered by author/reviewer/status/repo/date |
| `get_github_pr_detail(pr_id)` | read | Full PR: diff stats, review comments, CI status, linked ticket |
| `get_github_commits(repo, author, since)` | read | Recent commits, filtered by repo/author/date |
| `link_jira_to_github(ticket_id, pr_url)` | read | Resolve a ticket to its linked PR(s) via commit-message scan; if `pr_url` is given, also reports whether that specific PR matches |
| `get_slack_messages(channel, user, mentions, since)` | read | Recent messages/DMs, filtered by channel/user/mentions/date |
| `search_slack(query, channel, user, since)` | read | Full-text keyword search over Slack |
| `post_slack_message(channel, message)` | write — gated | Draft a channel post; returns a `pending_action_id` |
| `get_calendar_events(start_date, end_date, event_type, participant)` | read | Events in a date range (accepts `today`/`this_week`/etc.), filtered by type/participant |
| `get_gmail_threads(sender, subject_contains, label, unread, since)` | read | List email threads, filtered by sender/subject/label/unread/date |
| `get_gmail_thread_detail(thread_id)` | read | Full thread: every message, participants, labels |
| `send_email(to, subject, body)` | write — gated | Draft an email; returns a `pending_action_id` |
| `confirm_action(pending_action_id)` | action | Executes a previously-drafted write, after human approval — the only way drafted writes take effect |
| `get_user_profile()` | read | Current developer's profile: name, team, role, working hours, timezone |
| `escalate_to_user(reason)` | action | Flags that manual intervention or a decision is needed |
