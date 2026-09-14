from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from review_agent_tools.documentation_configuration import read_configuration
from review_agent_tools.github.source import ReviewPolicySource


class ConfigurationReadTests(unittest.TestCase):
    def test_reads_default_branch_once_then_exact_policy_without_a_model(self):
        github = Mock()
        github.request_json.side_effect = [
            {"id": 42, "full_name": "example/project", "default_branch": "release/docs"},
            {"name": "release/docs", "commit": {"sha": "a" * 40}},
        ]
        content = 'version = 1\n[[area]]\nid = "api"\nsources = ["src/**"]\ndocuments = ["docs/api.md"]\nintent = "API behavior"\n'
        with patch("review_agent_tools.documentation_configuration.read_documentation_policy_at_revision") as read:
            read.return_value = ReviewPolicySource("ok", "a" * 40, content, "b" * 40, "c" * 64)
            result = read_configuration(github, repository="example/project", provider_repository_id=42)
        self.assertEqual(result.state, "valid")
        self.assertEqual(result.revision, "a" * 40)
        self.assertEqual(result.policy.areas[0].documents, ("docs/api.md",))
        self.assertIn("release%2Fdocs", github.request_json.call_args_list[1].args[0])
        read.assert_called_once_with(github, repository="example/project", revision="a" * 40)
        self.assertEqual(github.request_json.call_count, 2)

    def test_rejects_repository_mismatch_before_reading_policy(self):
        github = Mock()
        github.request_json.return_value = {"id": 43, "full_name": "example/project", "default_branch": "main"}
        result = read_configuration(github, repository="example/project", provider_repository_id=42)
        self.assertEqual(result.state, "unavailable")
        self.assertIsNone(result.policy)
        self.assertEqual(github.request_json.call_count, 1)

    def test_invalid_configuration_is_displayed_without_activating_rules(self):
        github = Mock()
        github.request_json.side_effect = [
            {"id": 42, "full_name": "example/project", "default_branch": "main"},
            {"name": "main", "commit": {"sha": "a" * 40}},
        ]
        with patch("review_agent_tools.documentation_configuration.read_documentation_policy_at_revision") as read:
            read.return_value = ReviewPolicySource("ok", "a" * 40, "version = 7", "b" * 40, "c" * 64)
            result = read_configuration(github, repository="example/project", provider_repository_id=42)
        self.assertEqual(result.state, "invalid")
        self.assertIsNotNone(result.problem)
        self.assertIsNone(result.policy)


import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import psycopg

from tests import test_admin_api, test_postgres_github_app
from review_agent_tools.documentation_configuration import DocumentationConfiguration
from review_agent_tools.postgres import github_app, documentation_configuration

DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


@unittest.skipUnless(DSN, "requires isolated PostgreSQL database")
class ConfigurationAPITests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_admin_api.AdminAPITests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.login()
        self.client = self.fixture.client
        installation = test_postgres_github_app.PostgreSQLGitHubAppTests("runTest").installation()
        with psycopg.connect(DSN) as connection, connection.transaction():
            access = github_app.grant_repository_access(
                connection, installation_id=installation.id, provider_repository_id=9001,
                full_name="CCimen/review-agent", actor="test", reason="test selection",
            )
            github_app.enable_repository(
                connection, repository_id=access.repository_id, profile_key="default-standard",
                trigger_mode=github_app.TriggerMode.MANUAL, actor="test", reason="test approval",
            )
        self.repository_id = int(access.repository_id)
        self.url = f"/api/repositories/{self.repository_id}/documentation"

    def test_refresh_snapshot_is_bounded_display_state_and_older_refresh_cannot_replace_it(self):
        snapshot = DocumentationConfiguration(
            "not_configured", datetime.now(timezone.utc), default_branch="main", revision="a" * 40,
        )
        with patch("review_agent_tools.operator_setup.github_app_authenticator") as authenticator, patch(
            "review_agent_tools.documentation_configuration.read_configuration", return_value=snapshot,
        ):
            response = self.client.post(self.url + "/refresh")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["configuration"], "not_configured")
        self.assertEqual(data["configuration_detail"]["revision"], "a" * 40)
        self.assertEqual(data["capability"], "missing_checks")
        self.assertEqual(data["resolved"]["effective_mode"], "off")
        authenticator.return_value.installation_token.assert_called_once_with(
            7001, repository_ids=(9001,), permissions={"contents": "read"},
        )
        self.assertEqual(self.client.get(self.url).json()["configuration_detail"], data["configuration_detail"])
        with psycopg.connect(DSN) as connection, connection.transaction():
            retained = documentation_configuration.save(
                connection, repository_id=self.repository_id,
                snapshot=replace(snapshot, revision="b" * 40),
                refresh_started_at=snapshot.read_at - timedelta(minutes=1),
            )
        self.assertEqual(retained.revision, "a" * 40)

    def test_policy_change_during_network_read_discards_snapshot(self):
        def changed(*args, **kwargs):
            with psycopg.connect(DSN) as connection, connection.transaction():
                connection.execute(
                    "UPDATE review_agent.repositories SET documentation_revision = documentation_revision + 1 WHERE id = %s",
                    (self.repository_id,),
                )
            return DocumentationConfiguration("not_configured", datetime.now(timezone.utc))
        with patch("review_agent_tools.operator_setup.github_app_authenticator"), patch(
            "review_agent_tools.documentation_configuration.read_configuration", side_effect=changed,
        ):
            response = self.client.post(self.url + "/refresh")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNone(self.client.get(self.url).json()["configuration_detail"])
