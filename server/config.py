"""Central place for env-var driven settings for every live integration.

Loads a local `.env` (gitignored) if present via python-dotenv, so `uv run`
picks up credentials the same way Docker does via `env_file: .env`. Nothing
here talks to a network — it just reads os.environ and exposes typed
settings, so client modules can import `settings` instead of scattering
`os.environ.get(...)` calls across the codebase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _list_env(name: str) -> list[str] | None:
    value = os.environ.get(name)
    if not value or not value.strip():
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class JiraSettings:
    base_url: str | None = field(default_factory=lambda: os.environ.get("JIRA_BASE_URL"))
    email: str | None = field(default_factory=lambda: os.environ.get("JIRA_EMAIL"))
    api_token: str | None = field(default_factory=lambda: os.environ.get("JIRA_API_TOKEN"))
    sprint_field: str | None = field(default_factory=lambda: os.environ.get("JIRA_SPRINT_FIELD"))
    story_points_field: str | None = field(
        default_factory=lambda: os.environ.get("JIRA_STORY_POINTS_FIELD")
    )
    # Jira Cloud's search endpoint rejects a JQL query with no restricting
    # clause at all ("Unbounded JQL queries are not allowed") — this backs a
    # default `updated >=` bound applied only when the caller gave no
    # filters whatsoever.
    default_lookback_days: int = field(
        default_factory=lambda: int(os.environ.get("JIRA_DEFAULT_LOOKBACK_DAYS", "90"))
    )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.email and self.api_token)


@dataclass(frozen=True)
class GitHubSettings:
    token: str | None = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN"))
    org: str | None = field(default_factory=lambda: os.environ.get("GITHUB_ORG"))
    repos: list[str] | None = field(default_factory=lambda: _list_env("GITHUB_REPOS"))

    @property
    def configured(self) -> bool:
        return bool(self.token and self.org)


@dataclass(frozen=True)
class SlackSettings:
    bot_token: str | None = field(default_factory=lambda: os.environ.get("SLACK_BOT_TOKEN"))
    user_token: str | None = field(default_factory=lambda: os.environ.get("SLACK_USER_TOKEN"))
    # How far back to look when a tool call doesn't give a `since` — without
    # this, an unfiltered call means "every message in every conversation the
    # bot can see," fetched one full-history page at a time.
    default_lookback_days: int = field(
        default_factory=lambda: int(os.environ.get("SLACK_DEFAULT_LOOKBACK_DAYS", "14"))
    )

    @property
    def configured(self) -> bool:
        return bool(self.bot_token)


@dataclass(frozen=True)
class GoogleSettings:
    client_secrets_file: str = field(
        default_factory=lambda: os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")
    )
    token_file: str = field(
        default_factory=lambda: os.environ.get("GOOGLE_TOKEN_FILE", "token.json")
    )
    gmail_max_threads: int = field(
        default_factory=lambda: int(os.environ.get("GMAIL_MAX_THREADS", "50"))
    )

    @property
    def configured(self) -> bool:
        return os.path.exists(self.token_file)


@dataclass(frozen=True)
class UserProfileSettings:
    """Backs get_user_profile(). No single API returns team/role/working
    hours the way the old mock users.json did, so this is local config
    rather than a fetched value."""

    name: str | None = field(default_factory=lambda: os.environ.get("USER_NAME"))
    email: str | None = field(default_factory=lambda: os.environ.get("USER_EMAIL"))
    team: str | None = field(default_factory=lambda: os.environ.get("USER_TEAM"))
    role: str | None = field(default_factory=lambda: os.environ.get("USER_ROLE"))
    timezone: str | None = field(default_factory=lambda: os.environ.get("USER_TIMEZONE"))
    working_hours: str | None = field(default_factory=lambda: os.environ.get("USER_WORKING_HOURS"))


@dataclass(frozen=True)
class Settings:
    jira: JiraSettings = field(default_factory=JiraSettings)
    github: GitHubSettings = field(default_factory=GitHubSettings)
    slack: SlackSettings = field(default_factory=SlackSettings)
    google: GoogleSettings = field(default_factory=GoogleSettings)
    user_profile: UserProfileSettings = field(default_factory=UserProfileSettings)
    writes_enabled: bool = field(default_factory=lambda: _bool_env("WRITES_ENABLED", False))


settings = Settings()
