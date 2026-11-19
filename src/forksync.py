"""Discover and synchronize forks across personal and organization owners."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

API_URL = "https://api.github.com"
MAX_RETRIES = 3
MAX_RETRY_AFTER = 60
MAX_RATE_LIMIT_WAIT = 300
RATE_LIMIT_SAFETY_MARGIN = 1

SUCCESS_STATUSES = {"SYNCED", "UP_TO_DATE", "DRY_RUN", "SKIPPED"}
ATTENTION_STATUSES = {
    "UPSTREAM_UNAVAILABLE", "BRANCH_MISSING", "TOKEN_POLICY_BLOCKED",
    "ACCESS_RESTRICTED", "AUTHENTICATION_FAILED", "CONFLICT",
    "SYNC_REJECTED", "FAILED", "DISCOVERY_FAILED", "CONFIGURATION_ERROR",
}
FATAL_STATUSES = {"AUTHENTICATION_FAILED", "FAILED", "DISCOVERY_FAILED", "CONFIGURATION_ERROR"}

NEXT_ACTIONS = {
    "UPSTREAM_UNAVAILABLE": "Check whether the upstream repository was deleted, made private, renamed, transferred, or hidden from this token. Keep the fork; exclude it only if you no longer want to monitor it.",
    "BRANCH_MISSING": "Open the upstream repository and identify its current default branch. Correct the fork's default branch or upstream branch before retrying.",
    "TOKEN_POLICY_BLOCKED": "Create a fine-grained token whose expiry satisfies the upstream organization's policy (the observed policy requires 366 days or less), update the owner token secret, and rerun.",
    "ACCESS_RESTRICTED": "Review the upstream organization's token policy and repository permissions. Prefer a GitHub App installation where supported, or use a token accepted by that organization.",
    "AUTHENTICATION_FAILED": "Replace the expired, revoked, or invalid token secret and rerun the workflow.",
    "CONFLICT": "Fetch upstream and fork branches locally, resolve the divergence manually, run tests, and push the reviewed resolution to your fork. ForkSync will not force-push or resolve conflicts automatically.",
    "SYNC_REJECTED": "Inspect the GitHub API response and repository rules. Resolve the reported restriction, then rerun the sync.",
    "FAILED": "Inspect the error and Actions logs. Retry transient failures; investigate persistent API or network errors before the next run.",
    "DISCOVERY_FAILED": "Check the owner scope, token permissions, organization approval and API availability. Fix discovery access before relying on this run.",
    "CONFIGURATION_ERROR": "Fix the ForkSync configuration or missing secret identified in the workflow log, then rerun.",
}


class ConfigurationError(RuntimeError):
    """Raised when ForkSync configuration is invalid."""


class GitHubError(RuntimeError):
    """GitHub API error with structured status and response details."""

    def __init__(self, message: str, status_code: int | None = None,
                 response_body: str = "", headers: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.headers = headers or {}

    @property
    def api_message(self) -> str:
        try:
            payload = json.loads(self.response_body)
            return str(payload.get("message") or self.response_body)
        except (json.JSONDecodeError, TypeError, AttributeError):
            return self.response_body or str(self)


class GitHubClient:
    def __init__(self, token: str, api_url: str = API_URL) -> None:
        self.token, self.api_url = token, api_url.rstrip("/")
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset: float | None = None

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
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
        remaining, reset = headers.get("X-RateLimit-Remaining"), headers.get("X-RateLimit-Reset")
        try:
            parsed_remaining = int(remaining) if remaining is not None else None
            parsed_reset = float(reset) if reset is not None else None
        except (TypeError, ValueError):
            return
        if parsed_remaining is not None:
            self.rate_limit_remaining = parsed_remaining
        if parsed_reset is not None:
            self.rate_limit_reset = parsed_reset

    def _wait_for_rate_limit(self) -> None:
        if self.rate_limit_remaining is None or self.rate_limit_remaining > 0:
            return
        if self.rate_limit_reset is None:
            raise GitHubError("GitHub API rate limit is exhausted but X-RateLimit-Reset is unavailable")
        delay = max(0.0, self.rate_limit_reset - time.time() + RATE_LIMIT_SAFETY_MARGIN)
        max_wait = float(os.getenv("FORKSYNC_MAX_RATE_LIMIT_WAIT", MAX_RATE_LIMIT_WAIT))
        if delay > max_wait:
            raise GitHubError(
                f"GitHub API rate limit is exhausted; reset requires waiting {delay:.1f}s, "
                f"exceeding the configured maximum of {max_wait:.1f}s"
            )
        if delay:
            time.sleep(delay)
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
                    if not raw:
                        return None
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise GitHubError(f"{method} {url} returned invalid JSON: {exc}") from exc
            except urllib.error.HTTPError as exc:
                self._update_rate_limit(exc.headers)
                detail = exc.read().decode("utf-8", errors="replace")
                retry_after = exc.headers.get("Retry-After")
                primary_limited = exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0"
                secondary_limited = exc.code in (403, 429) and bool(retry_after)
                if attempt < MAX_RETRIES - 1 and primary_limited:
                    self._wait_for_rate_limit()
                    continue
                if attempt < MAX_RETRIES - 1 and (500 <= exc.code <= 599 or secondary_limited):
                    time.sleep(self._retry_delay(attempt, retry_after))
                    continue
                raise GitHubError(
                    f"{method} {url} failed ({exc.code}): {detail}",
                    status_code=exc.code, response_body=detail, headers=exc.headers,
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubError(f"{method} {url} failed: {exc.reason}") from exc
        raise GitHubError(f"{method} {url} failed after retries")

    def _paginate(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        repositories: list[dict[str, Any]] = []
        page = 1
        while True:
            query = urllib.parse.urlencode({**params, "per_page": 100, "page": page})
            batch = self.request("GET", f"{path}?{query}")
            if not batch:
                break
            repositories.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return repositories

    def owned_repositories(self) -> list[dict[str, Any]]:
        """List repositories owned by the authenticated personal account."""
        return self._paginate("/user/repos", {"affiliation": "owner", "type": "owner"})

    def organization_repositories(self, organization: str) -> list[dict[str, Any]]:
        """List repositories visible to the token in one organization."""
        encoded = urllib.parse.quote(organization, safe="")
        return self._paginate(f"/orgs/{encoded}/repos", {"type": "all"})

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
    owner_scope: str = "user"


@dataclass
class Outcome:
    repository: str
    status: str
    message: str
    branch: str | None = None
    upstream: str | None = None
    owner_scope: str = "user"

    def to_dict(self) -> dict[str, Any]:
        item = asdict(self)
        item["repository_url"] = f"https://github.com/{self.repository}" if "/" in self.repository else None
        item["upstream_url"] = f"https://github.com/{self.upstream}" if self.upstream else None
        item["recommended_action"] = NEXT_ACTIONS.get(self.status, "No manual action required.")
        item["requires_attention"] = self.status in ATTENTION_STATUSES
        return item


def classify_github_error(error: GitHubError) -> str:
    """Map GitHub errors to actionable, non-ambiguous status categories."""
    message = f"{error} {error.response_body}".lower()
    if error.status_code == 401:
        return "AUTHENTICATION_FAILED"
    if error.status_code == 403:
        if "forbids access via a fine-grained personal access token" in message or (
            "fine-grained personal access token" in message and "366 days" in message
        ):
            return "TOKEN_POLICY_BLOCKED"
        return "ACCESS_RESTRICTED"
    if error.status_code == 404:
        return "UPSTREAM_UNAVAILABLE"
    if error.status_code == 409:
        return "CONFLICT"
    if error.status_code == 422:
        return "SYNC_REJECTED"
    return "FAILED"


def _csv_set(value: str | None) -> set[str]:
    return {item.strip().lower() for item in (value or "").split(",") if item.strip()}


def load_config() -> dict[str, Any]:
    path = Path(os.getenv("FORKSYNC_CONFIG", "forksync.json"))
    config: dict[str, Any] = {
        "owners": ["user"],
        "exclude_repositories": [],
        "exclude_upstreams": [],
    }
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Could not read {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigurationError(f"{path} must contain a JSON object.")
        config.update(loaded)
    if os.getenv("FORKSYNC_OWNERS"):
        config["owners"] = [v.strip() for v in os.environ["FORKSYNC_OWNERS"].split(",") if v.strip()]
    for key, env_name in (
        ("exclude_repositories", "FORKSYNC_EXCLUDE_REPOSITORIES"),
        ("exclude_upstreams", "FORKSYNC_EXCLUDE_UPSTREAMS"),
    ):
        if os.getenv(env_name):
            config[key] = [v.strip() for v in os.environ[env_name].split(",") if v.strip()]

    owners = config.get("owners")
    if not isinstance(owners, list) or not owners:
        raise ConfigurationError("At least one owner scope must be configured.")
    normalized: list[str] = []
    for value in owners:
        if not isinstance(value, str):
            raise ConfigurationError("Each owner scope must be a string.")
        scope = value.strip()
        if scope.lower() in {"user", "me"}:
            scope = "user"
        elif scope.lower().startswith("org:") and scope[4:].strip():
            scope = f"org:{scope[4:].strip()}"
        else:
            raise ConfigurationError(f"Invalid owner scope {value!r}. Use 'user' or 'org:ORGANIZATION'.")
        if scope.lower() not in {item.lower() for item in normalized}:
            normalized.append(scope)
    config["owners"] = normalized
    for key in ("exclude_repositories", "exclude_upstreams"):
        values = config.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ConfigurationError(f"{key} must be a list of repository full names.")
        config[key] = values
    return config


def load_owner_tokens() -> dict[str, str]:
    raw = os.getenv("FORKSYNC_OWNER_TOKENS_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("FORKSYNC_OWNER_TOKENS_JSON must be a JSON object mapping owner scopes to tokens.") from exc
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in data.items()
    ):
        raise ConfigurationError("FORKSYNC_OWNER_TOKENS_JSON must map owner scopes to non-empty token strings.")
    return {key.lower(): value.strip() for key, value in data.items()}


def _token_for_scope(scope: str, default_token: str, owner_tokens: dict[str, str]) -> str:
    candidates = [scope.lower()]
    if scope.lower().startswith("org:"):
        candidates.append(scope[4:].lower())
    return next((owner_tokens[key] for key in candidates if key in owner_tokens), default_token)


def _status_for_discovery_error(error: GitHubError) -> str:
    status = classify_github_error(error)
    return "DISCOVERY_FAILED" if status == "FAILED" else status


def build_sync_plan(
    client: GitHubClient,
    owners: list[str] | None = None,
    client_by_owner: dict[str, GitHubClient] | None = None,
    config: dict[str, Any] | None = None,
) -> list[SyncPlanEntry]:
    """Discover forks and validate every candidate before any sync writes."""
    settings = config or {"exclude_repositories": [], "exclude_upstreams": []}
    scopes = owners or ["user"]
    clients = {key.lower(): value for key, value in (client_by_owner or {}).items()}
    excluded_repositories = {v.lower() for v in settings.get("exclude_repositories", [])}
    excluded_upstreams = {v.lower() for v in settings.get("exclude_upstreams", [])}
    plan: list[SyncPlanEntry] = []

    for scope in scopes:
        scoped_client = clients.get(scope.lower(), client)
        try:
            if scope == "user":
                repos = scoped_client.owned_repositories()
            elif scope.startswith("org:"):
                repos = scoped_client.organization_repositories(scope[4:])
            else:
                raise ConfigurationError(f"Unsupported owner scope: {scope}")
        except GitHubError as exc:
            plan.append(SyncPlanEntry(
                f"{scope}/<discovery>", _status_for_discovery_error(exc), None, None,
                f"Could not discover repositories for {scope}: {exc}", scope,
            ))
            continue

        forks = sorted(
            (repo for repo in repos if repo.get("fork")),
            key=lambda repo: repo.get("full_name", "").lower(),
        )
        for repo in forks:
            name = repo.get("full_name") or "<unknown repository>"
            if name.lower() in excluded_repositories:
                plan.append(SyncPlanEntry(name, "SKIPPED", None, None, "Repository is explicitly excluded by configuration.", scope))
                continue
            if repo.get("archived", False):
                plan.append(SyncPlanEntry(name, "SKIPPED", None, None, "The fork is archived.", scope))
                continue
            try:
                details = scoped_client.repository_details(name)
            except GitHubError as exc:
                plan.append(SyncPlanEntry(name, classify_github_error(exc), None, None, f"Could not inspect fork metadata: {exc}", scope))
                continue
            if details.get("archived", False):
                plan.append(SyncPlanEntry(name, "SKIPPED", None, None, "The fork is archived.", scope))
                continue
            if details.get("fork") is False:
                plan.append(SyncPlanEntry(name, "SKIPPED", None, None, "Repository is no longer marked as a fork.", scope))
                continue

            parent = details.get("parent")
            upstream = parent.get("full_name") if isinstance(parent, dict) else None
            if not upstream:
                plan.append(SyncPlanEntry(name, "UPSTREAM_UNAVAILABLE", None, None, "Missing upstream parent metadata. The repository may have been detached from its original network.", scope))
                continue
            if upstream.lower() in excluded_upstreams:
                plan.append(SyncPlanEntry(name, "SKIPPED", details.get("default_branch"), upstream, "Upstream is explicitly excluded by configuration.", scope))
                continue

            branch = details.get("default_branch") or repo.get("default_branch")
            if not branch:
                plan.append(SyncPlanEntry(name, "BRANCH_MISSING", None, upstream, "Missing fork default-branch metadata.", scope))
                continue
            try:
                scoped_client.branch_details(upstream, branch)
            except GitHubError as exc:
                if exc.status_code == 404:
                    try:
                        scoped_client.repository_details(upstream)
                    except GitHubError as upstream_error:
                        if upstream_error.status_code == 404:
                            status = "UPSTREAM_UNAVAILABLE"
                            reason = (
                                f"Upstream repository {upstream} is not visible to this token or no longer exists "
                                "(GitHub returned 404). GitHub does not expose enough information to distinguish deletion, privacy and authorization hiding."
                            )
                        else:
                            status = classify_github_error(upstream_error)
                            reason = f"Could not inspect upstream repository {upstream}: {upstream_error}"
                    else:
                        status = "BRANCH_MISSING"
                        reason = f"Upstream repository exists, but branch '{branch}' is not present or not visible."
                else:
                    status = classify_github_error(exc)
                    reason = f"Could not validate upstream branch '{branch}' on {upstream}: {exc}"
                plan.append(SyncPlanEntry(name, status, branch, upstream, reason, scope))
                continue

            plan.append(SyncPlanEntry(name, "READY", branch, upstream, f"Upstream branch '{branch}' exists and is ready to sync.", scope))
    return plan


def sync_all(
    client: GitHubClient,
    dry_run: bool = False,
    owners: list[str] | None = None,
    client_by_owner: dict[str, GitHubClient] | None = None,
    config: dict[str, Any] | None = None,
) -> list[Outcome]:
    plan = build_sync_plan(client, owners, client_by_owner, config)
    outcomes: list[Outcome] = []
    ready = sum(entry.status == "READY" for entry in plan)
    print(
        f"Sync plan: {len(plan)} forks; {ready} ready; "
        f"{sum(entry.status == 'SKIPPED' for entry in plan)} skipped; "
        f"{sum(entry.status not in {'READY', 'SKIPPED'} for entry in plan)} require attention."
    )
    clients = {key.lower(): value for key, value in (client_by_owner or {}).items()}
    for entry in plan:
        if entry.status != "READY":
            outcomes.append(Outcome(entry.repository, entry.status, entry.reason, entry.branch, entry.upstream, entry.owner_scope))
            continue
        if dry_run:
            outcomes.append(Outcome(entry.repository, "DRY_RUN", f"Would sync {entry.branch} from {entry.upstream}.", entry.branch, entry.upstream, entry.owner_scope))
            continue
        try:
            scoped_client = clients.get(entry.owner_scope.lower(), client)
            result = scoped_client.sync_fork(entry.repository, entry.branch or "")
            merge_type = str(result.get("merge_type", "synced")).lower()
            status = "UP_TO_DATE" if merge_type in {"none", "up_to_date", "up-to-date"} else "SYNCED"
            outcomes.append(Outcome(entry.repository, status, f"{entry.branch}: {merge_type}; upstream {entry.upstream}", entry.branch, entry.upstream, entry.owner_scope))
        except GitHubError as exc:
            outcomes.append(Outcome(entry.repository, classify_github_error(exc), str(exc), entry.branch, entry.upstream, entry.owner_scope))

    for item in outcomes:
        print(f"[{item.status}] {item.repository}: {item.message}")
    successful = sum(item.status in {"SYNCED", "UP_TO_DATE", "DRY_RUN"} for item in outcomes)
    skipped = sum(item.status == "SKIPPED" for item in outcomes)
    attention = sum(item.status in ATTENTION_STATUSES for item in outcomes)
    print(f"Summary: {successful} synced/current/preview, {skipped} skipped, {attention} require attention.")
    return outcomes


def _counts(outcomes: list[Outcome]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in outcomes:
        counts[item.status] = counts.get(item.status, 0) + 1
    return dict(sorted(counts.items()))


def build_report(outcomes: list[Outcome], dry_run: bool) -> dict[str, Any]:
    items = [item.to_dict() for item in outcomes]
    attention = [item for item in items if item["requires_attention"]]
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "totals": {
            "repositories": len(items),
            "synced": sum(item.status == "SYNCED" for item in outcomes),
            "up_to_date": sum(item.status == "UP_TO_DATE" for item in outcomes),
            "skipped": sum(item.status == "SKIPPED" for item in outcomes),
            "requiring_attention": len(attention),
            "by_status": _counts(outcomes),
        },
        "repositories": items,
        "attention_items": attention,
    }


def _markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def render_markdown_summary(report: dict[str, Any]) -> str:
    totals = report["totals"]
    lines = [
        "## ForkSync report",
        "",
        f"Generated: {report['generated_at']}  ",
        f"Mode: {'dry run' if report['dry_run'] else 'live sync'}  ",
        f"Repositories inspected: **{totals['repositories']}** · "
        f"Synced: **{totals['synced']}** · Up to date: **{totals['up_to_date']}** · "
        f"Skipped: **{totals['skipped']}** · Need attention: **{totals['requiring_attention']}**",
        "",
    ]
    counts = totals.get("by_status", {})
    if counts:
        lines.extend(["### Status breakdown", "", "| Status | Count |", "|---|---:|"])
        lines.extend(f"| {status} | {count} |" for status, count in counts.items())
        lines.append("")
    attention = report["attention_items"]
    if not attention:
        lines.extend(["### Manual attention", "", "No forks currently require manual attention. Previously open attention items can be closed."])
    else:
        lines.extend([
            "### Manual attention required", "",
            "These repositories were not synchronized. Fork and upstream names are linked; use the recommended action to correct the cause.",
            "", "| Fork | Upstream | Status | Reason | Recommended next action |",
            "|---|---|---|---|---|",
        ])
        for item in attention:
            fork = f"[{_markdown_cell(item['repository'])}]({item['repository_url']})" if item["repository_url"] else _markdown_cell(item["repository"])
            upstream = f"[{_markdown_cell(item['upstream'])}]({item['upstream_url']})" if item["upstream_url"] else "—"
            lines.append(
                f"| {fork} | {upstream} | {_markdown_cell(item['status'])} | "
                f"{_markdown_cell(item['message'])} | {_markdown_cell(item['recommended_action'])} |"
            )
    lines.extend(["", "Full machine-readable details are attached as the forksync-report workflow artifact.", ""])
    return "\n".join(lines)


def write_report(outcomes: list[Outcome], dry_run: bool, report_path: str | None = None) -> dict[str, Any]:
    report = build_report(outcomes, dry_run)
    destination = Path(report_path or os.getenv("FORKSYNC_REPORT_JSON", "forksync-report.json"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write(render_markdown_summary(report))
    print(f"JSON report written to {destination}")
    return report


def should_fail(outcomes: list[Outcome]) -> bool:
    statuses = {item.status for item in outcomes}
    fatal = bool(statuses & FATAL_STATUSES)
    strict = os.getenv("FORKSYNC_STRICT", "false").lower() in {"1", "true", "yes"}
    fail_on_conflict = os.getenv("FORKSYNC_FAIL_ON_CONFLICT", "false").lower() in {"1", "true", "yes"}
    return fatal or (strict and bool(statuses & ATTENTION_STATUSES)) or (fail_on_conflict and "CONFLICT" in statuses)


def main() -> int:
    dry_run = os.getenv("FORKSYNC_DRY_RUN", "false").lower() in {"1", "true", "yes"}
    outcomes: list[Outcome] = []
    try:
        config = load_config()
        owner_tokens = load_owner_tokens()
        default_token = os.getenv("FORKSYNC_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
        if not default_token and not owner_tokens:
            raise ConfigurationError("Missing token. Set FORKSYNC_TOKEN or provide FORKSYNC_OWNER_TOKENS_JSON.")
        clients: dict[str, GitHubClient] = {}
        for scope in config["owners"]:
            token = _token_for_scope(scope, default_token, owner_tokens)
            if not token:
                raise ConfigurationError(f"No token is configured for {scope}. Set FORKSYNC_TOKEN or add it to FORKSYNC_OWNER_TOKENS_JSON.")
            clients[scope.lower()] = GitHubClient(token)
        primary = next(iter(clients.values()))
        outcomes = sync_all(primary, dry_run=dry_run, owners=config["owners"], client_by_owner=clients, config=config)
    except (GitHubError, ConfigurationError) as exc:
        status = "CONFIGURATION_ERROR" if isinstance(exc, ConfigurationError) else classify_github_error(exc)
        outcomes.append(Outcome("<global>", status, str(exc), owner_scope="global"))
        print(f"Fatal ForkSync error: {exc}", file=sys.stderr)

    try:
        write_report(outcomes, dry_run)
    except OSError as exc:
        print(f"Could not write ForkSync report: {exc}", file=sys.stderr)
        return 1
    return 1 if should_fail(outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
