"""Shared OAuth credential loader for Gmail + Calendar. The one-time consent
flow that produces the token file this reads lives in
scripts/google_auth_setup.py — this module only ever loads + refreshes.
"""

from __future__ import annotations

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from ..config import settings
from .errors import NotConfiguredError

# Read-only for now — WRITES_ENABLED work would need gmail.send / calendar
# (write) added here and the token re-consented via google_auth_setup.py.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def get_credentials() -> Credentials:
    if not settings.google.configured:
        raise NotConfiguredError(
            f"Google OAuth is not set up — run scripts/google_auth_setup.py "
            f"to create {settings.google.token_file}"
        )
    creds = Credentials.from_authorized_user_file(settings.google.token_file, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(settings.google.token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds
