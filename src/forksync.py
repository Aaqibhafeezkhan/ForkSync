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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

API_URL = "https://api.github.com"
MAX_RETRIES = 3
MAX_RETRY_AFTER = 60
MAX_RATE_LIMIT_WAIT = 300
RATE_LIMIT_SAFETY_MARGIN = 1


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str, api_url: str = API_URL) -> None:
        self.token, self.api_url = token, api_url.rstrip("/")
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset: float | None = None

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
        """Return a bounded Retry-After delay or exponential backoff."""
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), MAX_RETRY_AFTER)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                    return min(max(delay, 0.0), MAX_RETRY_AFTER)
                except (TypeError, ValueError, OverflowError):
                    pass
        return float(2 ** attempt)

    def _update_rate_limit(self, headers: Any) -> None:
        """Remember primary rate-limit state reported by GitHub."""
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        parsed_remaining = None
        parsed_reset = None
        try:
            if remaining is not None:
                parsed_remaining = int(remaining)
            if reset is not None:
                parsed_reset = float(reset)
        except (TypeError, ValueError):
            # Ignore malformed headers rather than corrupting known state.
            return
        if parsed_remaining is not None:
            self.rate_limit_remaining = parsed_remaining
        if parsed_reset is not None:
            self.rate_limit_reset = parsed_reset

    def _wait_for_rate_limit(self) -> None:
        """Wait for a known exhausted primary quota, within a bounded limit."""
        if self.rate_limit_remaining is None or self.rate_limit_remaining > 0:
            return
        if self.rate_limit_reset is None:
            raise GitHubError(
                "GitHub API rate limit is exhausted but X-RateLimit-Reset is unavailable"
            )
        delay = max(
            0.0,
            self.rate_limit_reset - time.time() + RATE_LIMIT_SAFETY_MARGIN,
        )
        max_wait = float(os.getenv("FORKSYNC_MAX_RATE_LIMIT_WAIT", MAX_RATE_LIMIT_WAIT))
        if delay > max_wait:
            raise GitHubError(
                f"GitHub API rate limit is exhausted; reset requires waiting {delay:.1f}s, "
                f"exceeding the configured maximum of {max_wait:.1f}s"
            )
        if delay:
            time.sleep(delay)
        # Force a fresh response to refresh quota state after the reset.
        self.rate_limit_remaining = None

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
        for attempt in range(MAX_RETRIES):
            try:
                self._wait_for_rate_limit()
                with urllib.request.urlopen(request, timeout=30) as response:
                    self._update_rate_limit(response.headers)
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                self._update_rate_limit(exc.headers)
                detail = exc.read().decode("utf-8", errors="replace")
                retry_after = exc.headers.get("Retry-After")
                primary_rate_limited = (
                    exc.code in (403, 429)
                    and exc.headers.get("X-RateLimit-Remaining") == "0"
                )
                secondary_rate_limited = exc.code in (403, 429) and bool(retry_after)
                if attempt < MAX_RETRIES - 1 and primary_rate_limited:
                    self._wait_for_rate_limit()
                    continue
                if attempt < MAX_RETRIES - 1 and (
                    500 <= exc.code <= 599 or secondary_rate_limited
                ):
                    time.sleep(self._retry_delay(attempt, retry_after))
                    continue
                raise GitHubError(f"{method} {url} failed ({exc.code}): {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt < MAX_RETRIES - 1:
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

    def branch_details(self, full_name: str, branch: str) -> dict[str, Any]:
        encoded_branch = urllib.parse.quote(branch, safe="")
        return self.request("GET", f"/repos/{full_name}/branches/{encoded_branch}")

    def sync_fork(self, full_name: str, branch: str) -> dict[str, Any]:
        return self.request("POST", f"/repos/{full_name}/merge-upstream", {"branch": branch})


@dataclass
class SyncPlanEntry:
    repository: str
    status: str
    branch: str | None
    upstream: str | None
    reason: str


@dataclass
class Outcome:
    repository: str
    status: str
    message: str


def build_sync_plan(client: GitHubClient) -> list[SyncPlanEntry]:
    """Discover forks and validate every candidate before any sync writes."""
    repos = client.owned_repositories()
    excluded = os.getenv("FORKSYNC_REPOSITORY", "").lower()
    plan: list[SyncPlanEntry] = []

    forks = [repo for repo in repos if repo.get("fork")]
    forks.sort(key=lambda repo: repo.get("full_name", "").lower())

    for repo in forks:
        name = repo.get("full_name") or "<unknown repository>"
        if name.lower() == excluded:
            plan.append(SyncPlanEntry(name, "SKIPPED", None, None, "This is the ForkSync utility repository."))
            continue
        if repo.get("archived", False):
            plan.append(SyncPlanEntry(name, "SKIPPED", None, None, "The fork is archived."))
            continue

        try:
            details = client.repository_details(name)
        except GitHubError as exc:
            plan.append(SyncPlanEntry(name, "BLOCKED", None, None, f"Could not inspect fork metadata: {exc}"))
            continue

        if details.get("archived", False):
            plan.append(SyncPlanEntry(name, "SKIPPED", None, None, "The fork is archived."))
            continue
        if details.get("fork") is False:
            plan.append(SyncPlanEntry(name, "BLOCKED", None, None, "Repository is no longer marked as a fork."))
            continue

        parent = details.get("parent")
        upstream = parent.get("full_name") if isinstance(parent, dict) else None
        if not upstream:
            plan.append(SyncPlanEntry(name, "BLOCKED", None, None, "Missing upstream parent metadata."))
            continue

        branch = details.get("default_branch") or repo.get("default_branch")
        if not branch:
            plan.append(SyncPlanEntry(name, "BLOCKED", None, upstream, "Missing fork default-branch metadata."))
            continue

        try:
            client.branch_details(upstream, branch)
        except GitHubError as exc:
            if "(404)" in str(exc):
                reason = f"Upstream branch '{branch}' was not found or is not accessible on {upstream}."
            else:
                reason = f"Could not validate upstream branch '{branch}' on {upstream}: {exc}"
            plan.append(SyncPlanEntry(name, "BLOCKED", branch, upstream, reason))
            continue

        plan.append(SyncPlanEntry(
            name,
            "READY",
            branch,
            upstream,
            f"Upstream branch '{branch}' exists and is ready to sync.",
        ))

    return plan


def sync_all(client: GitHubClient, dry_run: bool = False) -> list[Outcome]:
    plan = build_sync_plan(client)
    outcomes: list[Outcome] = []
    ready = sum(entry.status == "READY" for entry in plan)
    print(
        f"Sync plan: {len(plan)} forks; {ready} ready; "
        f"{sum(entry.status == 'SKIPPED' for entry in plan)} skipped; "
        f"{sum(entry.status == 'BLOCKED' for entry in plan)} blocked."
    )

    for entry in plan:
        if entry.status != "READY":
            outcomes.append(Outcome(entry.repository, entry.status, entry.reason))
            continue
        if dry_run:
            outcomes.append(Outcome(
                entry.repository,
                "DRY RUN",
                f"Would sync {entry.branch} from {entry.upstream}",
            ))
            continue
        try:
            result = client.sync_fork(entry.repository, entry.branch)
            merge_type = result.get("merge_type", "synced")
            outcomes.append(Outcome(
                entry.repository,
                "OK",
                f"{entry.branch}: {merge_type}; upstream {entry.upstream}",
            ))
        except GitHubError as exc:
            outcomes.append(Outcome(entry.repository, "FAILED", str(exc)))

    for item in outcomes:
        print(f"[{item.status}] {item.repository}: {item.message}")
    failed = sum(item.status in {"FAILED", "BLOCKED"} for item in outcomes)
    successful = sum(item.status in {"OK", "DRY RUN"} for item in outcomes)
    skipped = sum(item.status == "SKIPPED" for item in outcomes)
    print(f"Summary: {successful} successful/preview, {skipped} skipped, {failed} blocked/failed.")
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
    return 1 if any(item.status in {"FAILED", "BLOCKED"} for item in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
