from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import sys
from typing import cast
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools import (  # noqa: E402
    github_webhook,
    review_contract,
    review_run_application,
)
from review_agent_tools.github import app_auth, app_processor  # noqa: E402
from review_agent_tools.github.app_auth import (  # noqa: E402
    InstallationToken,
    GitHubAppTokenService,
)
from review_agent_tools.github.gateway import (  # noqa: E402
    GitHubGatewayProtocolError,
    GitHubGatewayRejected,
    GitHubGatewayRetryable,
    ReviewGitHubGateway,
)
from review_agent_tools.github.gateway_client import (  # noqa: E402
    ReviewGitHubGatewayClient,
)
from review_agent_tools.github.publication import GitHubIssueCommentGateway  # noqa: E402
from review_agent_tools.postgres import (  # noqa: E402
    github_app,
    jobs,
    registry,
    webhook_deliveries,
)
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402
from review_agent_tools.source_control import (  # noqa: E402
    GitHubReadClient,
    GitHubReadError,
)


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


class _Tokens:
    def __init__(self) -> None:
        self.token_requests: list[tuple[int, str]] = []
        self.invalidated_repository_ids: list[int] = []
        self.repository_verifications: list[tuple[int, int, str]] = []

    def token_for(
        self, provider_repository_id: int, *, purpose: str = "review_read"
    ) -> InstallationToken:
        self.token_requests.append((provider_repository_id, purpose))
        return InstallationToken(
            "installation-token",
            datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def invalidate(
        self, provider_repository_id: int, *, purpose: str = "review_read"
    ) -> None:
        del purpose
        self.invalidated_repository_ids.append(provider_repository_id)

    def app_bot_login(self) -> str:
        return "review-agent[bot]"

    def verify_installation_repository(
        self,
        provider_installation_id: int,
        provider_repository_id: int,
        expected_full_name: str,
    ) -> app_auth.VerifiedInstallationRepository:
        self.repository_verifications.append(
            (
                provider_installation_id,
                provider_repository_id,
                expected_full_name,
            )
        )
        return app_auth.VerifiedInstallationRepository(
            provider_repository_id=provider_repository_id,
            full_name=expected_full_name,
        )


class _FeedbackGitHub:
    def __init__(self) -> None:
        self.reactions: list[tuple[str, int, str]] = []
        self.comments: list[tuple[str, int, str]] = []

    def create_issue_comment_reaction(
        self, repository: str, comment_id: int, content: str
    ) -> bool:
        self.reactions.append((repository, comment_id, content))
        return True

    def list_issue_comments(
        self,
        repository: str,
        issue_number: int,
        *,
        max_pages: int = 3,
        newest_first: bool = False,
    ) -> list[object]:
        del repository, issue_number, max_pages, newest_first
        return []

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> object:
        self.comments.append((repository, issue_number, body))
        return object()


class _GitHub(GitHubReadClient):
    def __init__(
        self,
        *,
        permission_user_id: int = 5001,
        permission_login: str = "ccimen",
        permission: str = "write",
        head_repository_id: int | None = 9001,
        before_pull: object | None = None,
        request_error: GitHubReadError | None = None,
    ) -> None:
        super().__init__("unused")
        self.permission_user_id = permission_user_id
        self.permission_login = permission_login
        self.permission = permission
        self.head_repository_id = head_repository_id
        self.before_pull = before_pull
        self.request_error = request_error
        self.endpoints: list[str] = []

    def request_json(self, endpoint: str, *, max_bytes: int = 2_000_000) -> object:
        self.endpoints.append(endpoint)
        if self.request_error is not None:
            raise self.request_error
        if endpoint.endswith("/permission"):
            return {
                "permission": self.permission,
                "user": {
                    "id": self.permission_user_id,
                    "login": self.permission_login,
                },
            }
        if callable(self.before_pull):
            self.before_pull()
        head_repository = (
            None
            if self.head_repository_id is None
            else {"id": self.head_repository_id, "full_name": "CCimen/review-agent"}
        )
        return {
            "number": 42,
            "state": "open",
            "base": {
                "sha": "b" * 40,
                "repo": {"id": 9001, "full_name": "CCimen/review-agent"},
            },
            "head": {"sha": "a" * 40, "repo": head_repository},
        }


class _RejectedGateway:
    def authorize_review_delivery(self, **_values: object) -> object:
        raise GitHubGatewayRejected("delivery_lease_lost")


class _ProtocolFailureGateway:
    def authorize_review_delivery(self, **_values: object) -> object:
        raise GitHubGatewayProtocolError("invalid response")


class _FeedbackAuthorizationProtocolFailureGateway:
    def authorize_feedback_delivery(self, **_values: object) -> object:
        raise GitHubGatewayProtocolError("invalid response")


class _AcknowledgementFailureGateway:
    def __init__(self, delegate: ReviewGitHubGateway) -> None:
        self._delegate = delegate

    def authorize_review_delivery(self, **values: object) -> object:
        return self._delegate.authorize_review_delivery(**values)

    def acknowledge_review(self, **_values: object) -> bool:
        raise GitHubGatewayRetryable("github_unreachable")


class _FeedbackAcknowledgementProtocolFailureGateway:
    def __init__(self, delegate: ReviewGitHubGateway) -> None:
        self._delegate = delegate

    def authorize_feedback_delivery(self, **values: object) -> object:
        return self._delegate.authorize_feedback_delivery(**values)

    def acknowledge_feedback(self, **_values: object) -> bool:
        raise GitHubGatewayProtocolError("invalid response")


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class GitHubAppProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
        self.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.runtime.open()
        self.addCleanup(self.runtime.close)
        self.tokens = _Tokens()
        self.feedback_github = _FeedbackGitHub()
        self.contract = review_contract.ReviewContract(
            profile="default-standard",
            hermes_image="hermes@test",
            model_provider="openai-codex",
            model="gpt-test",
            reasoning_effort="high",
            plugin_result_max_chars=160_000,
            profile_bundle_sha256="1" * 64,
            managed_config_sha256="2" * 64,
            engine_bundle_sha256="3" * 64,
            sha256="4" * 64,
        )

    def processor(
        self,
        github: _GitHub | None = None,
        gateway: object | None = None,
        *,
        acknowledgement_failure: bool = False,
        feedback_acknowledgement_protocol_failure: bool = False,
    ) -> app_processor.GitHubAppProcessor:
        client = github or _GitHub()
        gateway_service = ReviewGitHubGateway(
            postgres=self.runtime,
            tokens=cast(GitHubAppTokenService, self.tokens),
            profile="default-standard",
            github_factory=lambda _: client,
            feedback_factory=lambda _: cast(
                GitHubIssueCommentGateway, self.feedback_github
            ),
        )
        selected_gateway: object = gateway if gateway is not None else gateway_service
        if acknowledgement_failure:
            selected_gateway = _AcknowledgementFailureGateway(gateway_service)
        if feedback_acknowledgement_protocol_failure:
            selected_gateway = _FeedbackAcknowledgementProtocolFailureGateway(
                gateway_service
            )
        return app_processor.GitHubAppProcessor(
            postgres=self.runtime,
            gateway=cast(
                ReviewGitHubGatewayClient,
                selected_gateway,
            ),
            config=app_processor.ProcessorConfig(
                profile="default-standard",
                policy_revision="policy-v1",
                job_priority=0,
                job_max_attempts=3,
                active_job_limit=100,
                contract_environment={},
                retry_delay=timedelta(0),
            ),
        )

    def register(self, event: str, payload: object) -> int:
        normalized = github_webhook.normalize_event(event, payload)
        if normalized.event == "installation":
            category = webhook_deliveries.CommandCategory.INSTALLATION
        elif normalized.event == "installation_repositories":
            category = webhook_deliveries.CommandCategory.REPOSITORY_ACCESS
        elif normalized.command_kind is github_webhook.CommandKind.REVIEW:
            category = webhook_deliveries.CommandCategory.REVIEW
        elif normalized.command_kind in {
            github_webhook.CommandKind.FINDING_FEEDBACK,
            github_webhook.CommandKind.QUALITY_FEEDBACK,
            github_webhook.CommandKind.INVALID,
        }:
            category = webhook_deliveries.CommandCategory.FEEDBACK
        else:
            category = webhook_deliveries.CommandCategory.IGNORED
        with self.runtime.transaction() as connection:
            registered = webhook_deliveries.register_delivery(
                connection,
                definition=webhook_deliveries.DeliveryDefinition(
                    delivery_guid=str(uuid4()),
                    event=normalized.event,
                    action=normalized.action,
                    payload_sha256=hashlib.sha256(repr(payload).encode()).hexdigest(),
                    provider_installation_id=normalized.provider_installation_id,
                    provider_repository_id=normalized.provider_repository_id,
                    repository_full_name=normalized.repository,
                    command_category=category,
                    normalized_schema_version=normalized.schema_version,
                    normalized_payload=normalized.normalized,
                ),
                max_attempts=3,
            )
        return registered.delivery.id

    @staticmethod
    def installation_payload(*, selection: str = "selected") -> dict[str, object]:
        payload: dict[str, object] = {
            "action": "created",
            "installation": {
                "id": 7001,
                "account": {"id": 8001, "login": "CCimen", "type": "User"},
                "repository_selection": selection,
                "permissions": {
                    "contents": "read",
                    "issues": "write",
                    "pull_requests": "write",
                },
            },
        }
        if selection == "selected":
            payload["repositories"] = [{"id": 9001, "full_name": "CCimen/review-agent"}]
        return payload

    @staticmethod
    def review_payload() -> dict[str, object]:
        return {
            "action": "created",
            "installation": {
                "id": 7001,
                "repository_selection": "selected",
            },
            "repository": {"id": 9001, "full_name": "CCimen/review-agent"},
            "issue": {"number": 42, "pull_request": {"url": "pr"}},
            "comment": {
                "id": 6001,
                "body": "/review",
                "author_association": "MEMBER",
            },
            "sender": {"id": 5001, "login": "ccimen", "type": "User"},
        }

    def enable_repository(self) -> None:
        installation = self.register("installation", self.installation_payload())
        result = self.processor().process_next(lease_owner="worker-setup")
        self.assertEqual(result.delivery_id if result else None, installation)
        with self.runtime.transaction() as connection:
            authorization = github_app.authorize_review_read
            access = connection.execute(
                "SELECT repository_id FROM review_agent.github_app_repository_access"
            ).fetchone()
            assert access is not None
            github_app.enable_repository(
                connection,
                repository_id=access[0],
                profile_key="default-standard",
                trigger_mode=github_app.TriggerMode.MANUAL,
                actor="operator:test",
                reason="approve pilot",
            )
            authorization(connection, 9001)

    def test_installation_created_grants_selected_repositories_disabled(self) -> None:
        payload = self.installation_payload()
        repositories = payload["repositories"]
        assert isinstance(repositories, list)
        repositories.append({"id": 9002, "full_name": "CCimen/second-repository"})
        delivery_id = self.register("installation", payload)

        result = self.processor().process_next(lease_owner="worker-1")

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "accepted")
        with self.runtime.transaction() as connection:
            installation = github_app.get_installation_by_provider_id(connection, 7001)
            access = connection.execute(
                "SELECT repository_id, enabled, trigger_mode "
                "FROM review_agent.github_app_repository_access "
                "ORDER BY repository_id"
            ).fetchall()
            delivery = webhook_deliveries.get_delivery(connection, delivery_id)
        self.assertEqual(installation.repository_selection, "selected")
        self.assertEqual(len(access), 2)
        self.assertTrue(all(not row[1] for row in access))
        self.assertEqual({row[2] for row in access}, {"automatic"})
        self.assertIsNone(delivery.normalized_payload)

    def test_installation_created_accepts_no_selected_repositories(self) -> None:
        payload = self.installation_payload()
        payload["repositories"] = []
        delivery_id = self.register("installation", payload)

        result = self.processor().process_next(lease_owner="worker-empty")

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "accepted")
        with self.runtime.transaction() as connection:
            access_count = connection.execute(
                "SELECT count(*) "
                "FROM review_agent.github_app_repository_access"
            ).fetchone()
        self.assertEqual(access_count, (0,))

    def test_all_repository_installation_is_accepted_but_not_automatically_approved(
        self,
    ) -> None:
        delivery_id = self.register(
            "installation", self.installation_payload(selection="all")
        )

        result = self.processor().process_next(lease_owner="worker-1")

        self.assertEqual(result.status if result else None, "accepted")
        with self.runtime.transaction() as connection:
            delivery = webhook_deliveries.get_delivery(connection, delivery_id)
            installation = github_app.get_installation_by_provider_id(
                connection, 7001
            )
            access_count = connection.execute(
                "SELECT count(*) FROM review_agent.github_app_repository_access"
            ).fetchone()
        self.assertEqual(delivery.status, "accepted")
        self.assertEqual(
            installation.repository_selection,
            github_app.RepositorySelection.ALL,
        )
        self.assertEqual(
            installation.repository_activation_policy,
            github_app.RepositoryActivationPolicy.EXPLICIT,
        )
        self.assertEqual(access_count, (0,))

    def test_repository_name_conflict_rolls_back_and_terminalizes(self) -> None:
        with self.runtime.transaction() as connection:
            registry.ensure_repository(
                connection,
                registry.RepositoryDefinition(
                    provider="github",
                    provider_repository_id=9002,
                    full_name="CCimen/review-agent",
                ),
            )
        delivery_id = self.register("installation", self.installation_payload())

        result = self.processor().process_next(lease_owner="worker-1")

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.reason if result else None, "repository_name_conflict")
        with self.runtime.transaction() as connection:
            delivery = webhook_deliveries.get_delivery(connection, delivery_id)
            installation_count = connection.execute(
                "SELECT count(*) FROM review_agent.github_app_installations"
            ).fetchone()
        self.assertEqual(delivery.status, "rejected")
        self.assertEqual(installation_count, (0,))

    def test_repository_changes_follow_current_installation_and_fence_stale_removal(
        self,
    ) -> None:
        self.enable_repository()
        replacement = self.installation_payload()
        replacement_installation = replacement["installation"]
        assert isinstance(replacement_installation, dict)
        replacement_installation["id"] = 7002
        replacement["repositories"] = []
        self.register("installation", replacement)
        self.processor().process_next(lease_owner="worker-replacement")

        def repository_change(action: str, installation_id: int) -> dict[str, object]:
            field = (
                "repositories_added" if action == "added" else "repositories_removed"
            )
            return {
                "action": action,
                "installation": {"id": installation_id},
                "repository_selection": "selected",
                field: [{"id": 9001, "full_name": "CCimen/review-agent"}],
            }

        added_id = self.register(
            "installation_repositories", repository_change("added", 7002)
        )
        added = self.processor().process_next(lease_owner="worker-add")
        stale_id = self.register(
            "installation_repositories", repository_change("removed", 7001)
        )
        stale = self.processor().process_next(lease_owner="worker-stale-remove")

        with self.runtime.transaction() as connection:
            current_installation = github_app.get_installation_by_provider_id(
                connection, 7002
            )
            repository_id = connection.execute(
                "SELECT id FROM review_agent.repositories "
                "WHERE provider = 'github' AND provider_repository_id = %s",
                (9001,),
            ).fetchone()
            assert repository_id is not None
            current_access = github_app.get_repository_access(
                connection, repository_id[0]
            )
        self.assertEqual(added.delivery_id if added else None, added_id)
        self.assertEqual(added.status if added else None, "accepted")
        self.assertEqual(stale.delivery_id if stale else None, stale_id)
        self.assertEqual(stale.status if stale else None, "accepted")
        self.assertIsNone(stale.reason if stale else None)
        self.assertEqual(current_access.installation_id, current_installation.id)
        self.assertEqual(
            current_access.access_state, github_app.RepositoryAccess.AVAILABLE
        )
        self.assertFalse(current_access.enabled)

        removed_id = self.register(
            "installation_repositories", repository_change("removed", 7002)
        )
        removed = self.processor().process_next(lease_owner="worker-remove")
        with self.runtime.transaction() as connection:
            removed_access = github_app.get_repository_access(
                connection, repository_id[0]
            )
        self.assertEqual(removed.delivery_id if removed else None, removed_id)
        self.assertEqual(removed.status if removed else None, "accepted")
        self.assertEqual(
            removed_access.access_state, github_app.RepositoryAccess.REMOVED
        )

    def test_installation_suspension_fences_an_enabled_repository(self) -> None:
        self.enable_repository()
        suspended_payload = self.installation_payload()
        suspended_payload["action"] = "suspend"
        delivery_id = self.register("installation", suspended_payload)

        result = self.processor().process_next(lease_owner="worker-suspend")

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        with self.runtime.transaction() as connection:
            installation = github_app.get_installation_by_provider_id(connection, 7001)
            with self.assertRaises(github_app.GitHubAppRepositoryUnauthorized):
                github_app.authorize_review_read(connection, 9001)
        self.assertEqual(installation.status, github_app.InstallationStatus.SUSPENDED)

    def test_ignored_and_app_feedback_deliveries_terminalize(self) -> None:
        self.enable_repository()
        ignored_payload = self.review_payload()
        ignored_comment = ignored_payload["comment"]
        assert isinstance(ignored_comment, dict)
        ignored_comment["body"] = "not a review command"
        ignored_id = self.register("issue_comment", ignored_payload)

        feedback_payload = self.review_payload()
        feedback_comment = feedback_payload["comment"]
        assert isinstance(feedback_comment, dict)
        feedback_comment["body"] = (
            "/review false-positive F2 because Existing validation covers it."
        )
        feedback_id = self.register("issue_comment", feedback_payload)

        ignored = self.processor().process_next(lease_owner="worker-1")
        feedback = self.processor().process_next(lease_owner="worker-2")

        self.assertEqual(ignored.delivery_id if ignored else None, ignored_id)
        self.assertEqual(ignored.reason if ignored else None, "not_review_command")
        self.assertEqual(feedback.delivery_id if feedback else None, feedback_id)
        self.assertEqual(feedback.status if feedback else None, "accepted")
        self.assertIsNone(feedback.reason if feedback else None)
        self.assertEqual(
            self.feedback_github.reactions,
            [("CCimen/review-agent", 6001, "confused")],
        )
        self.assertEqual(len(self.feedback_github.comments), 1)

    def test_invalid_feedback_gets_confused_without_opening_feedback_state(self) -> None:
        self.enable_repository()
        payload = self.review_payload()
        comment = payload["comment"]
        assert isinstance(comment, dict)
        comment["body"] = "/review false-positive F1"
        delivery_id = self.register("issue_comment", payload)

        result = self.processor().process_next(lease_owner="worker-invalid")

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "rejected")
        self.assertEqual(result.reason if result else None, "invalid_command")
        self.assertEqual(
            self.feedback_github.reactions,
            [("CCimen/review-agent", 6001, "confused")],
        )
        with self.runtime.transaction() as connection:
            count = connection.execute(
                "SELECT count(*) FROM review_agent.processed_feedback_events"
            ).fetchone()
        self.assertEqual(count, (0,))

    def test_unauthorized_feedback_produces_no_github_write(self) -> None:
        self.enable_repository()
        payload = self.review_payload()
        comment = payload["comment"]
        assert isinstance(comment, dict)
        comment["body"] = (
            "/review false-positive F2 because Existing validation covers it."
        )
        delivery_id = self.register("issue_comment", payload)

        result = self.processor(_GitHub(permission="read")).process_next(
            lease_owner="worker-unauthorized-feedback"
        )

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "rejected")
        self.assertEqual(result.reason if result else None, "sender_not_authorized")
        self.assertEqual(self.feedback_github.reactions, [])
        self.assertEqual(self.feedback_github.comments, [])

    def test_review_uses_live_identity_snapshot_and_atomic_existing_admission(
        self,
    ) -> None:
        self.enable_repository()
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.repositories "
                "SET name = %s, full_name = %s "
                "WHERE provider = 'github' AND provider_repository_id = %s",
                (
                    "review-agent-before-rename",
                    "CCimen/review-agent-before-rename",
                    9001,
                ),
            )
        first_delivery = self.register("issue_comment", self.review_payload())
        github = _GitHub()

        with patch.object(
            app_processor.review_contract,
            "load_packaged_contract",
            return_value=self.contract,
        ):
            first = self.processor(github).process_next(lease_owner="worker-1")
            second_delivery = self.register("issue_comment", self.review_payload())
            second = self.processor(github).process_next(lease_owner="worker-2")
            assert first is not None and first.run_id is not None
            with self.runtime.transaction() as connection:
                review_run_application.fail_run_in_transaction(
                    connection,
                    review_run_application.ReviewRunId(first.run_id),
                    failure_code="stale_timeout",
                )
            third_delivery = self.register("issue_comment", self.review_payload())
            third = self.processor(github).process_next(lease_owner="worker-3")

        self.assertEqual(first.delivery_id if first else None, first_delivery)
        self.assertEqual(second.delivery_id if second else None, second_delivery)
        self.assertEqual(
            first.run_id if first else None, second.run_id if second else None
        )
        self.assertEqual(
            first.job_id if first else None, second.job_id if second else None
        )
        self.assertEqual(third.delivery_id if third else None, third_delivery)
        self.assertEqual(third.status if third else None, "accepted")
        self.assertEqual(first.run_id, third.run_id if third else None)
        self.assertEqual(first.job_id, third.job_id if third else None)
        self.assertEqual(
            self.tokens.token_requests,
            [
                (9001, "review_read"),
                (9001, "publication"),
                (9001, "review_read"),
                (9001, "publication"),
                (9001, "review_read"),
                (9001, "publication"),
            ],
        )
        self.assertEqual(
            self.feedback_github.reactions,
            [
                ("CCimen/review-agent", 6001, "eyes"),
                ("CCimen/review-agent", 6001, "eyes"),
                ("CCimen/review-agent", 6001, "eyes"),
            ],
        )
        self.assertEqual(
            github.endpoints[0],
            "/repos/CCimen/review-agent/collaborators/ccimen/permission",
        )
        with self.runtime.transaction() as connection:
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM review_agent.review_runs), "
                "(SELECT count(*) FROM review_agent.review_jobs)"
            ).fetchone()
            repository_name = connection.execute(
                "SELECT full_name FROM review_agent.repositories "
                "WHERE provider = 'github' AND provider_repository_id = %s",
                (9001,),
            ).fetchone()
        self.assertEqual(counts, (1, 1))
        self.assertEqual(repository_name, ("CCimen/review-agent",))

    def test_approved_all_repository_installation_activates_exact_repo_on_first_review(
        self,
    ) -> None:
        installation_delivery = self.register(
            "installation", self.installation_payload(selection="all")
        )
        installed = self.processor().process_next(lease_owner="worker-install")
        self.assertEqual(
            installed.delivery_id if installed else None,
            installation_delivery,
        )
        with self.runtime.transaction() as connection:
            installation = github_app.get_installation_by_provider_id(
                connection, 7001
            )
            github_app.set_repository_activation_policy(
                connection,
                installation_id=installation.id,
                policy=github_app.RepositoryActivationPolicy.AUTOMATIC,
                actor="operator:owner",
                reason="approved organization-managed reviews",
            )
        delivery_id = self.register("issue_comment", self.review_payload())

        with patch.object(
            app_processor.review_contract,
            "load_packaged_contract",
            return_value=self.contract,
        ):
            result = self.processor().process_next(lease_owner="worker-review")

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "accepted")
        self.assertIsNotNone(result.run_id if result else None)
        self.assertEqual(
            self.tokens.repository_verifications,
            [(7001, 9001, "CCimen/review-agent")],
        )
        with self.runtime.transaction() as connection:
            access = github_app.get_repository_access_by_provider_id(
                connection, 9001
            )
        self.assertTrue(access.enabled)
        self.assertEqual(access.trigger_mode, github_app.TriggerMode.AUTOMATIC)
        self.assertEqual(access.profile_key, "default-standard")

    def test_unapproved_all_repository_installation_does_not_verify_or_admit_repo(
        self,
    ) -> None:
        self.register("installation", self.installation_payload(selection="all"))
        self.processor().process_next(lease_owner="worker-install")
        delivery_id = self.register("issue_comment", self.review_payload())

        result = self.processor().process_next(lease_owner="worker-review")

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "rejected")
        self.assertEqual(result.reason if result else None, "repository_not_authorized")
        self.assertEqual(self.tokens.repository_verifications, [])
        with self.runtime.transaction() as connection:
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM review_agent.repositories), "
                "(SELECT count(*) FROM review_agent.review_runs)"
            ).fetchone()
        self.assertEqual(counts, (0, 0))

    def test_review_acknowledgement_failure_keeps_durable_admission_accepted(
        self,
    ) -> None:
        self.enable_repository()
        delivery_id = self.register("issue_comment", self.review_payload())

        with patch.object(
            app_processor.review_contract,
            "load_packaged_contract",
            return_value=self.contract,
        ):
            result = self.processor(
                acknowledgement_failure=True
            ).process_next(lease_owner="worker-ack-failure")

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "accepted")
        self.assertIsNotNone(result.run_id if result else None)
        self.assertIsNotNone(result.job_id if result else None)
        with self.runtime.transaction() as connection:
            delivery = webhook_deliveries.get_delivery(connection, delivery_id)
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM review_agent.review_runs), "
                "(SELECT count(*) FROM review_agent.review_jobs)"
            ).fetchone()
        self.assertEqual(delivery.status, webhook_deliveries.DeliveryStatus.ACCEPTED)
        self.assertEqual(counts, (1, 1))
        self.assertEqual(self.feedback_github.reactions, [])

    def test_lost_gateway_lease_stops_without_terminalizing_or_admitting(self) -> None:
        self.enable_repository()
        delivery_id = self.register("issue_comment", self.review_payload())

        result = self.processor(gateway=_RejectedGateway()).process_next(
            lease_owner="worker-1"
        )

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "processing")
        self.assertEqual(result.reason if result else None, "delivery_lease_lost")
        with self.runtime.transaction() as connection:
            delivery = webhook_deliveries.get_delivery(connection, delivery_id)
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM review_agent.review_runs), "
                "(SELECT count(*) FROM review_agent.review_jobs)"
            ).fetchone()
        self.assertEqual(delivery.status, webhook_deliveries.DeliveryStatus.PROCESSING)
        self.assertEqual(counts, (0, 0))

    def test_review_gateway_protocol_failure_terminalizes_without_admitting(
        self,
    ) -> None:
        self.enable_repository()
        delivery_id = self.register("issue_comment", self.review_payload())

        with self.assertLogs(app_processor.logger.name, level="WARNING") as logs:
            result = self.processor(gateway=_ProtocolFailureGateway()).process_next(
                lease_owner="worker-1"
            )

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "failed")
        self.assertEqual(
            result.reason if result else None, "github_gateway_invalid_response"
        )
        with self.runtime.transaction() as connection:
            delivery = webhook_deliveries.get_delivery(connection, delivery_id)
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM review_agent.review_runs), "
                "(SELECT count(*) FROM review_agent.review_jobs)"
            ).fetchone()
        self.assertEqual(delivery.attempt_count, 1)
        self.assertEqual(delivery.failure_code, "github_gateway_invalid_response")
        self.assertIsNone(delivery.normalized_payload)
        self.assertEqual(counts, (0, 0))
        self.assertIn(
            f"GitHub gateway protocol failure for delivery {delivery_id}: "
            "GitHubGatewayProtocolError",
            "\n".join(logs.output),
        )

    def test_feedback_gateway_protocol_failure_terminalizes_before_recording(
        self,
    ) -> None:
        self.enable_repository()
        payload = self.review_payload()
        comment = payload["comment"]
        assert isinstance(comment, dict)
        comment["body"] = (
            "/review false-positive F2 because Existing validation covers it."
        )
        delivery_id = self.register("issue_comment", payload)

        result = self.processor(
            gateway=_FeedbackAuthorizationProtocolFailureGateway()
        ).process_next(lease_owner="worker-feedback-protocol")

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "failed")
        self.assertEqual(
            result.reason if result else None, "github_gateway_invalid_response"
        )
        with self.runtime.transaction() as connection:
            delivery = webhook_deliveries.get_delivery(connection, delivery_id)
            feedback_count = connection.execute(
                "SELECT count(*) FROM review_agent.processed_feedback_events"
            ).fetchone()
        self.assertEqual(delivery.attempt_count, 1)
        self.assertEqual(delivery.failure_code, "github_gateway_invalid_response")
        self.assertIsNone(delivery.normalized_payload)
        self.assertEqual(feedback_count, (0,))

    def test_feedback_acknowledgement_protocol_failure_accepts_recorded_feedback(
        self,
    ) -> None:
        self.enable_repository()
        payload = self.review_payload()
        comment = payload["comment"]
        assert isinstance(comment, dict)
        comment["body"] = (
            "/review false-positive F2 because Existing validation covers it."
        )
        delivery_id = self.register("issue_comment", payload)

        with self.assertLogs(app_processor.logger.name, level="WARNING") as logs:
            result = self.processor(
                feedback_acknowledgement_protocol_failure=True
            ).process_next(lease_owner="worker-feedback-ack-protocol")

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "accepted")
        self.assertIsNone(result.reason if result else None)
        self.assertIsNone(
            self.processor().process_next(lease_owner="worker-feedback-retry")
        )
        with self.runtime.transaction() as connection:
            delivery = webhook_deliveries.get_delivery(connection, delivery_id)
            feedback_count = connection.execute(
                "SELECT count(*) FROM review_agent.processed_feedback_events"
            ).fetchone()
        self.assertEqual(delivery.attempt_count, 1)
        self.assertIsNone(delivery.failure_code)
        self.assertIsNone(delivery.normalized_payload)
        self.assertEqual(feedback_count, (1,))
        self.assertIn(
            f"Feedback for delivery {delivery_id} was recorded without a "
            "GitHub acknowledgement: GitHubGatewayProtocolError",
            "\n".join(logs.output),
        )

    def test_mismatched_sender_and_cross_repository_head_reject(self) -> None:
        self.enable_repository()
        unauthorized_id = self.register("issue_comment", self.review_payload())
        unauthorized = self.processor(_GitHub(permission_user_id=9999)).process_next(
            lease_owner="worker-1"
        )
        fork_id = self.register("issue_comment", self.review_payload())
        fork = self.processor(_GitHub(head_repository_id=9002)).process_next(
            lease_owner="worker-2"
        )

        self.assertEqual(
            unauthorized.delivery_id if unauthorized else None, unauthorized_id
        )
        self.assertEqual(
            unauthorized.reason if unauthorized else None, "sender_not_authorized"
        )
        self.assertEqual(fork.delivery_id if fork else None, fork_id)
        self.assertEqual(fork.reason if fork else None, "fork_source_not_supported")

    def test_provider_denials_terminalize_while_rate_limits_retry(self) -> None:
        self.enable_repository()
        for kind, expected_status, expected_reason in (
            ("unauthorized", "rejected", "provider_authorization_denied"),
            ("forbidden", "rejected", "provider_authorization_denied"),
            ("rate_limited", "received", "github_read_unavailable"),
        ):
            with self.subTest(kind=kind):
                delivery_id = self.register("issue_comment", self.review_payload())
                result = self.processor(
                    _GitHub(
                        request_error=GitHubReadError(
                            kind,
                            "provider failure",
                            retryable=kind == "rate_limited",
                        )
                    )
                ).process_next(lease_owner=f"worker-{kind}")
                self.assertEqual(result.delivery_id if result else None, delivery_id)
                self.assertEqual(result.status if result else None, expected_status)
                self.assertEqual(result.reason if result else None, expected_reason)

    def test_revocation_during_github_io_cannot_commit_a_run(self) -> None:
        self.enable_repository()
        delivery_id = self.register("issue_comment", self.review_payload())

        def revoke() -> None:
            with psycopg.connect(DSN) as connection:
                with connection.transaction():
                    row = connection.execute(
                        "SELECT repository_id "
                        "FROM review_agent.github_app_repository_access"
                    ).fetchone()
                    assert row is not None
                    github_app.disable_repository(
                        connection,
                        repository_id=row[0],
                        actor="operator:test",
                        reason="revoke during provider read",
                    )

        with patch.object(
            app_processor.review_contract,
            "load_packaged_contract",
            return_value=self.contract,
        ):
            result = self.processor(_GitHub(before_pull=revoke)).process_next(
                lease_owner="worker-1"
            )

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.reason if result else None, "repository_not_authorized")
        with self.runtime.transaction() as connection:
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM review_agent.review_runs), "
                "(SELECT count(*) FROM review_agent.review_jobs)"
            ).fetchone()
        self.assertEqual(counts, (0, 0))

    def test_queue_pressure_retries_without_leaving_a_run(self) -> None:
        self.enable_repository()
        delivery_id = self.register("issue_comment", self.review_payload())

        with (
            patch.object(
                app_processor.review_contract,
                "load_packaged_contract",
                return_value=self.contract,
            ),
            patch.object(
                app_processor,
                "admit_postgres_review_in_transaction",
                side_effect=jobs.ReviewQueueFull("full"),
            ),
        ):
            result = self.processor().process_next(lease_owner="worker-1")

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "received")
        self.assertEqual(result.reason if result else None, "review_queue_unavailable")
        with self.runtime.transaction() as connection:
            delivery = webhook_deliveries.get_delivery(connection, delivery_id)
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM review_agent.review_runs), "
                "(SELECT count(*) FROM review_agent.review_jobs)"
            ).fetchone()
        self.assertIsNotNone(delivery.normalized_payload)
        self.assertEqual(counts, (0, 0))


if __name__ == "__main__":
    unittest.main()
