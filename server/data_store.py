"""In-memory store for the mock dataset, loaded from data/*.json.

Now that the server can run as a long-lived network service shared by several
agent SDKs (rather than a fresh `uv run` per task), `maybe_reload()` re-reads any
data file whose mtime has changed since it was last loaded — so editing the JSON
on disk shows up without restarting the process.

Confirmed writes (via confirm_action) are persisted straight back to the
relevant data/*.json file, so the change is visible to the viewer and to any
other process reading these files, not just this server's memory. This means
confirmed writes permanently modify the dataset — commit data/ to git if you
want an easy way back to a clean baseline for repeated test runs.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import time_utils

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load(name: str) -> Any:
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fictional_now_iso(today_str: str) -> str:
    """'Now', but with the date pinned to the dataset's frozen `today` instead of
    the real date — so new records (a posted Slack message, a sent email) land
    on the right side of 'since=today'/'since=yesterday' filters instead of
    landing in the real-world future relative to the dataset's own timeline."""
    today = time_utils.get_today(today_str)
    wall_clock = datetime.now(time_utils.DEFAULT_TZ)
    return datetime(
        today.year, today.month, today.day,
        wall_clock.hour, wall_clock.minute, wall_clock.second,
        tzinfo=time_utils.DEFAULT_TZ,
    ).isoformat()


class DataStore:
    # filename -> attribute name, for the plain list/dict files (slack_data.json
    # is special-cased in _load_all/maybe_reload since it feeds two attributes).
    _SIMPLE_FILES = {
        "meta.json": "meta",
        "users.json": "users",
        "jira_tickets.json": "jira_tickets",
        "github_prs.json": "github_prs",
        "github_commits.json": "github_commits",
        "gmail_threads.json": "gmail_threads",
        "calendar_events.json": "calendar_events",
    }

    def __init__(self) -> None:
        self._mtimes: dict[str, float] = {}
        self._load_all()
        self.sent_log: list[dict] = []  # audit trail of executed write actions
        self.pending_actions: dict[str, dict] = {}  # draft writes awaiting confirm_action

    @staticmethod
    def _mtime(name: str) -> float:
        return (DATA_DIR / name).stat().st_mtime

    def _load_all(self) -> None:
        for fname, attr in self._SIMPLE_FILES.items():
            setattr(self, attr, _load(fname))
            self._mtimes[fname] = self._mtime(fname)
        slack = _load("slack_data.json")
        self.slack_channels: list[dict] = slack["channels"]
        self.slack_conversations: list[dict] = slack["conversations"]
        self._mtimes["slack_data.json"] = self._mtime("slack_data.json")

    def maybe_reload(self) -> None:
        """Re-read any data file whose mtime changed since it was last loaded."""
        for fname, old_mtime in list(self._mtimes.items()):
            try:
                current_mtime = self._mtime(fname)
            except FileNotFoundError:
                continue
            if current_mtime == old_mtime:
                continue
            if fname == "slack_data.json":
                slack = _load(fname)
                self.slack_channels = slack["channels"]
                self.slack_conversations = slack["conversations"]
            else:
                setattr(self, self._SIMPLE_FILES[fname], _load(fname))
            self._mtimes[fname] = current_mtime

    def _save(self, fname: str, data: Any) -> None:
        """Write a data file back to disk and update our own tracked mtime, so
        the next maybe_reload() doesn't immediately re-read the very thing we
        just wrote (harmless if it did — same data — just a wasted read)."""
        with open(DATA_DIR / fname, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        self._mtimes[fname] = self._mtime(fname)

    def _save_jira(self) -> None:
        self._save("jira_tickets.json", self.jira_tickets)

    def _save_slack(self) -> None:
        self._save("slack_data.json", {"channels": self.slack_channels, "conversations": self.slack_conversations})

    def _save_gmail(self) -> None:
        self._save("gmail_threads.json", self.gmail_threads)

    @property
    def today_str(self) -> str:
        return self.meta["today"]

    def current_user(self) -> dict:
        uid = self.meta["current_user_id"]
        for u in self.users:
            if u["id"] == uid:
                return u
        raise LookupError("current_user_id not found in users.json")

    def user_by_username(self, username: str) -> dict | None:
        for u in self.users:
            if u["username"] == username:
                return u
        return None

    # ---- Jira ----

    def find_ticket(self, ticket_id: str) -> dict | None:
        for t in self.jira_tickets:
            if t["id"].lower() == ticket_id.lower():
                return t
        return None

    def query_tickets(
        self,
        assignee: str | None = None,
        status: list[str] | None = None,
        sprint: str | None = None,
        project: str | None = None,
        labels: list[str] | None = None,
        updated_since: str | None = None,
    ) -> list[dict]:
        results = self.jira_tickets
        if assignee:
            results = [t for t in results if t["assignee"] == assignee]
        if status:
            wanted = {s.lower() for s in status}
            results = [t for t in results if t["status"].lower() in wanted]
        if sprint:
            results = [t for t in results if t.get("sprint") == sprint]
        if project:
            results = [t for t in results if t["project"] == project]
        if labels:
            wanted_labels = {l.lower() for l in labels}
            results = [
                t for t in results
                if wanted_labels & {l.lower() for l in t.get("labels", [])}
            ]
        if updated_since:
            bound = time_utils.resolve_since(updated_since, self.today_str)
            results = [t for t in results if time_utils.parse_dt(t["updated"]) >= bound]
        return results

    # ---- GitHub ----

    def find_pr(self, pr_id: str) -> dict | None:
        for p in self.github_prs:
            if p["id"].lower() == pr_id.lower():
                return p
        for p in self.github_prs:
            if str(p["number"]) == str(pr_id):
                return p
        return None

    def query_prs(
        self,
        author: str | None = None,
        reviewer: str | None = None,
        status: str | None = None,
        repo: str | None = None,
        updated_since: str | None = None,
    ) -> list[dict]:
        results = self.github_prs
        if author:
            results = [p for p in results if p["author"] == author]
        if reviewer:
            results = [p for p in results if reviewer in p.get("reviewers", [])]
        if status:
            results = [p for p in results if p["status"].lower() == status.lower()]
        if repo:
            results = [p for p in results if p["repo"] == repo]
        if updated_since:
            bound = time_utils.resolve_since(updated_since, self.today_str)
            results = [p for p in results if time_utils.parse_dt(p["updated_at"]) >= bound]
        return results

    def query_commits(
        self,
        repo: str | None = None,
        author: str | None = None,
        since: str | None = None,
    ) -> list[dict]:
        results = self.github_commits
        if repo:
            results = [c for c in results if c["repo"] == repo]
        if author:
            results = [c for c in results if c["author"] == author]
        if since:
            bound = time_utils.resolve_since(since, self.today_str)
            results = [c for c in results if time_utils.parse_dt(c["date"]) >= bound]
        return sorted(results, key=lambda c: c["date"], reverse=True)

    GITHUB_ORG = "nimbus-labs"

    def pr_web_url(self, pr: dict) -> str:
        return f"https://github.com/{self.GITHUB_ORG}/{pr['repo']}/pull/{pr['number']}"

    def find_pr_by_url(self, pr_url: str) -> dict | None:
        needle = pr_url.strip().rstrip("/").lower()
        for p in self.github_prs:
            if needle == self.pr_web_url(p).lower() or needle == p["id"].lower():
                return p
        return None

    def prs_for_ticket(self, ticket_id: str) -> list[dict]:
        ticket_id = ticket_id.upper()
        matched_numbers = {
            (c["repo"], c["pr_number"])
            for c in self.github_commits
            if c["pr_number"] is not None and c["message"].upper().startswith(ticket_id + ":")
        }
        by_ref = [
            p for p in self.github_prs
            if (p.get("linked_jira_ticket") or "").upper() == ticket_id
        ]
        by_commit = [
            p for p in self.github_prs
            if (p["repo"], p["number"]) in matched_numbers
        ]
        seen, out = set(), []
        for p in by_ref + by_commit:
            if p["id"] not in seen:
                seen.add(p["id"])
                out.append(p)
        return out

    # ---- Slack ----

    def _all_messages(self) -> list[dict]:
        out = []
        for conv in self.slack_conversations:
            for m in conv["messages"]:
                out.append({**m, "channel": conv.get("channel"), "conversation_id": conv["id"],
                            "type": conv["type"], "participants": conv.get("participants")})
        return out

    def query_slack_messages(
        self,
        channel: str | None = None,
        user: str | None = None,
        mentions: str | None = None,
        since: str | None = None,
    ) -> list[dict]:
        results = self._all_messages()
        if channel:
            results = [m for m in results if m["channel"] == channel]
        if user:
            results = [
                m for m in results
                if m["user"] == user or (m["participants"] and user in m["participants"])
            ]
        if mentions:
            results = [m for m in results if mentions in m.get("mentions", [])]
        if since:
            bound = time_utils.resolve_since(since, self.today_str)
            results = [m for m in results if time_utils.parse_dt(m["ts"]) >= bound]
        return sorted(results, key=lambda m: m["ts"])

    def search_slack(
        self,
        query: str,
        channel: str | None = None,
        user: str | None = None,
        since: str | None = None,
    ) -> list[dict]:
        q = query.lower()
        results = self.query_slack_messages(channel=channel, user=user, since=since)
        return [m for m in results if q in m["text"].lower()]

    def _apply_post_slack_message(self, channel: str, message: str, user: str) -> dict:
        ts = _fictional_now_iso(self.today_str)
        entry = {
            "channel": channel,
            "user": user,
            "text": message,
            "ts": ts,
        }
        self.sent_log.append({"type": "slack_message", **entry})
        conv = {
            "id": f"conv_runtime_{len(self.slack_conversations) + 1}",
            "type": "channel",
            "channel": channel,
            "messages": [{"id": f"msg_runtime_{len(self.sent_log)}", "user": user,
                          "text": message, "ts": ts, "mentions": []}],
        }
        self.slack_conversations.append(conv)
        return entry

    # ---- Calendar ----

    def query_calendar_events(
        self,
        start_date: str,
        end_date: str,
        event_type: str | None = None,
        participant: str | None = None,
    ) -> list[dict]:
        start = time_utils.resolve_date(start_date, self.today_str, bound="start")
        end = time_utils.resolve_date(end_date, self.today_str, bound="end")
        results = []
        for e in self.calendar_events:
            e_start = time_utils.parse_dt(e["start"])
            if not time_utils.in_day_range(e_start, start, end):
                continue
            if event_type and e["type"] != event_type:
                continue
            if participant and participant not in e.get("participants", []):
                continue
            results.append(e)
        return sorted(results, key=lambda e: e["start"])

    # ---- Gmail ----

    def query_gmail_threads(
        self,
        sender: str | None = None,
        subject_contains: str | None = None,
        label: str | None = None,
        unread: bool | None = None,
        since: str | None = None,
    ) -> list[dict]:
        results = self.gmail_threads
        if sender:
            results = [
                t for t in results
                if any(m["from"] == sender for m in t["messages"])
            ]
        if subject_contains:
            needle = subject_contains.lower()
            results = [t for t in results if needle in t["subject"].lower()]
        if label:
            results = [t for t in results if label in t.get("labels", [])]
        if unread is not None:
            results = [t for t in results if t.get("unread") == unread]
        if since:
            bound = time_utils.resolve_since(since, self.today_str)
            results = [t for t in results if time_utils.parse_dt(t["date"]) >= bound]
        return sorted(results, key=lambda t: t["date"], reverse=True)

    def find_gmail_thread(self, thread_id: str) -> dict | None:
        for t in self.gmail_threads:
            if t["id"] == thread_id:
                return t
        return None

    def _apply_send_email(self, to: list[str], subject: str, body: str, sender: str) -> dict:
        entry = {"type": "email", "from": sender, "to": to, "subject": subject, "body": body}
        self.sent_log.append(entry)
        ts = _fictional_now_iso(self.today_str)
        thread = {
            "id": f"thread_runtime_{len(self.gmail_threads) + 1}",
            "subject": subject,
            "participants": [sender, *to],
            "labels": ["work"],
            "unread": False,
            "flagged": False,
            "date": ts,
            "messages": [{"from": sender, "to": to, "cc": [], "date": ts, "body": body}],
        }
        self.gmail_threads.append(thread)
        return entry

    # ---- Jira write ----

    def _apply_update_jira_ticket(self, ticket_id: str, fields: dict) -> dict:
        ticket = self.find_ticket(ticket_id)
        if ticket is None:
            raise LookupError(f"Unknown ticket: {ticket_id}")
        before = {k: ticket.get(k) for k in fields}
        ticket.update(fields)
        self.sent_log.append({"type": "jira_update", "ticket_id": ticket_id, "before": before, "after": fields})
        return ticket

    # ---- Gated-write approval workflow (pending_action_id -> confirm_action) ----

    def draft_action(self, action_type: str, payload: dict) -> dict:
        """Record a write action awaiting human approval. Nothing is applied yet —
        the caller must invoke confirm_action() with the returned id to execute it."""
        action_id = uuid.uuid4().hex[:12]
        record = {
            "id": action_id,
            "action_type": action_type,
            "payload": payload,
            "status": "pending",
            "created_at": _now_iso(),
        }
        self.pending_actions[action_id] = record
        return record

    def confirm_action(self, action_id: str) -> dict:
        """Execute a previously-drafted write action. Only takes effect here —
        draft_action() never mutates anything on its own."""
        record = self.pending_actions.get(action_id)
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
            result = self._apply_update_jira_ticket(payload["ticket_id"], payload["fields"])
            self._save_jira()
        elif action_type == "post_slack_message":
            result = self._apply_post_slack_message(payload["channel"], payload["message"], payload["user"])
            self._save_slack()
        elif action_type == "send_email":
            result = self._apply_send_email(payload["to"], payload["subject"], payload["body"], payload["sender"])
            self._save_gmail()
        else:
            return {"error": f"Unknown action_type '{action_type}'", "found": True}
        record["status"] = "confirmed"
        record["confirmed_at"] = _now_iso()
        return {"status": "confirmed", "pending_action_id": action_id, "action_type": action_type, "result": result}


_store: DataStore | None = None


def get_store() -> DataStore:
    global _store
    if _store is None:
        _store = DataStore()
    else:
        _store.maybe_reload()
    return _store
