"""One-time Google OAuth consent flow for Gmail + Calendar.

Prerequisites (see README.md for the full walkthrough):
  1. Create a Google Cloud project, enable the Gmail API and Calendar API.
  2. Configure the OAuth consent screen (External, Testing, add yourself as
     a test user).
  3. Create an OAuth Client ID (Desktop app type) and download it as
     client_secret.json in the repo root (or point GOOGLE_CLIENT_SECRETS_FILE
     at wherever you saved it).

Run:
    uv run python scripts/google_auth_setup.py

This opens a browser for you to grant access, then writes a refreshable
token to token.json (or GOOGLE_TOKEN_FILE) — server/clients/google_auth.py
reads that file on every Gmail/Calendar call, refreshing it automatically.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow

from server.clients.google_auth import SCOPES
from server.config import settings


def main() -> None:
    secrets_file = settings.google.client_secrets_file
    if not Path(secrets_file).exists():
        print(
            f"Missing {secrets_file} — download an OAuth Client ID (Desktop app) "
            "from Google Cloud Console first. See README.md.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    flow = InstalledAppFlow.from_client_secrets_file(secrets_file, SCOPES)
    creds = flow.run_local_server(port=0)

    token_file = settings.google.token_file
    with open(token_file, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print(f"Saved credentials to {token_file}. Gmail/Calendar tools are ready to use.")


if __name__ == "__main__":
    main()
