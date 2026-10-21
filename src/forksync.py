"""Discover owned GitHub forks and sync their default branches with upstream."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

API_URL = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str, api_url: str = API_URL) -> None:
        self.token, self.api_url = token, api_url.rstrip("/")

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        body = None if payload is None else json.dumps(payload).encode()
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ForkSync",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code in (403, 429) and attempt < 2 and (
                    exc.headers.get("Retry-After")
                    or exc.headers.get("X-RateLimit-Remaining") == "0"
                ):
                    time.sleep(min(int(exc.headers.get("Retry-After", "5")), 60))
                    continue
                raise GitHubError(f"{method} {url} failed ({exc.code}): {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubError(f"{method} {url} failed: {exc.reason}") from exc
        raise GitHubError(f"{method} {url} failed after retries")

    def owned_repositories(self) -> list[dict[str, Any]]:
        repos, page = [], 1
        while True:
            query = urllib.parse.urlencode(
                {"affiliation": "owner", "per_page": 100, "page": page, "sort": "full_name"}
            )
            batch = self.request("GET", f"/user/repos?{query}")
            if not batch:
                break
            repos.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return repos

    def repository_details(self, full_name: str) -> dict[str, Any]:
        return self.request("GET", f"/repos/{full_name}")

    def sync_fork(self, full_name: str, branch: str) -> dict[str, Any]:
        return self.request("POST", f"/repos/{full_name}/merge-upstream", {"branch": branch})


@dataclass
class Outcome:
    repository: str
    status: str
    message: str


def sync_all(client: GitHubClient, dry_run: bool = False) -> list[Outcome]:
    repos = client.owned_repositories()
    excluded = os.getenv("FORKSYNC_REPOSITORY", "").lower()
    forks = [
        repo for repo in repos
        if repo.get("fork")
        and repo.get("full_name", "").lower() != excluded
        and not repo.get("archived", False)
    ]
    forks.sort(key=lambda repo: repo.get("full_name", "").lower())
    outcomes: list[Outcome] = []
    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []

    # The list-repositories response may omit parent/source metadata. Fetch each
    # fork's detail resource before deciding whether it has a usable upstream.
    for repo in forks:
        name = repo["full_name"]
        try:
            details = client.repository_details(name)
        except GitHubError as exc:
            outcomes.append(Outcome(name, "FAILED", f"Could not inspect upstream metadata: {exc}"))
            continue
        if details.get("archived", False):
            continue
        branch = details.get("default_branch") or repo.get("default_branch")
        parent = details.get("parent")
        if branch and parent and parent.get("full_name"):
            eligible.append((details, parent))

    print(f"Discovered {len(repos)} owned repositories; {len(forks)} non-archived forks; {len(eligible)} eligible forks.")
    for repo, parent in eligible:
        name = repo["full_name"]
        branch = repo.get("default_branch")
        upstream = parent["full_name"]
        if dry_run:
            outcomes.append(Outcome(name, "DRY RUN", f"Would sync {branch} from {upstream}"))
            continue
        try:
            result = client.sync_fork(name, branch)
            merge_type = result.get("merge_type", "synced")
            outcomes.append(Outcome(name, "OK", f"{branch}: {merge_type}; upstream {upstream}"))
        except GitHubError as exc:
            outcomes.append(Outcome(name, "FAILED", str(exc)))

    for item in outcomes:
        print(f"[{item.status}] {item.repository}: {item.message}")
    failed = sum(item.status == "FAILED" for item in outcomes)
    successful = sum(item.status in {"OK", "DRY RUN"} for item in outcomes)
    print(f"Summary: {successful} successful/preview, {failed} failed.")
    return outcomes


def main() -> int:
    token = os.getenv("FORKSYNC_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token:
        print("Missing token. Set FORKSYNC_TOKEN (recommended) or GITHUB_TOKEN.", file=sys.stderr)
        return 2
    dry_run = os.getenv("FORKSYNC_DRY_RUN", "false").lower() in {"1", "true", "yes"}
    try:
        outcomes = sync_all(GitHubClient(token), dry_run=dry_run)
    except GitHubError as exc:
        print(f"Fatal GitHub API error: {exc}", file=sys.stderr)
        return 1
    return 1 if any(item.status == "FAILED" for item in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
