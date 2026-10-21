# ForkSync

Automatically synchronize the **default branch** of every non-archived GitHub fork you own with its upstream repository. Runs daily in GitHub Actions and supports manual runs and dry runs.

## Behavior and safety

- Discovers all owned repositories via the GitHub REST API, with pagination.
- Syncs only the default branch of non-archived forks with an upstream parent.
- Skips this ForkSync repository, feature branches, and archived forks.
- Never force-pushes, deletes branches, changes settings, or logs the token.
- Continues after an individual fork fails and reports each result.
- Diverged branches or permission/upstream problems may require manual resolution.

## Create a fine-grained personal access token

1. Go to [GitHub fine-grained personal access tokens](https://github.com/settings/personal-access-tokens) and click **Generate new token**.
2. Name it `ForkSync GitHub Actions` and choose an expiry you can maintain.
3. Set **Resource owner** to `Aaqibhafeezkhan`.
4. Under **Repository access**, choose **All repositories** to discover and sync all your owned forks. If you choose **Only select repositories**, include ForkSync and every fork to sync; add future forks manually.
5. Under **Repository permissions**, set **Contents** to **Read and write**. Leave other repository permissions at **No access**; **Metadata: Read-only** is automatically granted.
6. Click **Generate token** and copy it immediately. GitHub will not display the full token again. Do not commit or share it.
7. Open this repository's **Settings → Secrets and variables → Actions → New repository secret**.
8. Name the secret `FORKSYNC_TOKEN`, paste the token as its value, and save.

The token must include every fork you want to sync. Organization-owned repositories may also require organization approval or policy changes.

## Run automatically or manually

The schedule is **06:17 UTC daily (11:47 AM India time)**. GitHub Actions schedules are approximate and can be delayed; scheduled workflows run from the default branch.

To run manually, open [Actions → Sync forks](../../actions/workflows/sync-forks.yml), select **Run workflow**, then choose `dry_run = true` to preview or `false` to perform syncs. Check the job log for each repository's outcome.

## Run locally

Requires Python 3.11+ and no third-party dependencies.

PowerShell:
```powershell
$env:FORKSYNC_TOKEN = "github_pat_your_token_here"
$env:FORKSYNC_REPOSITORY = "Aaqibhafeezkhan/ForkSync"
$env:FORKSYNC_DRY_RUN = "true"
python src/forksync.py
```

Bash:
```bash
export FORKSYNC_TOKEN="github_pat_your_token_here"
export FORKSYNC_REPOSITORY="Aaqibhafeezkhan/ForkSync"
FORKSYNC_DRY_RUN=true python src/forksync.py
```

## Tests
```bash
python -m unittest discover -s tests -v
```

## API
Uses GitHub's [Sync a fork branch with the upstream repository](https://docs.github.com/en/rest/branches/branches#sync-a-fork-branch-with-the-upstream-repository) endpoint.
