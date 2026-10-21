import unittest
from unittest.mock import Mock, patch

from src.forksync import GitHubClient, GitHubError, sync_all


class GitHubClientTests(unittest.TestCase):
    def test_owned_repositories_paginates(self):
        client = GitHubClient("token")
        client.request = Mock(side_effect=[[{"id": 1}] * 100, [{"id": 2}]])
        self.assertEqual(len(client.owned_repositories()), 101)
        self.assertEqual(client.request.call_count, 2)

    def test_only_non_archived_forks_with_upstream_are_synced(self):
        client = GitHubClient("token")
        client.owned_repositories = Mock(return_value=[
            {"full_name": "me/fork", "fork": True, "default_branch": "main"},
            {"full_name": "me/original", "fork": False, "default_branch": "main"},
            {"full_name": "me/old", "fork": True, "default_branch": "main", "archived": True},
            {"full_name": "me/no-upstream", "fork": True, "default_branch": "main"},
        ])
        client.repository_details = Mock(side_effect=[
            {"full_name": "me/fork", "default_branch": "main", "parent": {"full_name": "upstream/project"}},
            {"full_name": "me/no-upstream", "default_branch": "main"},
        ])
        client.sync_fork = Mock(return_value={"merge_type": "fast-forward"})
        with patch.dict("os.environ", {"FORKSYNC_REPOSITORY": "me/forksync"}):
            outcomes = sync_all(client)
        self.assertEqual([item.repository for item in outcomes], ["me/fork"])
        client.sync_fork.assert_called_once_with("me/fork", "main")
        self.assertEqual(outcomes[0].status, "OK")
        self.assertEqual(client.repository_details.call_count, 2)

    def test_dry_run_does_not_write(self):
        client = GitHubClient("token")
        client.owned_repositories = Mock(return_value=[
            {"full_name": "me/fork", "fork": True, "default_branch": "main"}
        ])
        client.repository_details = Mock(return_value={
            "full_name": "me/fork", "default_branch": "main",
            "parent": {"full_name": "upstream/project"},
        })
        client.sync_fork = Mock()
        outcomes = sync_all(client, dry_run=True)
        self.assertEqual(outcomes[0].status, "DRY RUN")
        client.sync_fork.assert_not_called()

    def test_repository_details_error_is_reported(self):
        client = GitHubClient("token")
        client.owned_repositories = Mock(return_value=[
            {"full_name": "me/fork", "fork": True, "default_branch": "main"}
        ])
        client.repository_details = Mock(side_effect=GitHubError("permission denied"))
        outcomes = sync_all(client)
        self.assertEqual(outcomes[0].status, "FAILED")

    def test_sync_api_error_is_reported(self):
        client = GitHubClient("token")
        client.owned_repositories = Mock(return_value=[
            {"full_name": "me/fork", "fork": True, "default_branch": "main"}
        ])
        client.repository_details = Mock(return_value={
            "full_name": "me/fork", "default_branch": "main",
            "parent": {"full_name": "upstream/project"},
        })
        client.sync_fork = Mock(side_effect=GitHubError("permission denied"))
        self.assertEqual(sync_all(client)[0].status, "FAILED")


if __name__ == "__main__":
    unittest.main()
