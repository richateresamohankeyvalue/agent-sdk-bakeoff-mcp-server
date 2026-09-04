"""Google Calendar API client — replaces the calendar_events.json mock.

Reference: https://developers.google.com/workspace/calendar/api/guides

Known gap: real Calendar events have no "type" field the way the mock did
(standup/1:1/code_review/...) — `_guess_event_type()` does a best-effort
keyword match against the summary/description, so `event_type` is a fuzzy
filter here, not an exact one.
"""

from __future__ import annotations

from datetime import datetime

from googleapiclient.discovery import build as build_service

from .. import time_utils
from .google_auth import get_credentials

_EVENT_TYPE_KEYWORDS = {
    "standup": ["standup", "stand-up", "daily sync"],
    "one_on_one": ["1:1", "1-1", "one on one", "one-on-one"],
    "code_review": ["code review"],
    "design_review": ["design review"],
    "retro": ["retro", "retrospective"],
    "sprint_planning": ["sprint planning", "planning"],
    "all_hands": ["all hands", "all-hands", "town hall"],
    "team_sync": ["team sync", "sync"],
    "incident": ["incident", "postmortem", "post-mortem"],
    "interview": ["interview"],
    "client_call": ["client call", "client meeting"],
    "focus_block": ["focus block", "focus time", "deep work"],
    "out_of_office": ["out of office", "ooo", "pto", "vacation"],
}


def _service():
    return build_service("calendar", "v3", credentials=get_credentials(), cache_discovery=False)


def _guess_event_type(summary: str, description: str) -> str | None:
    haystack = f"{summary} {description}".lower()
    for tag, keywords in _EVENT_TYPE_KEYWORDS.items():
        if any(k in haystack for k in keywords):
            return tag
    return None


def _day_bounds(value: str, bound: str) -> datetime:
    d = time_utils.resolve_date(value, bound=bound)
    if bound == "start":
        return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=time_utils.DEFAULT_TZ)
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=time_utils.DEFAULT_TZ)


def query_events(
    start_date: str,
    end_date: str,
    event_type: str | None = None,
    participant: str | None = None,
) -> list[dict]:
    time_min = _day_bounds(start_date, "start")
    time_max = _day_bounds(end_date, "end")

    service = _service()
    events: list[dict] = []
    page_token = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                pageToken=page_token,
            )
            .execute()
        )
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    out = [_normalize_event(e) for e in events]
    if event_type:
        wanted = event_type.lower()
        out = [e for e in out if e["type"] == wanted or wanted in e["title"].lower()]
    if participant:
        needle = participant.lower()
        out = [
            e for e in out
            if any(needle in p.lower() for p in e["participants"]) or needle in (e["organizer"] or "").lower()
        ]
    return sorted(out, key=lambda e: e["start"])


def _normalize_event(event: dict) -> dict:
    start = event.get("start", {})
    end = event.get("end", {})
    attendees = event.get("attendees", [])
    summary = event.get("summary", "")
    description = event.get("description", "")
    return {
        "id": event["id"],
        "type": _guess_event_type(summary, description),
        "title": summary,
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": "date" in start and "dateTime" not in start,
        "organizer": (event.get("organizer") or {}).get("email"),
        "participants": [a.get("email") for a in attendees if a.get("email")],
        "location": event.get("location"),
        "description": description,
        "recurring": "recurringEventId" in event,
    }
