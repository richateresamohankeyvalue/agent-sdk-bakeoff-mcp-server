"""GitHub REST API v3 client — replaces the github_prs.json/github_commits.json
mock. Uses the Search API for listing PRs so author/reviewer/status/updated_since
filters map onto native search qualifiers server-side instead of client-side
filtering across every repo.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from .. import time_utils
from ..config import settings
from .errors import NotConfiguredError

API_BASE = "https://api.github.com"


def _require_configured() -> None:
    if not settings.github.configured:
        raise NotConfiguredError(
            "GitHub is not configured (need GITHUB_TOKEN, GITHUB_ORG)"
        )


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={
            "Authorization": f"Bearer {settings.github.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=20.0,
    )


def _paginate(client: httpx.Client, url: str, params: dict, items_key: str | None = None) -> list[dict]:
    out: list[dict] = []
    next_url: str | None = url
    next_params: dict | None = {**params, "per_page": 100}
    while next_url:
        resp = client.get(next_url, params=next_params)
        resp.raise_for_status()
        data = resp.json()
        out.extend(data[items_key] if items_key else data)
        next_url = resp.links.get("next", {}).get("url")
        next_params = None  # next_url already carries all query params
    return out


_account_type_cache: str | None = None


def _account_type(client: httpx.Client) -> str:
    """"Organization" or "User" — GITHUB_ORG isn't necessarily a GitHub
    Organization; repos owned by a personal account need different
    endpoints/search qualifiers (/orgs/... and org: are Organization-only)."""
    global _account_type_cache
    if _account_type_cache is None:
        resp = client.get(f"/users/{settings.github.org}")
        resp.raise_for_status()
        _account_type_cache = resp.json().get("type", "Organization")
    return _account_type_cache


def _repos() -> list[str]:
    if settings.github.repos:
        return settings.github.repos
    with _client() as client:
        if _account_type(client) == "Organization":
            items = _paginate(client, f"/orgs/{settings.github.org}/repos", {"type": "all"})
        else:
            items = _paginate(client, "/user/repos", {"visibility": "all", "affiliation": "owner"})
    return [r["name"] for r in items]


def build_search_query(
    author: str | None = None,
    reviewer: str | None = None,
    status: str | None = None,
    repo: str | None = None,
    updated_since: str | None = None,
    owner_qualifier: str | None = None,
) -> str:
    """Pure function: filter args -> GitHub search-issues query string.
    `owner_qualifier` overrides the default `org:{GITHUB_ORG}` scope (e.g.
    with `user:{GITHUB_ORG}` for a personal-account owner) — query_prs()
    resolves the real one via a network call; this stays network-free for
    unit testing."""
    org = settings.github.org
    qualifiers = ["type:pr"]
    qualifiers.append(f"repo:{org}/{repo}" if repo else (owner_qualifier or f"org:{org}"))
    if author:
        qualifiers.append(f"author:{author}")
    if reviewer:
        qualifiers.append(f"review-requested:{reviewer}")
    if status:
        s = status.lower()
        if s == "open":
            qualifiers.append("state:open")
        elif s == "merged":
            qualifiers.append("is:merged")
        elif s == "closed":
            qualifiers.append("state:closed")
    if updated_since:
        bound = time_utils.resolve_since(updated_since)
        qualifiers.append(f"updated:>={bound.strftime('%Y-%m-%d')}")
    return " ".join(qualifiers)


def _repo_from_url(html_url: str) -> str:
    # https://github.com/{org}/{repo}/pull/{number}
    return html_url.split("/pull/")[0].rsplit("/", 1)[-1]


def _normalize_search_item(item: dict) -> dict:
    """Accepts either a /search/issues item (PR fields nested under
    `pull_request`) or a direct pull-request object (fields at the top
    level, as returned by the commit->PR association endpoint)."""
    repo = _repo_from_url(item["html_url"])
    merged_at = item.get("merged_at") or item.get("pull_request", {}).get("merged_at")
    return {
        "id": f"{repo}#{item['number']}",
        "number": item["number"],
        "title": item.get("title"),
        "author": (item.get("user") or {}).get("login"),
        "status": "merged" if merged_at else item.get("state"),
        "repo": repo,
        "updated_at": item.get("updated_at"),
        "created_at": item.get("created_at"),
        "url": item.get("html_url"),
    }


def query_prs(
    author: str | None = None,
    reviewer: str | None = None,
    status: str | None = None,
    repo: str | None = None,
    updated_since: str | None = None,
) -> list[dict]:
    _require_configured()
    with _client() as client:
        owner_qualifier = None
        if not repo:
            owner_qualifier = (
                f"org:{settings.github.org}" if _account_type(client) == "Organization"
                else f"user:{settings.github.org}"
            )
        query = build_search_query(author, reviewer, status, repo, updated_since, owner_qualifier)
        items = _paginate(client, "/search/issues", {"q": query, "sort": "updated"}, items_key="items")
    return [_normalize_search_item(item) for item in items]


def _parse_pr_id(pr_id: str) -> tuple[str | None, int]:
    if "#" in pr_id:
        repo, number = pr_id.split("#", 1)
        return repo, int(number)
    return None, int(pr_id)


def get_pr_detail(pr_id: str) -> dict | None:
    _require_configured()
    org = settings.github.org
    repo, number = _parse_pr_id(pr_id)
    candidate_repos = [repo] if repo else _repos()
    with _client() as client:
        for candidate in candidate_repos:
            resp = client.get(f"/repos/{org}/{candidate}/pulls/{number}")
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            pr = resp.json()

            reviews_resp = client.get(f"/repos/{org}/{candidate}/pulls/{number}/reviews")
            reviews_resp.raise_for_status()
            reviews = [
                {
                    "author": (r.get("user") or {}).get("login"),
                    "state": r.get("state"),
                    "body": r.get("body"),
                    "submitted_at": r.get("submitted_at"),
                }
                for r in reviews_resp.json()
            ]

            status_resp = client.get(f"/repos/{org}/{candidate}/commits/{pr['head']['sha']}/status")
            status_resp.raise_for_status()
            ci = status_resp.json()

            return {
                "id": f"{candidate}#{number}",
                "number": number,
                "title": pr.get("title"),
                "author": (pr.get("user") or {}).get("login"),
                "status": "merged" if pr.get("merged_at") else pr.get("state"),
                "repo": candidate,
                "body": pr.get("body"),
                "updated_at": pr.get("updated_at"),
                "created_at": pr.get("created_at"),
                "additions": pr.get("additions"),
                "deletions": pr.get("deletions"),
                "changed_files": pr.get("changed_files"),
                "review_comments": reviews,
                "ci_status": {"state": ci.get("state"), "checks": ci.get("statuses", [])},
                "url": pr.get("html_url"),
            }
    return None


def query_commits(
    repo: str | None = None,
    author: str | None = None,
    since: str | None = None,
) -> list[dict]:
    _require_configured()
    org = settings.github.org
    repos = [repo] if repo else _repos()
    params: dict[str, Any] = {}
    if author:
        params["author"] = author
    if since:
        bound = time_utils.resolve_since(since)
        params["since"] = bound.isoformat()

    commits: list[dict] = []
    with _client() as client:
        for r in repos:
            for c in _paginate(client, f"/repos/{org}/{r}/commits", params):
                commits.append(
                    {
                        "sha": c["sha"],
                        "repo": r,
                        "author": (c.get("author") or {}).get("login") or c["commit"]["author"]["name"],
                        "message": c["commit"]["message"],
                        "date": c["commit"]["author"]["date"],
                        "url": c.get("html_url"),
                    }
                )
    return sorted(commits, key=lambda c: c["date"], reverse=True)


def prs_for_ticket(ticket_id: str) -> list[dict]:
    """Scans commit messages for the "TICKET-ID: ..." convention (same
    convention the mock dataset used) and resolves each matching commit to
    its associated PR(s) via GitHub's commit->PR association endpoint."""
    _require_configured()
    org = settings.github.org
    prefix = re.compile(rf"^{re.escape(ticket_id)}\s*:", re.IGNORECASE)
    seen: set[str] = set()
    out: list[dict] = []
    with _client() as client:
        for repo in _repos():
            for c in _paginate(client, f"/repos/{org}/{repo}/commits", {}):
                if not prefix.match(c["commit"]["message"]):
                    continue
                pulls_resp = client.get(f"/repos/{org}/{repo}/commits/{c['sha']}/pulls")
                if pulls_resp.status_code != 200:
                    continue
                for pr in pulls_resp.json():
                    pr_id = f"{repo}#{pr['number']}"
                    if pr_id in seen:
                        continue
                    seen.add(pr_id)
                    out.append(_normalize_search_item(pr))
    return out


def pr_web_url(pr: dict) -> str:
    return pr.get("url") or f"https://github.com/{settings.github.org}/{pr['repo']}/pull/{pr['number']}"


def find_pr_by_url(pr_url: str) -> dict | None:
    match = re.search(r"/([^/]+)/pull/(\d+)/?$", pr_url.strip())
    if not match:
        return None
    return get_pr_detail(f"{match.group(1)}#{match.group(2)}")
