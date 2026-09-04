"""In-memory pending-action store for the two-step draft/confirm write gate.

draft_action() never mutates anything — confirm_action() is the only place
that dispatches to a real client's write function (jira_client.update_issue,
slack_client.post_message, gmail_client.send_message), and only after a
human has approved that specific pending_action_id.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .clients import gmail_client, jira_client, slack_client

_pending: dict[str, dict] = {}
sent_log: list[dict] = []  # audit trail of executed writes + escalations


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def draft_action(action_type: str, payload: dict) -> dict:
    action_id = uuid.uuid4().hex[:12]
    record = {
        "id": action_id,
        "action_type": action_type,
        "payload": payload,
        "status": "pending",
        "created_at": _now_iso(),
    }
    _pending[action_id] = record
    return record


def confirm_action(action_id: str) -> dict:
    record = _pending.get(action_id)
    if record is None:
        return {"error": f"No pending action with id '{action_id}'", "found": False}
    if record["status"] != "pending":
        return {
            "error": f"Action '{action_id}' is already {record['status']}, not pending",
            "found": True,
            "status": record["status"],
        }

    action_type = record["action_type"]
    payload = record["payload"]
    if action_type == "update_jira_ticket":
        result = jira_client.update_issue(payload["ticket_id"], payload["fields"])
    elif action_type == "post_slack_message":
        result = slack_client.post_message(payload["channel"], payload["message"])
    elif action_type == "send_email":
        result = gmail_client.send_message(payload["to"], payload["subject"], payload["body"], payload["sender"])
    else:
        return {"error": f"Unknown action_type '{action_type}'", "found": True}

    record["status"] = "confirmed"
    record["confirmed_at"] = _now_iso()
    sent_log.append({"type": action_type, **payload})
    return {"status": "confirmed", "pending_action_id": action_id, "action_type": action_type, "result": result}
