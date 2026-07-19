"""Gmail API client — replaces the gmail_threads.json mock.

Reference: https://developers.google.com/workspace/gmail/api/guides/list-messages
Filters map onto Gmail's own search operators (from:, subject:, label:,
is:unread/is:read, after:) rather than being applied client-side, same
approach as the Jira/GitHub clients.

Known gap: Gmail's "flagged" isn't a real concept — mapped from presence of
the built-in STARRED label.
"""

from __future__ import annotations

import base64
import re
from email.mime.text import MIMEText

from googleapiclient.discovery import build as build_service
from googleapiclient.errors import HttpError

from .. import time_utils
from ..config import settings
from .google_auth import get_credentials

_label_cache: dict[str, str] | None = None


def _service():
    return build_service("gmail", "v1", credentials=get_credentials(), cache_discovery=False)


def build_query(
    sender: str | None = None,
    subject_contains: str | None = None,
    label: str | None = None,
    unread: bool | None = None,
    since: str | None = None,
) -> str:
    """Pure function: filter args -> Gmail search query string."""
    terms: list[str] = []
    if sender:
        terms.append(f"from:{sender}")
    if subject_contains:
        needle = f'"{subject_contains}"' if " " in subject_contains else subject_contains
        terms.append(f"subject:{needle}")
    if label:
        needle = f'"{label}"' if " " in label else label
        terms.append(f"label:{needle}")
    if unread is not None:
        terms.append("is:unread" if unread else "is:read")
    if since:
        bound = time_utils.resolve_since(since)
        terms.append(f"after:{bound.strftime('%Y/%m/%d')}")
    return " ".join(terms)


def _labels(service) -> dict[str, str]:
    global _label_cache
    if _label_cache is None:
        resp = service.users().labels().list(userId="me").execute()
        _label_cache = {label["id"]: label["name"] for label in resp.get("labels", [])}
    return _label_cache


def _headers_dict(headers: list[dict]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in headers}


def _decode_body_data(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    if payload.get("body", {}).get("data"):
        text = _decode_body_data(payload["body"]["data"])
        if payload.get("mimeType") == "text/html":
            text = re.sub(r"<[^>]+>", "", text)
        return text
    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/plain":
            return _extract_body(part)
    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/html":
            return _extract_body(part)
    return ""


def _normalize_thread(thread: dict, label_map: dict[str, str]) -> dict:
    messages = thread.get("messages", [])
    normalized_messages = []
    participants: set[str] = set()
    label_ids: set[str] = set()
    for msg in messages:
        headers = _headers_dict(msg["payload"].get("headers", []))
        label_ids.update(msg.get("labelIds", []))
        to = [a.strip() for a in headers.get("to", "").split(",") if a.strip()]
        cc = [a.strip() for a in headers.get("cc", "").split(",") if a.strip()]
        participants.update([headers.get("from", "")] + to + cc)
        normalized_messages.append(
            {
                "from": headers.get("from"),
                "to": to,
                "cc": cc,
                "date": headers.get("date"),
                "body": _extract_body(msg["payload"]).strip(),
            }
        )
    last = messages[-1] if messages else {}
    last_headers = _headers_dict(last.get("payload", {}).get("headers", [])) if last else {}
    label_names = {label_map.get(lid, lid) for lid in label_ids}
    return {
        "id": thread["id"],
        "subject": last_headers.get("subject"),
        "participants": sorted(p for p in participants if p),
        "labels": sorted(label_names - {"UNREAD", "STARRED"}),
        "unread": "UNREAD" in label_ids,
        "flagged": "STARRED" in label_ids,
        "date": last_headers.get("date"),
        "messages": normalized_messages,
    }


def query_threads(
    sender: str | None = None,
    subject_contains: str | None = None,
    label: str | None = None,
    unread: bool | None = None,
    since: str | None = None,
) -> list[dict]:
    service = _service()
    query = build_query(sender, subject_contains, label, unread, since)
    label_map = _labels(service)
    cap = settings.google.gmail_max_threads

    # Gmail returns threads newest-first by default, so capping here keeps
    # the most recent ones — but an unfiltered call (query="") still means
    # "every thread in the mailbox," so without a cap this would fetch full
    # message content for the entire account, one API call per thread.
    thread_ids: list[str] = []
    page_token = None
    while len(thread_ids) < cap:
        resp = (
            service.users()
            .threads()
            .list(userId="me", q=query or None, pageToken=page_token, maxResults=min(100, cap - len(thread_ids)))
            .execute()
        )
        thread_ids.extend(t["id"] for t in resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    threads = []
    for tid in thread_ids[:cap]:
        detail = service.users().threads().get(userId="me", id=tid, format="full").execute()
        threads.append(_normalize_thread(detail, label_map))
    return sorted(threads, key=lambda t: t["date"] or "", reverse=True)


def get_thread(thread_id: str) -> dict | None:
    service = _service()
    label_map = _labels(service)
    try:
        detail = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            return None
        raise
    return _normalize_thread(detail, label_map)


def send_message(to: list[str], subject: str, body: str, sender: str) -> dict:
    """Only reached when WRITES_ENABLED=true — also requires the token to
    have been consented with the gmail.send scope (not in the default
    read-only SCOPES in google_auth.py)."""
    service = _service()
    message = MIMEText(body)
    message["to"] = ", ".join(to)
    message["from"] = sender
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"id": sent.get("id"), "thread_id": sent.get("threadId"), "to": to, "subject": subject}
