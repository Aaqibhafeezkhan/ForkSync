import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import Mock, patch

from src.forksync import (
    ConfigurationError,
    GitHubClient,
    GitHubError,
    Outcome,
    build_report,
    build_sync_plan,
    classify_github_error,
    load_config,
    load_owner_tokens,
    render_markdown_summary,
    should_fail,
    sync_all,
    write_report,
)


class GitHubClientTests(unittest.TestCase):
    def http_error(self, status, headers=None, body=b'{"message":"failure"}'):
        return urllib.error.HTTPError(
            "https://api.github.com/test", status, "error", headers or {}, io.BytesIO(body)
        )

    def response(self, body=b'{"ok": true}', headers=None):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = headers or {}
        response.read.return_value = body
        return response

    @patch("src.forksync.time.sleep")
    @patch("src.forksync.urllib.request.urlopen")
    def test_transient_502_is_retried(self, urlopen, sleep):
        urlopen.side_effect = [self.http_error(502), self.response()]
        self.assertEqual(GitHubClient("token").request("GET", "/test"), {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)

    @patch("src.forksync.time.sleep")
    @patch("src.forksync.urllib.request.urlopen")
    def test_5xx_retries_are_bounded(self, urlopen, sleep):
        urlopen.side_effect = [self.http_error(502), self.http_error(503), self.http_error(504)]
        with self.assertRaises(GitHubError) as context:
            GitHubClient("token").request("GET", "/test")
        self.assertEqual(context.exception.status_code, 504)
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [1.0, 2.0])

    @patch("src.forksync.time.sleep")
    @patch("src.forksync.urllib.request.urlopen")
    def test_retry_after_is_respected(self, urlopen, sleep):
        urlopen.side_effect = [
            self.http_error(503, {"Retry-After": "7"}),
            self.response(),
        ]
        GitHubClient("token").request("GET", "/test")
        sleep.assert_called_once_with(7.0)

    @patch("src.forksync.time.sleep")
    @patch("src.forksync.urllib.request.urlopen")
    def test_401_is_not_retried_and_status_is_preserved(self, urlopen, sleep):
        urlopen.side_effect = self.http_error(401)
        with self.assertRaises(GitHubError) as context:
            GitHubClient("token").request("GET", "/test")
        self.assertEqual(context.exception.status_code, 401)
        urlopen.assert_called_once()
        sleep.assert_not_called()

    @patch("src.forksync.time.sleep")
    @patch("src.forksync.time.time", return_value=1000)
    @patch("src.forksync.urllib.request.urlopen")
    def test_primary_rate_limit_waits_for_reset(self, urlopen, current_time, sleep):
        urlopen.side_effect = [
            self.http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1010"}),
            self.response(headers={"X-RateLimit-Remaining": "5"}),
        ]
        self.assertEqual(GitHubClient("token").request("GET", "/test"), {"ok": True})
        sleep.assert_called_once_with(11.0)

    def test_rate_limit_without_reset_fails_actionably(self):
        client = GitHubClient("token")
        client.rate_limit_remaining = 0
        with self.assertRaisesRegex(GitHubError, "X-RateLimit-Reset is unavailable"):
            client.request("GET", "/test")

    @patch("src.forksync.time.sleep")
    @patch("src.forksync.time.time", return_value=1000)
    @patch("src.forksync.urllib.request.urlopen")
    def test_rate_limit_wait_is_bounded(self, urlopen, current_time, sleep):
        client = GitHubClient("token")
        client.rate_limit_remaining = 0
        client.rate_limit_reset = 2000
        with self.assertRaisesRegex(GitHubError, "exceeding the configured maximum"):
            client.request("GET", "/test")
        urlopen.assert_not_called()
        sleep.assert_not_called()

    def test_malformed_rate_headers_do_not_overwrite_known_state(self):
        client = GitHubClient("token")
        client.rate_limit_remaining = 9
        client.rate_limit_reset = 1234
        client._update_rate_limit({
            "X-RateLimit-Remaining": "bad", "X-RateLimit-Reset": "bad"
        })
        self.assertEqual(client.rate_limit_remaining, 9)
        self.assertEqual(client.rate_limit_reset, 1234)

    def test_personal_repository_pagination(self):
        client = GitHubClient("token")
        client.request = Mock(side_effect=[[{"id": 1}] * 100, [{"id": 2}]])
        self.assertEqual(len(client.owned_repositories()), 101)
        self.assertEqual(client.request.call_count, 2)
        first_request_path = client.request.call_args_list[0].args[1]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(first_request_path).query)
        self.assertEqual(query["type"], ["owner"])
        self.assertNotIn("affiliation", query)

    def test_organization_repository_endpoint_and_pagination(self):
        client = GitHubClient("token")
        client.request = Mock(return_value=[{"full_name": "Aaqib-OSS/demo", "fork": True}])
        result = client.organization_repositories("Aaqib-OSS")
        self.assertEqual(result[0]["full_name"], "Aaqib-OSS/demo")
        self.assertIn("/orgs/Aaqib-OSS/repos?", client.request.call_args.args[1])

    def test_branch_name_is_url_encoded(self):
        client = GitHubClient("token")
        client.request = Mock(return_value={"name": "feature/one"})
        client.branch_details("upstream/project", "feature/one")
        client.request.assert_called_once_with(
            "GET", "/repos/upstream/project/branches/feature%2Fone"
        )


class ErrorClassificationTests(unittest.TestCase):
    def test_classifies_error_categories(self):
        cases = [
            (401, "", "AUTHENTICATION_FAILED"),
            (403, '{"message":"Resource not accessible by integration"}', "ACCESS_RESTRICTED"),
            (403, '{"message":"The organization forbids access via a fine-grained personal access token; maximum 366 days"}', "TOKEN_POLICY_BLOCKED"),
            (404, '{"message":"Not Found"}', "UPSTREAM_UNAVAILABLE"),
            (409, '{"message":"There are merge conflicts"}', "CONFLICT"),
            (422, '{"message":"Validation failed"}', "SYNC_REJECTED"),
            (500, '{"message":"Internal Server Error"}', "FAILED"),
        ]
        for code, body, expected in cases:
            with self.subTest(code=code, expected=expected):
                error = GitHubError("GitHub API error", code, body)
                self.assertEqual(classify_github_error(error), expected)


class SyncPlanTests(unittest.TestCase):
    def configured_client(self, repository="me/fork", upstream="upstream/project", branch="main"):
        client = GitHubClient("token")
        client.owned_repositories = Mock(return_value=[
            {
                "full_name": repository, "fork": True, "default_branch": branch,
                "parent": {"full_name": upstream},
            }
        ])
        client.repository_details = Mock()
        client.branch_details = Mock()
        client.sync_fork = Mock(return_value={"merge_type": "fast-forward"})
        return client

    def test_healthy_fork_syncs_without_per_repository_preflight(self):
        client = self.configured_client()
        outcomes = sync_all(client)
        self.assertEqual(outcomes[0].status, "SYNCED")
        client.sync_fork.assert_called_once_with("me/fork", "main")
        client.repository_details.assert_not_called()
        client.branch_details.assert_not_called()

    def test_dry_run_does_not_write(self):
        client = self.configured_client()
        outcomes = sync_all(client, dry_run=True)
        self.assertEqual(outcomes[0].status, "DRY_RUN")
        client.sync_fork.assert_not_called()

    def test_up_to_date_is_reported_separately(self):
        client = self.configured_client()
        client.sync_fork.return_value = {"merge_type": "none"}
        self.assertEqual(sync_all(client)[0].status, "UP_TO_DATE")

    def test_one_unresolvable_fork_does_not_block_other_forks(self):
        client = self.configured_client()
        client.owned_repositories.return_value = [
            {"full_name": "me/a-bad-fork", "fork": True, "default_branch": "main",
             "parent": {"full_name": "missing/upstream"}},
            {"full_name": "me/b-good-fork", "fork": True, "default_branch": "main",
             "parent": {"full_name": "healthy/upstream"}},
        ]

        def sync(repository, branch):
            if repository == "me/a-bad-fork":
                raise GitHubError("upstream not found", 404, '{"message":"Not Found"}')
            return {"merge_type": "fast-forward"}

        client.sync_fork.side_effect = sync
        outcomes = sync_all(client)
        self.assertEqual(
            [(item.repository, item.status) for item in outcomes],
            [("me/a-bad-fork", "UPSTREAM_UNAVAILABLE"), ("me/b-good-fork", "SYNCED")],
        )
        self.assertEqual(client.sync_fork.call_count, 2)

    def test_unexpected_per_fork_error_is_isolated(self):
        client = self.configured_client()
        client.owned_repositories.return_value = [
            {"full_name": "me/a-fork", "fork": True, "default_branch": "main",
             "parent": {"full_name": "upstream/a"}},
            {"full_name": "me/b-fork", "fork": True, "default_branch": "main",
             "parent": {"full_name": "upstream/b"}},
        ]
        client.sync_fork.side_effect = [
            RuntimeError("temporary failure"), {"merge_type": "fast-forward"}
        ]
        outcomes = sync_all(client)
        self.assertEqual([item.status for item in outcomes], ["FAILED", "SYNCED"])

    def test_archived_fork_is_skipped(self):
        client = self.configured_client()
        client.owned_repositories.return_value[0]["archived"] = True
        outcome = sync_all(client)[0]
        self.assertEqual(outcome.status, "SKIPPED")
        client.sync_fork.assert_not_called()

    def test_explicit_repository_exclusion_is_honoured(self):
        client = self.configured_client()
        outcome = sync_all(client, config={"exclude_repositories": ["me/fork"], "exclude_upstreams": []})[0]
        self.assertEqual(outcome.status, "SKIPPED")
        client.sync_fork.assert_not_called()

    def test_upstream_exclusion_is_honoured(self):
        client = self.configured_client()
        outcome = sync_all(client, config={"exclude_repositories": [], "exclude_upstreams": ["upstream/project"]})[0]
        self.assertEqual(outcome.status, "SKIPPED")
        client.sync_fork.assert_not_called()

    def test_missing_parent_metadata_is_attention_item(self):
        client = self.configured_client()
        client.owned_repositories.return_value[0].pop("parent")
        outcome = sync_all(client)[0]
        self.assertEqual(outcome.status, "UPSTREAM_UNAVAILABLE")
        client.sync_fork.assert_not_called()

    def test_missing_default_branch_is_attention_item(self):
        client = self.configured_client()
        client.owned_repositories.return_value[0]["default_branch"] = None
        outcome = sync_all(client)[0]
        self.assertEqual(outcome.status, "BRANCH_MISSING")
        client.sync_fork.assert_not_called()

    def test_multiple_owners_use_their_own_clients(self):
        user = self.configured_client("me/user-fork", "upstream/one")
        org = self.configured_client("Aaqib-OSS/org-fork", "upstream/two")
        org.owned_repositories = Mock()
        org.organization_repositories = Mock(return_value=[
            {"full_name": "Aaqib-OSS/org-fork", "fork": True, "default_branch": "main",
             "parent": {"full_name": "upstream/two"}}
        ])
        outcomes = sync_all(
            user, owners=["user", "org:Aaqib-OSS"],
            client_by_owner={"user": user, "org:Aaqib-OSS": org},
        )
        self.assertEqual(
            [(item.repository, item.status) for item in outcomes],
            [("me/user-fork", "SYNCED"), ("Aaqib-OSS/org-fork", "SYNCED")],
        )
        org.organization_repositories.assert_called_once_with("Aaqib-OSS")
        org.sync_fork.assert_called_once_with("Aaqib-OSS/org-fork", "main")

    def test_organization_discovery_permission_error_is_reported(self):
        client = GitHubClient("token")
        client.organization_repositories = Mock(
            side_effect=GitHubError("organization denied", 403, '{"message":"denied"}')
        )
        outcome = sync_all(client, owners=["org:Aaqib-OSS"])[0]
        self.assertEqual(outcome.status, "ACCESS_RESTRICTED")
        self.assertEqual(outcome.owner_scope, "org:Aaqib-OSS")


class ConfigurationAndReportingTests(unittest.TestCase):
    def test_config_defaults_to_personal_owner(self):
        with patch.dict(os.environ, {"FORKSYNC_CONFIG": "/tmp/nonexistent-forksync-config.json"}, clear=True):
            config = load_config()
        self.assertEqual(config["owners"], ["user"])

    def test_config_parses_personal_and_org_scopes(self):
        with patch.dict(os.environ, {
            "FORKSYNC_CONFIG": "/tmp/nonexistent-forksync-config.json",
            "FORKSYNC_OWNERS": "user,org:Aaqib-OSS",
        }, clear=True):
            config = load_config()
        self.assertEqual(config["owners"], ["user", "org:Aaqib-OSS"])

    def test_invalid_owner_scope_is_rejected(self):
        with patch.dict(os.environ, {
            "FORKSYNC_CONFIG": "/tmp/nonexistent-forksync-config.json",
            "FORKSYNC_OWNERS": "Aaqib-OSS",
        }, clear=True):
            with self.assertRaises(ConfigurationError):
                load_config()

    def test_owner_token_json_is_parsed(self):
        with patch.dict(os.environ, {
            "FORKSYNC_OWNER_TOKENS_JSON": '{"user":"user-token","org:Aaqib-OSS":"org-token"}'
        }, clear=True):
            tokens = load_owner_tokens()
        self.assertEqual(tokens["user"], "user-token")
        self.assertEqual(tokens["org:aaqib-oss"], "org-token")

    def test_invalid_owner_token_json_is_rejected(self):
        with patch.dict(os.environ, {"FORKSYNC_OWNER_TOKENS_JSON": "not-json"}, clear=True):
            with self.assertRaises(ConfigurationError):
                load_owner_tokens()

    def test_report_contains_actionable_attention_items(self):
        report = build_report([
            Outcome("me/fork", "CONFLICT", "Merge conflict", "main", "upstream/repo")
        ], dry_run=False)
        item = report["attention_items"][0]
        self.assertEqual(item["status"], "CONFLICT")
        self.assertTrue(item["requires_attention"])
        self.assertIn("resolve", item["recommended_action"].lower())
        self.assertEqual(item["repository_url"], "https://github.com/me/fork")

    def test_markdown_summary_links_fork_and_upstream(self):
        report = build_report([
            Outcome("me/fork", "CONFLICT", "Merge conflict", "main", "upstream/repo")
        ], dry_run=False)
        markdown = render_markdown_summary(report)
        self.assertIn("[me/fork](https://github.com/me/fork)", markdown)
        self.assertIn("[upstream/repo](https://github.com/upstream/repo)", markdown)
        self.assertIn("Recommended next action", markdown)

    def test_write_report_creates_json_and_step_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            json_path = Path(temp) / "report.json"
            summary_path = Path(temp) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path)}):
                report = write_report([
                    Outcome("me/fork", "UPSTREAM_UNAVAILABLE", "Not visible", "main", "upstream/repo")
                ], False, str(json_path))
            parsed = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["totals"]["requiring_attention"], 1)
            self.assertEqual(report["attention_items"][0]["status"], "UPSTREAM_UNAVAILABLE")
            self.assertIn("Manual attention required", summary_path.read_text(encoding="utf-8"))

    def test_expected_attention_does_not_fail_default_pipeline(self):
        outcomes = [Outcome("me/fork", "CONFLICT", "Merge conflict")]
        with patch.dict(os.environ, {"FORKSYNC_STRICT": "false", "FORKSYNC_FAIL_ON_CONFLICT": "false"}):
            self.assertFalse(should_fail(outcomes))

    def test_unexpected_global_failure_fails_pipeline(self):
        with patch.dict(os.environ, {"FORKSYNC_STRICT": "false"}):
            self.assertTrue(should_fail([Outcome("<global>", "FAILED", "server error")]))

    def test_unexpected_per_fork_failure_does_not_fail_pipeline(self):
        with patch.dict(os.environ, {"FORKSYNC_STRICT": "false"}):
            self.assertFalse(should_fail([Outcome("me/fork", "FAILED", "server error")]))

    def test_strict_mode_fails_on_attention(self):
        with patch.dict(os.environ, {"FORKSYNC_STRICT": "true"}):
            self.assertTrue(should_fail([Outcome("me/fork", "CONFLICT", "Merge conflict")]))

    def test_fail_on_conflict_is_independently_configurable(self):
        with patch.dict(os.environ, {
            "FORKSYNC_STRICT": "false", "FORKSYNC_FAIL_ON_CONFLICT": "true"
        }):
            self.assertTrue(should_fail([Outcome("me/fork", "CONFLICT", "Merge conflict")]))


if __name__ == "__main__":
    unittest.main()
