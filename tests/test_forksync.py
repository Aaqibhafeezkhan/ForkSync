import io
import unittest
import urllib.error
from unittest.mock import Mock, patch

from src.forksync import GitHubClient, GitHubError, sync_all


class GitHubClientTests(unittest.TestCase):
    def _http_error(self, status, headers=None):
        return urllib.error.HTTPError(
            "https://api.github.com/test",
            status,
            "error",
            headers or {},
            io.BytesIO(b'{"message":"temporary failure"}'),
        )

    @patch("src.forksync.time.sleep")
    @patch("src.forksync.urllib.request.urlopen")
    def test_transient_502_is_retried_and_succeeds(self, urlopen, sleep):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok": true}'
        urlopen.side_effect = [self._http_error(502), response]

        result = GitHubClient("token").request("GET", "/test")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)

    @patch("src.forksync.time.sleep")
    @patch("src.forksync.urllib.request.urlopen")
    def test_persistent_5xx_exhausts_retries(self, urlopen, sleep):
        urlopen.side_effect = [
            self._http_error(502),
            self._http_error(503),
            self._http_error(504),
        ]

        with self.assertRaisesRegex(GitHubError, r"failed \(504\)"):
            GitHubClient("token").request("GET", "/test")

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])

    @patch("src.forksync.time.sleep")
    @patch("src.forksync.urllib.request.urlopen")
    def test_retry_after_header_is_respected(self, urlopen, sleep):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok": true}'
        urlopen.side_effect = [self._http_error(503, {"Retry-After": "7"}), response]

        GitHubClient("token").request("GET", "/test")

        sleep.assert_called_once_with(7.0)
        self.assertEqual(urlopen.call_count, 2)

    @patch("src.forksync.time.sleep")
    @patch("src.forksync.urllib.request.urlopen")
    def test_authentication_and_permission_errors_are_not_retried(self, urlopen, sleep):
        urlopen.side_effect = self._http_error(401)

        with self.assertRaisesRegex(GitHubError, r"failed \(401\)"):
            GitHubClient("token").request("GET", "/test")

        urlopen.assert_called_once()
        sleep.assert_not_called()


    @patch("src.forksync.time.sleep")
    @patch("src.forksync.time.time", return_value=1000)
    @patch("src.forksync.urllib.request.urlopen")
    def test_primary_rate_limit_response_waits_for_reset_and_retries(
        self, urlopen, current_time, sleep
    ):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = {"X-RateLimit-Remaining": "5"}
        response.read.return_value = b'{"ok": true}'
        urlopen.side_effect = [
            self._http_error(
                403,
                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1010"},
            ),
            response,
        ]

        result = GitHubClient("token").request("GET", "/test")

        self.assertEqual(result, {"ok": True})
        sleep.assert_called_once_with(11.0)
        self.assertEqual(urlopen.call_count, 2)

    def test_exhausted_rate_limit_without_reset_fails_actionably(self):
        client = GitHubClient("token")
        client.rate_limit_remaining = 0

        with self.assertRaisesRegex(GitHubError, "X-RateLimit-Reset is unavailable"):
            client.request("GET", "/test")


    @patch("src.forksync.time.sleep")
    @patch("src.forksync.time.time", return_value=1000)
    @patch("src.forksync.urllib.request.urlopen")
    def test_primary_rate_limit_wait_exceeding_cap_fails_without_request(
        self, urlopen, current_time, sleep
    ):
        client = GitHubClient("token")
        client.rate_limit_remaining = 0
        client.rate_limit_reset = 2000

        with self.assertRaisesRegex(GitHubError, "exceeding the configured maximum"):
            client.request("GET", "/test")

        urlopen.assert_not_called()
        sleep.assert_not_called()

    @patch("src.forksync.time.sleep")
    @patch("src.forksync.urllib.request.urlopen")
    def test_secondary_rate_limit_honors_retry_after(self, urlopen, sleep):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = {"X-RateLimit-Remaining": "20"}
        response.read.return_value = b'{"ok": true}'
        urlopen.side_effect = [
            self._http_error(429, {"Retry-After": "9"}),
            response,
        ]

        GitHubClient("token").request("GET", "/test")

        sleep.assert_called_once_with(9.0)
        self.assertEqual(urlopen.call_count, 2)

    @patch("src.forksync.time.sleep")
    @patch("src.forksync.urllib.request.urlopen")
    def test_ordinary_forbidden_response_is_not_retried(self, urlopen, sleep):
        urlopen.side_effect = self._http_error(403, {"X-RateLimit-Remaining": "15"})

        with self.assertRaisesRegex(GitHubError, r"failed \(403\)"):
            GitHubClient("token").request("GET", "/test")

        urlopen.assert_called_once()
        sleep.assert_not_called()

    def test_malformed_rate_limit_headers_do_not_overwrite_state(self):
        client = GitHubClient("token")
        client.rate_limit_remaining = 5
        client.rate_limit_reset = 1234
        client._update_rate_limit({
            "X-RateLimit-Remaining": "not-a-number",
            "X-RateLimit-Reset": "invalid",
        })
        self.assertEqual(client.rate_limit_remaining, 5)
        self.assertEqual(client.rate_limit_reset, 1234)

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
