# ForkSync

A small GitHub Actions utility that synchronizes the default branch of non-archived forks with their upstream repositories. Supports scheduled runs, manual runs, and dry-run previews.

## Behavior and safety

- Discovers repositories accessible to the authenticated account, with pagination.
- Fetches each fork's repository details to reliably identify its upstream. The list-repositories endpoint may not include the `parent` metadata needed for this check.
- Syncs only the default branch of non-archived forks that have an upstream parent.
- Skips this utility repository, feature branches, and archived forks.
- Never force-pushes, deletes branches, changes settings, or logs the token.
- Continues after an individual fork fails and reports each result.
- Diverged branches or permission/upstream problems may require manual resolution.

## Create a fine-grained personal access token

1. Open [GitHub fine-grained personal access tokens](https://github.com/settings/personal-access-tokens) and click **Generate new token**.
2. Choose a descriptive token name and an expiry you can maintain.
3. Set **Resource owner** to the account that owns the repositories you want to sync.
4. Under **Repository access**, choose **All repositories** to discover and sync all owned forks. Alternatively, choose **Only select repositories** and include this utility repository plus every fork to sync. Add future forks if needed.
5. Under **Repository permissions**, set **Contents** to **Read and write**. Leave other repository permissions at **No access**; **Metadata: Read-only** is automatically granted.
6. Generate the token and copy it immediately. Do not commit or share it.
7. In this repository, open **Settings → Secrets and variables → Actions → New repository secret**.
8. Set the secret name to `FORKSYNC_TOKEN`, paste the token as its value, and save.

The token must include every fork you want to sync. Organization-owned repositories may also require organization approval or policy changes.

## Run automatically or manually

The workflow runs daily. GitHub Actions schedules are approximate and can be delayed; scheduled workflows run from the default branch.

To run manually, open [Actions → Sync forks](../../actions/workflows/sync-forks.yml), select **Run workflow**, then choose `dry_run = true` to preview or `false` to perform syncs. Check the job log for each repository's outcome.

## Run locally

Requires Python 3.11+ and no third-party dependencies.

PowerShell:
```powershell
$env:FORKSYNC_TOKEN = "github_pat_your_token_here"
$env:FORKSYNC_REPOSITORY = "<OWNER>/<REPOSITORY>"
$env:FORKSYNC_DRY_RUN = "true"
python src/forksync.py
```

Bash:
```bash
export FORKSYNC_TOKEN="github_pat_your_token_here"
export FORKSYNC_REPOSITORY="<OWNER>/<REPOSITORY>"
FORKSYNC_DRY_RUN=true python src/forksync.py
```

## Tests

```bash
python -m unittest discover -s tests -v
```

## API

Uses GitHub's [Sync a fork branch with the upstream repository](https://docs.github.com/en/rest/branches/branches#sync-a-fork-branch-with-the-upstream-repository) endpoint.
