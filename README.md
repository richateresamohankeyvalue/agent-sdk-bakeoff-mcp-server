# agent-sdk-bakeoff-mcp-server

MCP server (Jira, GitHub, Slack, Gmail, Calendar) for the AI Agent Toolkit Bake-off's Daily
Assistant agent — the shared, framework-agnostic tool layer every candidate SDK (Claude Agent
SDK, OpenAI Agents SDK, Agno, Mastra, DeepAgents, Hermes) plugs into.

Every tool hits the real service directly (JQL against Jira Cloud, the GitHub REST/Search API,
the Slack Web API, and the Gmail/Calendar APIs) — there's no mock dataset backing responses
anymore. `data/*.json` + `viewer/` still exist as a static, disconnected legacy demo of the old
mock dataset; they aren't read by the live server.

## 1. Credentials

Copy `.env.example` to `.env` (gitignored) and fill in real values. Without them, a tool just
returns `{"error": "... not configured ..."}` instead of live data.

```bash
cp .env.example .env
```

### Jira

Your own token — one per person.
1. id.atlassian.com → Security → API tokens → Create API token → copy it.
2. Set:

| Var | Value |
|---|---|
| `JIRA_BASE_URL` | e.g. `https://your-domain.atlassian.net` |
| `JIRA_EMAIL` | your Atlassian account email |
| `JIRA_API_TOKEN` | the token from step 1 |

Optional: `JIRA_SPRINT_FIELD`, `JIRA_STORY_POINTS_FIELD` (Jira custom field IDs, only needed to
read those values back), `JIRA_DEFAULT_LOOKBACK_DAYS` (default `90`, rarely needs changing).

### GitHub

Your own token — one per person.
1. github.com/settings/tokens → Generate new token (classic) → check the `repo` scope → Generate.
2. Set:

| Var | Value |
|---|---|
| `GITHUB_TOKEN` | the token from step 1 |
| `GITHUB_ORG` | the org or username the repos live under (works for either) |
| `GITHUB_REPOS` (optional) | comma-separated repo names, to limit which ones get queried |

### Slack

A Slack app already exists for this workspace — get the token from whoever set it up (or
api.slack.com/apps if you're an admin) and paste it in:

```
SLACK_BOT_TOKEN=xoxb-...
```

**Whose name do actions run under?** The Slack app/bot's — not yours personally. A message
posted via `post_slack_message` shows up as the app, the same way no matter who's running the
server.

Optional: `SLACK_USER_TOKEN` (`xoxp-...`) enables real full-text search in `search_slack`;
without it, search just scans recent history instead.

<details>
<summary>No Slack app exists yet? Create one.</summary>

1. api.slack.com/apps → Create New App → From scratch. Name it, pick the workspace.
2. OAuth & Permissions → add Bot Token Scopes: `channels:history`, `groups:history`,
   `im:history`, `mpim:history`, `channels:read`, `groups:read`, `im:read`, `mpim:read`,
   `users:read`, `chat:write`. (For real search) also add User Token Scope `search:read`.
3. Click Install to Workspace → approve. Both tokens appear on that page.
</details>

### Gmail + Calendar

A Google project already exists too:
1. Get `client_secret.json` from whoever set it up, and put it in the repo root.
2. Run:
   ```bash
   uv run python scripts/google_auth_setup.py
   ```
   This opens a browser for you to log in with your own Google account.

**Whose name do actions run under?** Yours — step 2 is a personal login, so Gmail/Calendar
tools read and act on *your own* mailbox and calendar, not anyone else's. Everyone on the team
runs step 2 themselves; only `client_secret.json` (the app's identity) is shared.

### Your profile

`get_user_profile()` doesn't fetch from anywhere — no API exposes "team/role/working hours" in
one place — so it just echoes back whatever you add to the same `.env` file:

```
USER_NAME=Jane Doe
USER_EMAIL=jane@company.com
USER_TEAM=Platform
USER_ROLE=Backend Engineer
USER_TIMEZONE=Asia/Kolkata
USER_WORKING_HOURS=09:30-18:30
```

### Write actions

`update_jira_ticket`, `post_slack_message`, `send_email` are **off by default**. Set
`WRITES_ENABLED=true` to turn them on — even then, they only draft a change and wait for a
human to call `confirm_action(pending_action_id=...)` before anything actually happens.

## 2. Running it

**Start the server (Docker, recommended — one shared instance every SDK connects to):**

```bash
docker compose up --build
# → SSE endpoint at http://localhost:8081/sse
```

Docker reads the same `.env` as local runs (`env_file: .env` in `docker-compose.yml`), and
mounts `token.json` read-only so the Google OAuth token from `scripts/google_auth_setup.py` is
visible in the container.

**Connect an agent SDK to it** (example: Claude Agent SDK / Claude Code):

```json
mcpServers: {"pulse-assistant": {"type": "sse", "url": "http://localhost:8081/sse"}}
```

Or run it locally without Docker, over stdio (single SDK spawns it directly):
```bash
uv run pulse-assistant-mcp
# mcpServers config: {"command": "uv", "args": ["--directory", "/path/to/repo", "run", "pulse-assistant-mcp"]}
```

**Legacy mock-dataset viewer** (browses the old frozen fixtures in `data/*.json`, unrelated to
the live tools above):

```bash
./viewer/serve.sh
# then open http://localhost:8765/viewer/ in a browser
```

## 3. Tools

| Tool | Type | Description |
|---|---|---|
| `get_jira_tickets(assignee, status, sprint, project, labels, updated_since)` | read | List Jira tickets via JQL, filtered any combination of the above |
| `get_jira_ticket_detail(ticket_id)` | read | Full ticket: description, comments, status history, linked tickets |
| `update_jira_ticket(ticket_id, fields)` | write — gated, disabled by default | Draft a field update (status, assignee, story_points, ...); returns a `pending_action_id` |
| `get_github_prs(author, reviewer, status, repo, updated_since)` | read | List PRs via the GitHub Search API, filtered by author/reviewer/status/repo/date |
| `get_github_pr_detail(pr_id)` | read | Full PR: diff stats, review comments, CI status |
| `get_github_commits(repo, author, since)` | read | Recent commits, filtered by repo/author/date |
| `link_jira_to_github(ticket_id, pr_url)` | read | Resolve a ticket to its linked PR(s) via commit-message scan; if `pr_url` is given, also reports whether that specific PR matches |
| `get_slack_messages(channel, user, mentions, since)` | read | Recent messages/DMs, filtered by channel/user/mentions/date |
| `search_slack(query, channel, user, since)` | read | Full-text search via Slack's search API (needs `SLACK_USER_TOKEN`) or a client-side fallback scan |
| `post_slack_message(channel, message)` | write — gated, disabled by default | Draft a channel post; returns a `pending_action_id` |
| `get_calendar_events(start_date, end_date, event_type, participant)` | read | Events in a date range (accepts `today`/`this_week`/etc.), filtered by type (best-effort match)/participant |
| `get_gmail_threads(sender, subject_contains, label, unread, since)` | read | List email threads via Gmail search operators, filtered by sender/subject/label/unread/date |
| `get_gmail_thread_detail(thread_id)` | read | Full thread: every message, participants, labels |
| `send_email(to, subject, body)` | write — gated, disabled by default | Draft an email; returns a `pending_action_id` |
| `confirm_action(pending_action_id)` | action | Executes a previously-drafted write, after human approval — the only way drafted writes take effect |
| `get_user_profile()` | read | Current developer's profile: name, team, role, working hours, timezone (local config, not fetched) |
| `escalate_to_user(reason)` | action | Flags that manual intervention or a decision is needed |
