from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PARENT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGIN_PARENT))

from review_agent_tools import operator_application, operator_setup  # noqa: E402
from review_agent_tools.github import app_auth  # noqa: E402
from review_agent_tools.github.gateway import (  # noqa: E402
    GitHubGatewayRejected,
    GitHubGatewayRetryable,
    OperatorAppStatus,
    OperatorSmokeResult,
)
from review_agent_tools.postgres import github_app, jobs, publications  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLUnavailable  # noqa: E402
from review_agent_tools.domain.review import RepositoryId  # noqa: E402


PINNED_HERMES_IMAGE = (
    "nousresearch/hermes-agent:v2026.8.3@"
    "sha256:16788311e2fa3035456bdc1bafb8ec2b1777db64ebf020af9bb7eb73c3712c9e"
)


def _private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _load_admin_cli():
    path = ROOT / "tools" / "review_agent_admin.py"
    spec = importlib.util.spec_from_file_location("review_agent_admin", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load review-agent-admin")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OperatorSetupTests(unittest.TestCase):
    def test_capabilities_state_the_shipped_app_only_contract(self) -> None:
        self.assertEqual(
            operator_setup.capabilities().to_json_obj(),
            {
                "advisory_only": True,
                "authentication": "github-app",
                "feedback": True,
                "fork_pull_requests": False,
                "repository_profiles": "deployment-profile-only",
                "selected_repositories_only": True,
                "trigger_mode": "manual",
            },
        )

    def test_registration_url_prefills_only_required_app_contract(self) -> None:
        personal = operator_setup.github_app_registration_url(
            owner="CCimen",
            owner_type="user",
            public_url="https://reviews.example.test/",
            homepage_url="https://docs.example.test/review-agent/",
        )
        personal_url = urlsplit(personal)
        self.assertEqual(personal_url.netloc, "github.com")
        self.assertEqual(personal_url.path, "/settings/apps/new")
        self.assertEqual(
            parse_qs(personal_url.query),
            {
                "events[]": ["issue_comment"],
                "name": ["review-agent-ccimen"],
                "permissions[contents]": ["read"],
                "permissions[issues]": ["write"],
                "permissions[pull_requests]": ["write"],
                "public": ["false"],
                "request_oauth_on_install": ["false"],
                "url": ["https://docs.example.test/review-agent/"],
                "webhook_active": ["true"],
                "webhook_url": [
                    "https://reviews.example.test/webhooks/github-app"
                ],
            },
        )

        organization = operator_setup.github_app_registration_url(
            owner="Example Org",
            owner_type="organization",
            public_url="https://reviews.example.test",
            homepage_url="https://github.com/example/review-agent",
            app_name="Example Review Agent",
        )
        organization_url = urlsplit(organization)
        self.assertEqual(
            organization_url.path,
            "/organizations/Example%20Org/settings/apps/new",
        )
        self.assertEqual(
            parse_qs(organization_url.query)["name"], ["Example Review Agent"]
        )

    def test_registration_url_rejects_insecure_or_ambiguous_urls(self) -> None:
        invalid_urls = (
            ("http://reviews.example.test", "https://docs.example.test"),
            ("https://reviews.example.test", "https://user:pw@docs.example.test"),
            ("https://reviews.example.test", "https://docs.example.test/?x=1"),
        )
        for public_url, homepage_url in invalid_urls:
            with self.subTest(public_url=public_url, homepage_url=homepage_url):
                with self.assertRaises(ValueError):
                    operator_setup.github_app_registration_url(
                        owner="example",
                        owner_type="user",
                        public_url=public_url,
                        homepage_url=homepage_url,
                    )

    def test_preflight_is_local_deterministic_and_secret_safe(self) -> None:
        webhook_secret = "do-not-print-webhook-secret"
        database_password = "do-not-print-database-password"
        private_key = _private_key()
        with tempfile.TemporaryDirectory() as temp:
            key_path = Path(temp) / "github-app.pem"
            key_path.write_text(private_key, encoding="utf-8")
            key_path.chmod(0o400)
            report = operator_setup.preflight(
                {
                    "REVIEW_AGENT_DATABASE_URL": (
                        "postgresql://review_agent:"
                        f"{database_password}@review-postgres:5432/review_agent"
                    ),
                    "REVIEW_AGENT_GITHUB_APP_ID": "123456",
                    "REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE": str(key_path),
                    "REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET": webhook_secret,
                    "API_SERVER_KEY": "configured-internal-api-key",
                    "REVIEW_AGENT_HERMES_IMAGE": PINNED_HERMES_IMAGE,
                    "REVIEW_AGENT_PROFILE": "sundsvall-standard",
                },
            )

        payload = report.to_json_obj()
        rendered = json.dumps(payload, sort_keys=True)
        self.assertTrue(report.ready, payload)
        self.assertNotIn(webhook_secret, rendered)
        self.assertNotIn(database_password, rendered)
        self.assertNotIn("PRIVATE KEY", rendered)
        self.assertEqual(
            {check.name: check.status for check in report.checks},
            {
                "database_configuration": "ready",
                "github_app_configuration": "ready",
                "profile_contract": "ready",
                "webhook_configuration": "ready",
                "internal_api_configuration": "ready",
            },
        )

    def test_preflight_reports_all_repairable_configuration_failures(self) -> None:
        report = operator_setup.preflight(
            {
                "REVIEW_AGENT_DATABASE_URL": "not-a-postgres-url",
                "REVIEW_AGENT_GITHUB_APP_ID": "zero",
                "REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE": "/missing/key.pem",
                "REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET": "replace-me",
                "API_SERVER_KEY": "change-me",
                "REVIEW_AGENT_HERMES_IMAGE": "latest",
                "REVIEW_AGENT_PROFILE": "not valid",
            },
            bootstrap_source=ROOT / "bootstrap",
        )

        self.assertFalse(report.ready)
        self.assertEqual(len(report.checks), 5)
        self.assertTrue(all(check.status == "error" for check in report.checks))
        rendered = json.dumps(report.to_json_obj(), sort_keys=True)
        self.assertNotIn("replace-me", rendered)
        self.assertNotIn("/missing/key.pem", rendered)

    def test_doctor_uses_read_only_contract_and_returns_secret_safe_status(self) -> None:
        snapshot = operator_application.DeploymentHealth(
            github_app=github_app.GitHubAppAccessHealth(
                active_installations=1,
                invalid_active_installations=0,
                repositories=1,
                enabled_repositories=1,
            ),
            review_queue=jobs.ReviewQueueHealth(
                active=2,
                queued=1,
                leased=1,
                expired_leases=0,
                dead_letters=2,
            ),
            publication_queue=publications.PublicationQueueHealth(
                pending=1,
                posting=0,
                expired_recoverable=0,
                expired_exhausted=0,
            ),
        )
        runtime = Mock()
        gateway = Mock()
        gateway.operator_status.return_value = OperatorAppStatus(
            provider_app_id=123456,
            slug="review-agent-ccimen",
            owner="CCimen",
            permissions=(
                ("contents", "read"),
                ("issues", "write"),
                ("metadata", "read"),
                ("pull_requests", "write"),
            ),
            events=("issue_comment",),
        )
        environment = {
            "REVIEW_AGENT_ACTIVE_JOB_LIMIT": "100",
            "REVIEW_AGENT_GITHUB_APP_ID": "123456",
            "REVIEW_AGENT_PROFILE": "sundsvall-standard",
        }
        with (
            patch.object(
                operator_setup.operator_application,
                "deployment_health",
                return_value=snapshot,
            ),
            patch.object(operator_setup.review_contract, "load_installed_contract"),
        ):
            report = operator_setup.doctor(
                environment,
                runtime=runtime,
                gateway=gateway,
                hermes_probe=lambda: True,
                hermes_home=Path("/non-secret-test-home"),
            )

        self.assertTrue(report.ready, report.to_json_obj())
        runtime.readiness.assert_called_once_with()
        gateway.operator_status.assert_called_once_with()
        rendered = json.dumps(report.to_json_obj(), sort_keys=True)
        self.assertNotIn("123456", rendered)
        self.assertEqual(
            {check.name for check in report.checks},
            {
                "database",
                "github_app",
                "hermes",
                "installations",
                "installed_profile",
                "queues",
                "repositories",
            },
        )

    def test_smoke_test_checks_capacity_then_uses_only_gateway_dry_run(self) -> None:
        snapshot = operator_application.DeploymentHealth(
            github_app=github_app.GitHubAppAccessHealth(
                active_installations=0,
                invalid_active_installations=0,
                repositories=0,
                enabled_repositories=0,
            ),
            review_queue=jobs.ReviewQueueHealth(
                active=3,
                queued=2,
                leased=1,
                expired_leases=0,
                dead_letters=2,
            ),
            publication_queue=publications.PublicationQueueHealth(
                pending=0,
                posting=0,
                expired_recoverable=0,
                expired_exhausted=0,
            ),
        )
        gateway = Mock()
        gateway.operator_smoke.return_value = OperatorSmokeResult(
            repository_id=9001,
            repository="CCimen/review-agent",
            pr_number=42,
            base_sha="b" * 40,
            head_sha="a" * 40,
            publication_permission=True,
        )
        with patch.object(
            operator_setup.operator_application,
            "deployment_health",
            return_value=snapshot,
        ):
            report = operator_setup.smoke_test(
                {"REVIEW_AGENT_ACTIVE_JOB_LIMIT": "100"},
                runtime=Mock(),
                gateway=gateway,
                repository="CCimen/review-agent",
                pr_number=42,
            )

        gateway.operator_smoke.assert_called_once_with(
            repository="CCimen/review-agent",
            pr_number=42,
        )
        self.assertEqual(
            report.to_json_obj(),
            {
                "active_job_limit": 100,
                "active_jobs": 3,
                "base_sha": "b" * 40,
                "dry_run": True,
                "head_sha": "a" * 40,
                "pr_number": 42,
                "publication_permission": True,
                "repository": "CCimen/review-agent",
                "repository_id": 9001,
            },
        )

    def test_queue_readiness_still_rejects_work_that_needs_recovery(self) -> None:
        blocking_states = (
            (100, 0, 0, operator_setup.OperatorCapacityUnavailable),
            (0, 1, 0, ValueError),
            (0, 0, 1, ValueError),
        )

        for active, expired_leases, expired_publications, error in blocking_states:
            with self.subTest(
                active=active,
                expired_leases=expired_leases,
                expired_publications=expired_publications,
            ):
                snapshot = operator_application.DeploymentHealth(
                    github_app=github_app.GitHubAppAccessHealth(
                        active_installations=1,
                        invalid_active_installations=0,
                        repositories=1,
                        enabled_repositories=1,
                    ),
                    review_queue=jobs.ReviewQueueHealth(
                        active=active,
                        queued=0,
                        leased=0,
                        expired_leases=expired_leases,
                        dead_letters=2,
                    ),
                    publication_queue=publications.PublicationQueueHealth(
                        pending=0,
                        posting=0,
                        expired_recoverable=expired_publications,
                        expired_exhausted=0,
                    ),
                )
                gateway = Mock()

                with (
                    patch.object(
                        operator_setup.operator_application,
                        "deployment_health",
                        return_value=snapshot,
                    ),
                    self.assertRaises(error),
                ):
                    operator_setup.smoke_test(
                        {"REVIEW_AGENT_ACTIVE_JOB_LIMIT": "100"},
                        runtime=Mock(),
                        gateway=gateway,
                        repository="CCimen/review-agent",
                        pr_number=42,
                    )

                gateway.operator_smoke.assert_not_called()


class OperatorAdminCliTests(unittest.TestCase):
    def test_capabilities_command_emits_one_machine_readable_document(self) -> None:
        admin = _load_admin_cli()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = admin.main(["capabilities"])

        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            operator_setup.capabilities().to_json_obj(),
        )

    def test_registration_url_reports_bounded_input_failure(self) -> None:
        admin = _load_admin_cli()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = admin.main(
                [
                    "github-app",
                    "registration-url",
                    "--owner",
                    "example",
                    "--owner-type",
                    "user",
                    "--public-url",
                    "https://review.example.test",
                    "--homepage-url",
                    "http://docs.example.test",
                ]
            )

        self.assertEqual(status, 1)
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {
                "error": {
                    "code": "invalid_registration_input",
                    "retryable": False,
                }
            },
        )

    def test_inventory_commands_emit_bounded_stable_json(self) -> None:
        admin = _load_admin_cli()
        runtime = Mock()
        installation = github_app.GitHubAppInstallation(
            id=github_app.GitHubAppInstallationId(11),
            provider_installation_id=7001,
            account_id=8001,
            account_login="CCimen",
            account_type=github_app.AccountType.USER,
            repository_selection=github_app.RepositorySelection.SELECTED,
            status=github_app.InstallationStatus.ACTIVE,
            contents_permission=github_app.PermissionLevel.READ,
            issues_permission=github_app.PermissionLevel.WRITE,
            pull_requests_permission=github_app.PermissionLevel.WRITE,
            created_at=datetime(2026, 8, 27, 4, tzinfo=timezone.utc),
            updated_at=datetime(2026, 8, 27, 4, tzinfo=timezone.utc),
            suspended_at=None,
            deleted_at=None,
        )
        repository = github_app.RepositoryAccessState(
            repository_id=RepositoryId(22),
            installation_id=installation.id,
            provider_repository_id=9001,
            full_name="CCimen/review-agent",
            access_state=github_app.RepositoryAccess.AVAILABLE,
            enabled=True,
            trigger_mode=github_app.TriggerMode.MANUAL,
            profile_key="sundsvall-standard",
            enabled_at=datetime(2026, 8, 27, 4, 1, tzinfo=timezone.utc),
            disabled_at=None,
            updated_by="operator:ccimen",
            update_reason="approved pilot",
            updated_at=datetime(2026, 8, 27, 4, 1, tzinfo=timezone.utc),
        )
        with (
            patch.object(admin, "_runtime", return_value=runtime),
            patch.object(
                admin.operator_application,
                "list_github_app_installations",
                return_value=(installation,),
            ),
            patch.object(
                admin.operator_application,
                "list_github_app_repositories",
                return_value=(repository,),
            ),
        ):
            installation_stdout = io.StringIO()
            with redirect_stdout(installation_stdout):
                installation_status = admin.main(
                    ["installations", "list"]
                )
            repository_stdout = io.StringIO()
            with redirect_stdout(repository_stdout):
                repository_status = admin.main(["repositories", "list"])

        self.assertEqual(installation_status, 0)
        self.assertEqual(repository_status, 0)
        self.assertEqual(runtime.close.call_count, 2)
        self.assertEqual(
            json.loads(installation_stdout.getvalue()),
            {
                "installations": [
                    {
                        "account": "CCimen",
                        "account_type": "user",
                        "contents_permission": "read",
                        "installation_id": 7001,
                        "issues_permission": "write",
                        "pull_requests_permission": "write",
                        "repository_selection": "selected",
                        "status": "active",
                    }
                ],
                "next_after_id": None,
            },
        )
        self.assertEqual(
            json.loads(repository_stdout.getvalue()),
            {
                "repositories": [
                    {
                        "access": "available",
                        "enabled": True,
                        "profile": "sundsvall-standard",
                        "repository": "CCimen/review-agent",
                        "repository_id": 9001,
                        "trigger_mode": "manual",
                    }
                ],
                "next_after_id": None,
            },
        )

    def test_repository_enablement_requires_audited_identity_and_returns_state(self) -> None:
        admin = _load_admin_cli()
        runtime = Mock()
        access = Mock(
            access_state=github_app.RepositoryAccess.AVAILABLE,
            enabled=True,
            full_name="CCimen/review-agent",
            profile_key="sundsvall-standard",
            provider_repository_id=9001,
            trigger_mode=github_app.TriggerMode.MANUAL,
        )
        stdout = io.StringIO()
        with (
            patch.object(admin, "_runtime", return_value=runtime),
            patch.object(
                admin.operator_application,
                "enable_github_app_repository",
                return_value=access,
            ) as enable,
            redirect_stdout(stdout),
        ):
            status = admin.main(
                [
                    "repositories",
                    "enable",
                    "9001",
                    "--profile",
                    "sundsvall-standard",
                    "--actor",
                    "operator:ccimen",
                    "--reason",
                    "approved pilot",
                ]
            )

        self.assertEqual(status, 0)
        enable.assert_called_once_with(
            runtime,
            provider_repository_id=9001,
            profile="sundsvall-standard",
            actor="operator:ccimen",
            reason="approved pilot",
        )
        runtime.close.assert_called_once()
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "access": "available",
                "enabled": True,
                "profile": "sundsvall-standard",
                "repository": "CCimen/review-agent",
                "repository_id": 9001,
                "trigger_mode": "manual",
            },
        )

    def test_github_app_onboard_reconciles_and_enables_by_repository_name(self) -> None:
        admin = _load_admin_cli()
        runtime = Mock()
        access = Mock(
            access_state=github_app.RepositoryAccess.AVAILABLE,
            enabled=True,
            full_name="CCimen/review-agent",
            profile_key="sundsvall-standard",
            provider_repository_id=9001,
            trigger_mode=github_app.TriggerMode.MANUAL,
        )
        reconciliation = Mock(
            installation=Mock(provider_installation_id=7001),
            repositories_enabled=0,
            repositories_removed=0,
            repositories_seen=1,
        )
        result = Mock(access=access, reconciliation=reconciliation)
        stdout = io.StringIO()
        with (
            patch.object(admin, "_runtime", return_value=runtime),
            patch.object(
                admin.operator_setup,
                "github_app_authenticator",
                return_value="authenticator",
            ),
            patch.object(
                admin.operator_application,
                "onboard_github_app_repository",
                return_value=result,
            ) as onboard,
            patch.dict(os.environ, {"REVIEW_AGENT_PROFILE": "sundsvall-standard"}),
            redirect_stdout(stdout),
        ):
            status = admin.main(
                [
                    "github-app",
                    "onboard",
                    "CCimen/review-agent",
                    "--actor",
                    "github:CCimen",
                ]
            )

        self.assertEqual(status, 0)
        onboard.assert_called_once_with(
            runtime,
            "authenticator",
            repository="CCimen/review-agent",
            profile="sundsvall-standard",
            actor="github:CCimen",
            reason="approved repository onboarding",
        )
        runtime.close.assert_called_once()
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "access": "available",
                "enabled": True,
                "profile": "sundsvall-standard",
                "repositories_removed": 0,
                "repositories_seen": 1,
                "installation_id": 7001,
                "repository": "CCimen/review-agent",
                "repository_id": 9001,
                "trigger_mode": "manual",
            },
        )

    def test_github_app_onboard_reports_bounded_failure_classes(self) -> None:
        admin = _load_admin_cli()
        arguments = [
            "github-app",
            "onboard",
            "CCimen/review-agent",
            "--actor",
            "github:CCimen",
        ]
        cases = (
            (
                app_auth.GitHubAppTokenRetryable("provider-secret"),
                os.EX_TEMPFAIL,
                "repository_onboarding_unavailable",
                True,
            ),
            (
                app_auth.GitHubAppTokenPermanent("provider-secret"),
                1,
                "repository_onboarding_failed",
                False,
            ),
        )
        for failure, expected_status, expected_code, retryable in cases:
            with self.subTest(expected_code=expected_code):
                runtime = Mock()
                stderr = io.StringIO()
                with (
                    patch.object(admin, "_runtime", return_value=runtime),
                    patch.object(
                        admin.operator_setup,
                        "github_app_authenticator",
                        return_value="authenticator",
                    ),
                    patch.object(
                        admin.operator_application,
                        "onboard_github_app_repository",
                        side_effect=failure,
                    ),
                    redirect_stderr(stderr),
                ):
                    status = admin.main(arguments)

                self.assertEqual(status, expected_status)
                runtime.close.assert_called_once()
                error = json.loads(stderr.getvalue())["error"]
                self.assertEqual(error["code"], expected_code)
                self.assertEqual(error["retryable"], retryable)
                self.assertNotIn("provider-secret", stderr.getvalue())

        stderr = io.StringIO()
        with (
            patch.object(
                admin,
                "_runtime",
                side_effect=PostgreSQLUnavailable("host=secret.internal"),
            ),
            redirect_stderr(stderr),
        ):
            status = admin.main(arguments)

        self.assertEqual(status, os.EX_TEMPFAIL)
        self.assertEqual(
            json.loads(stderr.getvalue())["error"],
            {"code": "database_unavailable", "retryable": True},
        )
        self.assertNotIn("secret.internal", stderr.getvalue())

    def test_smoke_command_preserves_retryable_and_permanent_failures(self) -> None:
        admin = _load_admin_cli()
        cases = (
            (
                GitHubGatewayRetryable("provider-secret"),
                os.EX_TEMPFAIL,
                "smoke_test_unavailable",
                True,
            ),
            (
                GitHubGatewayRejected("provider-secret"),
                1,
                "smoke_test_failed",
                False,
            ),
            (
                operator_setup.OperatorCapacityUnavailable("capacity-secret"),
                os.EX_TEMPFAIL,
                "smoke_test_unavailable",
                True,
            ),
        )
        for failure, expected_status, expected_code, retryable in cases:
            with self.subTest(expected_code=expected_code):
                runtime = Mock()
                stderr = io.StringIO()
                with (
                    patch.object(admin, "_runtime", return_value=runtime),
                    patch.object(admin, "_operator_client", return_value=Mock()),
                    patch.object(
                        admin.operator_setup,
                        "smoke_test",
                        side_effect=failure,
                    ),
                    redirect_stderr(stderr),
                ):
                    status = admin.main(
                        [
                            "smoke-test",
                            "--dry-run",
                            "--repository",
                            "CCimen/review-agent",
                            "--pr",
                            "42",
                        ]
                    )

                self.assertEqual(status, expected_status)
                runtime.close.assert_called_once_with()
                self.assertEqual(
                    json.loads(stderr.getvalue()),
                    {
                        "error": {
                            "code": expected_code,
                            "retryable": retryable,
                        }
                    },
                )
                self.assertNotIn("secret", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
