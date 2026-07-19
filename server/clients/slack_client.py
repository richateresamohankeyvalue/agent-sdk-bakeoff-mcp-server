"""Slack Web API client — replaces the slack_data.json mock.

Scoped to channels only (public + private) — DMs/group-DMs are deliberately
excluded, not just unsupported.

Known gaps vs. the old mock:
- `conversations.history` only returns top-level channel messages; it does
  not walk into thread replies (that needs a per-thread `conversations.replies`
  call), so deep thread replies won't surface here.
- `search.messages` (used for real full-text search) requires a *user* token
  with the `search:read` scope — bot tokens can't call it. If SLACK_USER_TOKEN
  isn't set, search_slack() falls back to scanning recent history
  (SLACK_DEFAULT_LOOKBACK_DAYS) and filtering client-side, which is narrower
  than real search.
- Listing across every conversation (no `channel` filter) is still one
  `conversations.history` call per channel the bot is a member of — fine
  for a bot in a handful of channels, but scales with however many it's
  actually been invited to. `_call()` retries on 429 using the
  `Retry-After` header as a backstop either way.
"""

from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Any

import httpx

from .. import time_utils
from ..config import settings
from .errors import NotConfiguredError

API_BASE = "https://slack.com/api"
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


def _require_configured() -> None:
    if not settings.slack.configured:
        raise NotConfiguredError("Slack is not configured (need SLACK_BOT_TOKEN)")


def _client(token: str | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {token or settings.slack.bot_token}"},
        timeout=20.0,
    )


def _call(client: httpx.Client, method: str, **params: Any) -> dict:
    # POST (not GET) for every method — required for the state-changing ones
    # (chat.postMessage) and harmless for the read ones.
    for attempt in range(4):
        resp = client.post(f"/{method}", data=params)
        if resp.status_code == 429 and attempt < 3:
            time.sleep(int(resp.headers.get("Retry-After", "1")))
            continue
        break
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error on {method}: {data.get('error')}")
    return data


def _paginate(client: httpx.Client, method: str, items_key: str, **params: Any) -> list[dict]:
    out: list[dict] = []
    cursor = None
    while True:
        call_params = {**params, "limit": 200}
        if cursor:
            call_params["cursor"] = cursor
        data = _call(client, method, **call_params)
        out.extend(data.get(items_key, []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return out


_user_cache: dict[str, dict] | None = None
_channel_cache: dict[str, dict] | None = None


def _users(client: httpx.Client) -> dict[str, dict]:
    global _user_cache
    if _user_cache is None:
        members = _paginate(client, "users.list", "members")
        _user_cache = {m["id"]: m for m in members}
    return _user_cache


def _username(client: httpx.Client, user_id: str | None) -> str | None:
    if not user_id:
        return None
    user = _users(client).get(user_id)
    return (user or {}).get("name") or user_id


def _user_id_by_username(client: httpx.Client, username: str) -> str | None:
    for uid, user in _users(client).items():
        if user.get("name") == username:
            return uid
    return None


def _conversations(client: httpx.Client) -> dict[str, dict]:
    """Channels (public + private) the bot is actually a member of — no
    DMs/group-DMs. Uses `users.conversations` (membership-scoped — Slack
    only enumerates conversations the calling bot/user is actually in)
    rather than `conversations.list` (which returns *every* public channel
    in the whole workspace regardless of membership). That mismatch was why
    scanning "every conversation" burned so much rate-limit budget: most of
    what conversations.list returned were public channels the bot had never
    been invited to, so every history call against them failed anyway —
    just at the cost of a wasted request."""
    global _channel_cache
    if _channel_cache is None:
        convos = _paginate(
            client, "users.conversations", "channels",
            types="public_channel,private_channel",
        )
        _channel_cache = {c["id"]: c for c in convos}
    return _channel_cache


def _resolve_channel_id(client: httpx.Client, channel_name: str) -> str | None:
    for cid, convo in _conversations(client).items():
        if convo.get("name") == channel_name:
            return cid
    return None


def _normalize_message(client: httpx.Client, msg: dict, convo: dict) -> dict:
    mention_ids = _MENTION_RE.findall(msg.get("text", ""))
    return {
        "id": msg.get("client_msg_id") or msg.get("ts"),
        "ts": time_utils.parse_dt(_slack_ts_to_iso(msg["ts"])).isoformat(),
        "user": _username(client, msg.get("user")),
        "text": msg.get("text", ""),
        "mentions": [_username(client, uid) for uid in mention_ids],
        "channel": convo.get("name"),
        "conversation_id": convo["id"],
    }


def _slack_ts_to_iso(ts: str) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(time_utils.DEFAULT_TZ).isoformat()


def query_messages(
    channel: str | None = None,
    user: str | None = None,
    mentions: str | None = None,
    since: str | None = None,
) -> list[dict]:
    _require_configured()
    with _client() as client:
        # Without a `since`, "every conversation" would otherwise mean
        # fetching each one's *entire* history — default to a bounded
        # lookback instead (same reasoning as Gmail's thread cap).
        if since:
            bound = time_utils.resolve_since(since)
        else:
            bound = time_utils.get_today() - timedelta(days=settings.slack.default_lookback_days)
            bound = time_utils.resolve_since(bound.isoformat())
        oldest = str(bound.timestamp())

        candidate_convos: list[dict]
        if channel:
            cid = _resolve_channel_id(client, channel)
            candidate_convos = [_conversations(client)[cid]] if cid else []
        else:
            candidate_convos = list(_conversations(client).values())

        out: list[dict] = []
        for convo in candidate_convos:
            if convo.get("is_archived"):
                continue
            history_params: dict[str, Any] = {"channel": convo["id"]}
            if oldest:
                history_params["oldest"] = oldest
            try:
                messages = _paginate(client, "conversations.history", "messages", **history_params)
            except RuntimeError:
                continue  # bot isn't a member of this conversation
            for msg in messages:
                if msg.get("subtype"):
                    continue
                normalized = _normalize_message(client, msg, convo)
                if user and normalized["user"] != user:
                    continue
                if mentions and mentions not in (normalized.get("mentions") or []):
                    continue
                out.append(normalized)
    return sorted(out, key=lambda m: m["ts"])


def search_messages(
    query: str,
    channel: str | None = None,
    user: str | None = None,
    since: str | None = None,
) -> list[dict]:
    _require_configured()
    if settings.slack.user_token:
        with _client(token=settings.slack.user_token) as client:
            search_query = query
            if channel:
                search_query += f" in:#{channel}"
            if user:
                search_query += f" from:@{user}"
            data = _call(client, "search.messages", query=search_query, count=100)
        return [
            {
                "ts": time_utils.parse_dt(_slack_ts_to_iso(m["ts"])).isoformat(),
                "user": (m.get("username")),
                "text": m.get("text", ""),
                "channel": (m.get("channel") or {}).get("name"),
            }
            for m in data.get("messages", {}).get("matches", [])
        ]
    # Fallback: no user token, so no real full-text search available — scan
    # recent history and filter client-side (narrower than real search).
    # query_messages() already applies the default lookback when since=None.
    results = query_messages(channel=channel, user=user, since=since)
    needle = query.lower()
    return [m for m in results if needle in m["text"].lower()]


def post_message(channel: str, message: str) -> dict:
    _require_configured()
    with _client() as client:
        cid = _resolve_channel_id(client, channel)
        if cid is None:
            raise RuntimeError(f"Unknown Slack channel: {channel}")
        data = _call(client, "chat.postMessage", channel=cid, text=message)
    return {"channel": channel, "ts": data.get("ts"), "text": message}
