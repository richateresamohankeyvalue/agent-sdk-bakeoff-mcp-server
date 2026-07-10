"""Resolves the symbolic time expressions used throughout the tool set
("today", "yesterday", "this_week", ...) against the dataset's frozen
reference date in data/meta.json, so the mock data stays reproducible
regardless of when the harness actually runs.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

DEFAULT_TZ = timezone(timedelta(hours=5, minutes=30))  # Asia/Kolkata

_reference_today: date | None = None


def set_reference_today(value: date | str | None) -> None:
    """Override 'today' for this process. Used by task config's date/time context."""
    global _reference_today
    if value is None:
        _reference_today = None
    elif isinstance(value, date):
        _reference_today = value
    else:
        _reference_today = date.fromisoformat(value)


def get_today(default_today: str) -> date:
    if _reference_today is not None:
        return _reference_today
    return date.fromisoformat(default_today)


def _start_of_day(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=DEFAULT_TZ)


def _end_of_day(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=DEFAULT_TZ)


def week_bounds(today: date) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def resolve_date(value: str | None, today_str: str, bound: str = "start") -> date | None:
    """Resolves a single date-ish token: 'today'/'yesterday'/'tomorrow', an ISO date,
    or a week keyword ('this_week'/'next_week'/'last_week') — for week keywords,
    `bound` picks the Monday ('start') or Sunday ('end') of that week."""
    if value is None:
        return None
    today = get_today(today_str)
    v = value.strip().lower()
    if v == "today":
        return today
    if v == "yesterday":
        return today - timedelta(days=1)
    if v == "tomorrow":
        return today + timedelta(days=1)
    if v in ("this_week", "next_week", "last_week"):
        offset = {"this_week": 0, "next_week": 7, "last_week": -7}[v]
        monday, sunday = week_bounds(today + timedelta(days=offset))
        return monday if bound == "start" else sunday
    return date.fromisoformat(value)


def resolve_since(value: str | None, today_str: str) -> datetime | None:
    """Resolves a 'since' filter to an inclusive lower-bound datetime."""
    if value is None:
        return None
    today = get_today(today_str)
    v = value.strip().lower()
    if v == "today":
        return _start_of_day(today)
    if v == "yesterday":
        return _start_of_day(today - timedelta(days=1))
    if v == "this_week":
        monday, _ = week_bounds(today)
        return _start_of_day(monday)
    if v == "last_week":
        monday, _ = week_bounds(today - timedelta(days=7))
        return _start_of_day(monday)
    try:
        d = date.fromisoformat(value)
        return _start_of_day(d)
    except ValueError:
        pass
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DEFAULT_TZ)
    return dt


def resolve_range(value: str | None, today_str: str) -> tuple[date, date] | None:
    """Resolves a symbolic range keyword ('this_week', 'today', ...) to (start, end) dates."""
    if value is None:
        return None
    today = get_today(today_str)
    v = value.strip().lower()
    if v == "today":
        return today, today
    if v == "yesterday":
        d = today - timedelta(days=1)
        return d, d
    if v == "tomorrow":
        d = today + timedelta(days=1)
        return d, d
    if v == "this_week":
        return week_bounds(today)
    if v == "next_week":
        return week_bounds(today + timedelta(days=7))
    if v == "last_week":
        return week_bounds(today - timedelta(days=7))
    d = date.fromisoformat(value)
    return d, d


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DEFAULT_TZ)
    return dt


def in_day_range(dt: datetime, start: date, end: date) -> bool:
    d = dt.astimezone(DEFAULT_TZ).date()
    return start <= d <= end
