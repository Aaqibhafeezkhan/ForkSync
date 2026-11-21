# ForkSync

ForkSync discovers forks owned by a personal GitHub account and configured organizations, then synchronizes each fork's default branch with the corresponding upstream branch.

It is designed for large fork collections: one unavailable upstream or merge conflict should be visible and actionable without making every otherwise successful synchronization appear broken.

## What it does

- Discovers personal-account forks and configured organization-owned forks, with pagination.
- Uses the paginated repository listing as the discovery source, then lets GitHub's merge-upstream API validate each fork's upstream and default branch.
- Synchronizes eligible default branches independently with bounded concurrency, so an unavailable upstream or conflict cannot block healthy forks.
- Never force-pushes, deletes branches, resolves conflicts automatically, changes repository settings, or logs tokens.
- Rechecks unavailable upstreams on every scheduled or manual run. A fork becomes healthy in the report automatically when its upstream is visible again and can be synchronized.
- Classifies expected per-repository conditions separately from unexpected operational failures.
- Writes a Markdown report to the Actions run summary and a JSON report artifact.
- Maintains one open issue titled "ForkSync: Manual attention queue". The issue is updated as problems change and is closed automatically when no forks need manual attention.

## Quick start

Requirements: Python 3.11+ and a GitHub token.

1. Create a fine-grained personal access token for the repositories you own.
2. Give it repository Metadata: Read-only and Contents: Read and write.
3. If needed, approve the token for your organization and ensure it can access every fork to be managed.
4. In this repository, open Settings → Secrets and variables → Actions.
5. Create a repository secret named FORKSYNC_TOKEN.
6. Run Actions → Sync forks with dry_run enabled first. Review the summary and report artifact before running live.

The default configuration monitors the authenticated personal account. The repository's forksync.json file excludes ForkSync itself.

## Personal and organization owners

Configure the Actions variable FORKSYNC_OWNERS with comma-separated scopes:

    user,org:Aaqib-OSS

Use the literal scope user for the personal account associated with the personal token. Use org:ORGANIZATION for each organization.

A fine-grained personal access token is tied to one resource owner. When personal and organization repositories need different tokens, create the tokens separately and save them in the repository or environment secret FORKSYNC_OWNER_TOKENS_JSON. Its value must be a JSON object:

    {"user":"<personal-token>","org:Aaqib-OSS":"<organization-token>"}

The ordinary FORKSYNC_TOKEN remains the fallback for scopes without a matching entry. For example, FORKSYNC_TOKEN can cover the personal account while FORKSYNC_OWNER_TOKENS_JSON contains only the organization token.

Each token must be authorized for the repositories under its resource owner and have the required repository permissions. Organization approval may be required. ForkSync does not print token values.

For a larger long-lived installation, a GitHub App installed on each relevant owner is preferable to manually rotated PATs. App installation tokens are short-lived and can be scoped to the repositories and permissions that ForkSync needs. The current implementation accepts PAT-based owner tokens; GitHub App token minting can be added as a deployment-specific authentication layer.

## Configuration

The optional forksync.json file supports:

- owners: owner scopes to inspect. Defaults to ["user"].
- exclude_repositories: fork full names to ignore.
- exclude_upstreams: upstream full names whose forks should be ignored.

Example:

    {
      "owners": ["user", "org:Aaqib-OSS"],
      "exclude_repositories": ["Aaqibhafeezkhan/ForkSync"],
      "exclude_upstreams": []
    }

Environment variables override the corresponding configuration:

| Variable | Purpose |
|---|---|
| FORKSYNC_TOKEN | Default token or fallback token |
| FORKSYNC_OWNER_TOKENS_JSON | JSON map of owner scope to token |
| FORKSYNC_OWNERS | Comma-separated scopes, e.g. user,org:Aaqib-OSS |
| FORKSYNC_EXCLUDE_REPOSITORIES | Comma-separated fork full names to exclude |
| FORKSYNC_EXCLUDE_UPSTREAMS | Comma-separated upstream full names to exclude |
| FORKSYNC_DRY_RUN | Preview without making synchronization writes |
| FORKSYNC_STRICT | Return a failing exit code when any manual-attention status exists |
| FORKSYNC_FAIL_ON_CONFLICT | Independently fail the run when a merge conflict is reported |
| FORKSYNC_REPORT_JSON | Override the JSON report path |
| FORKSYNC_CONFIG | Override the config file path |
| FORKSYNC_MAX_RATE_LIMIT_WAIT | Maximum wait for a known exhausted primary rate limit |

The scheduled workflow runs in normal non-strict mode. Expected repository-specific conditions are reported but do not fail the entire run. Invalid authentication, unexpected API/network failures, discovery failures and report-write failures still fail the job.

## Statuses and what to do

| Status | Meaning | Operator action |
|---|---|---|
| SYNCED | Fork was updated from upstream | None |
| UP_TO_DATE | No upstream changes were needed | None |
| SKIPPED | Archived or explicitly excluded | None unless the exclusion is unintended |
| UPSTREAM_UNAVAILABLE | Upstream is not visible or its parent metadata is unavailable | Check for deletion, privacy change, rename, transfer, or access restrictions |
| BRANCH_MISSING | Upstream repository is visible but the fork's configured default branch is not | Identify the correct upstream branch and align the fork branch deliberately |
| TOKEN_POLICY_BLOCKED | Upstream organization rejected the token based on its policy | Create an accepted token, for example one with an expiry of 366 days or less when required, then update the relevant secret |
| ACCESS_RESTRICTED | Token lacks permission or organization policy blocks access | Review repository selection, organization approval and token policy |
| AUTHENTICATION_FAILED | GitHub rejected the credential | Rotate or replace the token |
| CONFLICT | GitHub cannot merge upstream because the branches diverged | Resolve manually; ForkSync never force-pushes |
| SYNC_REJECTED | GitHub rejected the sync for another validation or repository-rule reason | Inspect the API response and repository rules |
| FAILED | Unexpected API or network failure | Inspect the run logs and retry after addressing the cause |
| DISCOVERY_FAILED | ForkSync could not list repositories for an owner | Check owner scope, token access, organization approval and GitHub API availability |

A GitHub 404 is intentionally not described as proof of shadow-banning. GitHub may return 404 when a repository does not exist or when it is not visible to the authenticated token. ForkSync checks the upstream repository metadata after a missing-branch response; if the repository itself is still not visible, it records UPSTREAM_UNAVAILABLE and does not claim to know why.

## Where to find manual corrections

Every run produces three ways to see the exact forks that need attention:

1. **Actions run summary:** Open Actions → Sync forks → the latest run → Summary. The Manual attention required table lists the fork, upstream, status, API reason and recommended next action.
2. **JSON artifact:** Download the forksync-report artifact from the same run. It contains every repository result plus an attention_items array that can be processed by other tools.
3. **Persistent tracker issue:** Open Issues → ForkSync: Manual attention queue. This issue is created or updated by each run and lists only current attention items. When a later run no longer reports any attention items, the issue is closed automatically.

A green scheduled run therefore does not mean every upstream is accessible. It means the sync job completed according to the configured failure policy. Always review the summary or tracker issue when it reports attention items.

## Suggested correction workflow

- UPSTREAM_UNAVAILABLE: Open the upstream URL in a browser while signed in. Check whether it was deleted, made private, renamed or transferred. Ask the upstream owner for access if appropriate. Keep your fork intact; ForkSync will recheck the upstream on the next run.
- BRANCH_MISSING: Compare the fork default branch with the current upstream default branch and available branch list. Decide whether to change the fork's default branch or whether the fork should remain independent.
- TOKEN_POLICY_BLOCKED: Create a new fine-grained PAT with an expiry accepted by the upstream organization, authorize the required repositories, replace the relevant Actions secret and rerun.
- ACCESS_RESTRICTED: Review token repository selection and organization approval. Some organizations restrict fine-grained PAT use; a GitHub App installed and approved by that organization may be a better long-term option.
- CONFLICT: Fetch the upstream and fork branches locally, inspect the divergence, resolve conflicts, run the repository's tests and push the reviewed resolution to your fork. Do not force-push as a workaround.
- SYNC_REJECTED: Read the GitHub API message in the report and check branch protection, repository rules and permissions.

## Actions workflow

The scheduled workflow runs daily. GitHub Actions schedules are approximate and can be delayed; schedule triggers run from the repository's default branch.

Manual dispatch inputs:

- dry_run: validate and report without synchronization writes.
- strict_mode: fail the run if any fork requires attention.
- fail_on_conflict: fail the run if at least one conflict is found.

The workflow uses Node 24-compatible versions of checkout, setup-python, upload-artifact and github-script. CI runs on pushes and pull requests and performs Python compilation and unit tests.

The tracker issue uses the workflow's built-in GITHUB_TOKEN with Issues: Read and write permission. The PAT used for fork synchronization is separate and does not need issue-write permission.

## Run locally

PowerShell:

    $env:FORKSYNC_TOKEN = "github_pat_your_token_here"
    $env:FORKSYNC_OWNERS = "user"
    $env:FORKSYNC_DRY_RUN = "true"
    python src/forksync.py

Bash:

    export FORKSYNC_TOKEN="github_pat_your_token_here"
    export FORKSYNC_OWNERS="user"
    FORKSYNC_DRY_RUN=true python src/forksync.py

Run the tests:

    python -m unittest discover -s tests -v

## Safety

- Only fork default branches are considered.
- GitHub validates the upstream repository and target branch as part of each isolated merge-upstream request.
- No automatic conflict resolution, force-push, branch deletion or repository mutation outside the GitHub fork-sync endpoint is performed.
- Expected per-fork exceptions are reported individually.
- Upstreams that are unavailable are rechecked every run; no permanent "shadow-ban" assumption is stored.
- Tokens are read from environment variables and never written into reports.

## License

MIT
