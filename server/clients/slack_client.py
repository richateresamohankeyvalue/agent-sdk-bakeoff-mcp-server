"""Slack Web API client — replaces the slack_data.json mock.

Known gaps vs. the old mock:
- `conversations.history` only returns top-level channel messages; it does
  not walk into thread replies (that needs a per-thread `conversations.replies`
  call), so deep thread replies won't surface here.
- `search.messages` (used for real full-text search) requires a *user* token
  with the `search:read` scope — bot tokens can't call it. If SLACK_USER_TOKEN
  isn't set, search_slack() falls back to scanning recent history
  (SLACK_SEARCH_LOOKBACK_DAYS) and filtering client-side, which is narrower
  than real search.
"""

from __future__ import annotations

import re
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
    resp = client.post(f"/{method}", data=params)
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
_auth_identity_cache: str | None = None


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


def _auth_identity(client: httpx.Client) -> str | None:
    """Username of the token's own identity — used to fill out the
    "other" participant slot in DM conversations."""
    global _auth_identity_cache
    if _auth_identity_cache is None:
        data = _call(client, "auth.test")
        _auth_identity_cache = data.get("user")
    return _auth_identity_cache


def _user_id_by_username(client: httpx.Client, username: str) -> str | None:
    for uid, user in _users(client).items():
        if user.get("name") == username:
            return uid
    return None


def _conversations(client: httpx.Client) -> dict[str, dict]:
    global _channel_cache
    if _channel_cache is None:
        convos = _paginate(
            client, "conversations.list", "channels",
            types="public_channel,private_channel,mpim,im",
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
    is_im = convo.get("is_im")
    is_mpim = convo.get("is_mpim")
    participants = None
    if is_im:
        participants = [_username(client, convo.get("user")), _auth_identity(client)]
    return {
        "id": msg.get("client_msg_id") or msg.get("ts"),
        "ts": time_utils.parse_dt(_slack_ts_to_iso(msg["ts"])).isoformat(),
        "user": _username(client, msg.get("user")),
        "text": msg.get("text", ""),
        "mentions": [_username(client, uid) for uid in mention_ids],
        "channel": convo.get("name") if not (is_im or is_mpim) else None,
        "conversation_id": convo["id"],
        "type": "im" if is_im else "mpim" if is_mpim else "channel",
        "participants": participants,
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
        oldest = None
        if since:
            oldest = str(time_utils.resolve_since(since).timestamp())

        candidate_convos: list[dict]
        if channel:
            cid = _resolve_channel_id(client, channel)
            candidate_convos = [_conversations(client)[cid]] if cid else []
        else:
            # `user` matches either message author or DM participant, both of
            # which can appear in any conversation, so there's no narrower
            # candidate set to compute up front — filtered per-message below.
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
                if user:
                    is_author = normalized["user"] == user
                    is_dm_participant = normalized["participants"] and user in normalized["participants"]
                    if not (is_author or is_dm_participant):
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
    # Fallback: no user token, so no real full-text search available —
    # scan recent history and filter client-side (narrower than real search).
    if since:
        effective_since = since
    else:
        bound_date = time_utils.get_today() - timedelta(days=settings.slack.search_lookback_days)
        effective_since = bound_date.isoformat()
    results = query_messages(channel=channel, user=user, since=effective_since)
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
