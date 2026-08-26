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

from review_agent_tools import github_webhook, review_contract  # noqa: E402
from review_agent_tools.github import app_processor  # noqa: E402
from review_agent_tools.github.app_auth import (  # noqa: E402
    ReviewReadToken,
    ReviewReadTokenService,
)
from review_agent_tools.postgres import (  # noqa: E402
    github_app,
    jobs,
    webhook_deliveries,
)
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402
from review_agent_tools.source_control import GitHubReadClient  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


class _Tokens:
    def __init__(self) -> None:
        self.repository_ids: list[int] = []

    def token_for(self, provider_repository_id: int) -> ReviewReadToken:
        self.repository_ids.append(provider_repository_id)
        return ReviewReadToken(
            "installation-token",
            datetime.now(timezone.utc) + timedelta(hours=1),
        )


class _GitHub(GitHubReadClient):
    def __init__(
        self,
        *,
        permission_user_id: int = 5001,
        permission_login: str = "ccimen",
        permission: str = "write",
        head_repository_id: int | None = 9001,
        before_pull: object | None = None,
    ) -> None:
        super().__init__("unused")
        self.permission_user_id = permission_user_id
        self.permission_login = permission_login
        self.permission = permission
        self.head_repository_id = head_repository_id
        self.before_pull = before_pull
        self.endpoints: list[str] = []

    def request_json(self, endpoint: str, *, max_bytes: int = 2_000_000) -> object:
        self.endpoints.append(endpoint)
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
        self.contract = review_contract.ReviewContract(
            profile="sundsvall-standard",
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
        self, github: _GitHub | None = None
    ) -> app_processor.GitHubAppProcessor:
        client = github or _GitHub()
        return app_processor.GitHubAppProcessor(
            postgres=self.runtime,
            tokens=cast(ReviewReadTokenService, self.tokens),
            config=app_processor.ProcessorConfig(
                profile="sundsvall-standard",
                policy_revision="policy-v1",
                job_priority=0,
                job_max_attempts=3,
                active_job_limit=100,
                retry_delay=timedelta(0),
            ),
            github_factory=lambda _: client,
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
                profile_key="sundsvall-standard",
                trigger_mode=github_app.TriggerMode.MANUAL,
                actor="operator:test",
                reason="approve pilot",
            )
            authorization(connection, 9001)

    def test_installation_created_grants_selected_repositories_disabled(self) -> None:
        delivery_id = self.register("installation", self.installation_payload())

        result = self.processor().process_next(lease_owner="worker-1")

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "accepted")
        with self.runtime.transaction() as connection:
            installation = github_app.get_installation_by_provider_id(connection, 7001)
            access = connection.execute(
                "SELECT repository_id FROM review_agent.github_app_repository_access"
            ).fetchone()
            assert access is not None
            repository = github_app.get_repository_access(connection, access[0])
            delivery = webhook_deliveries.get_delivery(connection, delivery_id)
        self.assertEqual(installation.repository_selection, "selected")
        self.assertFalse(repository.enabled)
        self.assertIsNone(delivery.normalized_payload)

    def test_all_repository_installation_is_explicitly_rejected(self) -> None:
        delivery_id = self.register(
            "installation", self.installation_payload(selection="all")
        )

        result = self.processor().process_next(lease_owner="worker-1")

        self.assertEqual(result.reason if result else None, "unsupported_selection")
        with self.runtime.transaction() as connection:
            delivery = webhook_deliveries.get_delivery(connection, delivery_id)
            installations = connection.execute(
                "SELECT count(*) FROM review_agent.github_app_installations"
            ).fetchone()
        self.assertEqual(delivery.status, "rejected")
        self.assertEqual(installations, (0,))

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
            with self.assertRaises(github_app.GitHubAppReviewReadUnauthorized):
                github_app.authorize_review_read(connection, 9001)
        self.assertEqual(installation.status, github_app.InstallationStatus.SUSPENDED)

    def test_ignored_and_pre_cutover_feedback_deliveries_terminalize(self) -> None:
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
        self.assertEqual(feedback.reason if feedback else None, "feedback_not_cut_over")

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

        self.assertEqual(first.delivery_id if first else None, first_delivery)
        self.assertEqual(second.delivery_id if second else None, second_delivery)
        self.assertEqual(
            first.run_id if first else None, second.run_id if second else None
        )
        self.assertEqual(
            first.job_id if first else None, second.job_id if second else None
        )
        self.assertEqual(self.tokens.repository_ids, [9001, 9001])
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
