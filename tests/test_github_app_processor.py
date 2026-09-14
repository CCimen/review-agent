from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import sys
from typing import cast
import unittest
from unittest.mock import Mock, patch
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
    deployment_settings,
    review_runs,
    jobs,
    registry,
    webhook_deliveries,
)
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.deployment_settings import DeploymentSettings  # noqa: E402
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

    def app_identity(self) -> app_auth.GitHubAppIdentity:
        return app_auth.GitHubAppIdentity(17, "review-agent", "CCimen", (), ("issue_comment", "pull_request", "check_run"))

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
        self.state = "open"
        self.draft = False
        self.base_sha = "b" * 40
        self.head_sha = "a" * 40
        self.number = 42

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
            "number": self.number,
            "state": self.state,
            "draft": self.draft,
            "base": {
                "sha": self.base_sha,
                "ref": "main",
                "repo": {"id": 9001, "full_name": "CCimen/review-agent"},
            },
            "head": {"sha": self.head_sha, "repo": head_repository},
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
            hermes_image="hermes@sha256:" + "a" * 64,
            model_provider="openai-codex",
            model="gpt-test",
            reasoning_effort="high",
            plugin_result_max_chars=160_000,
            profile_bundle_sha256="1" * 64,
            managed_config_sha256="2" * 64,
            engine_bundle_sha256="3" * 64,
            sha256="4" * 64,
        )

        self.contract = review_contract.with_model_route(self.contract, provider="openai-codex", model="gpt-test", effort="high")

    def processor(
        self,
        github: _GitHub | None = None,
        gateway: object | None = None,
        *,
        acknowledgement_failure: bool = False,
        feedback_acknowledgement_protocol_failure: bool = False,
        capacity_retry_delay: timedelta = timedelta(0),
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
                capacity_retry_delay=capacity_retry_delay,
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

    def enable_documentation(self, mode: str = "automatic") -> None:
        self.enable_repository()
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE review_agent.github_app_installations SET checks_permission = 'write'")
            connection.execute("UPDATE review_agent.repositories SET documentation_mode = %s", (mode,))
            deployment_settings.save(connection, settings=DeploymentSettings(documentation_review_enabled=True),
                expected_revision=0, actor="operator:test", reason="Enable documentation for the fixture")

    def documentation_event(self, action: str = "synchronize") -> dict[str, object]:
        return {"action": action, "installation": {"id": 7001},
            "repository": {"id": 9001, "full_name": "CCimen/review-agent"},
            "pull_request": {"number": 42}, "sender": {"id": 5001, "login": "ccimen"}}

    def settle_documentation(self, delivery_id: int) -> None:
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE review_agent.github_webhook_deliveries SET received_at = statement_timestamp() - interval '3 minutes', available_at = statement_timestamp() - interval '1 minute' WHERE id = %s", (delivery_id,))

    def test_documentation_debounce_uses_latest_delivery_and_fresh_subject_after_restart(self) -> None:
        self.enable_documentation()
        first = self.register("pull_request", self.documentation_event())
        second = self.register("pull_request", self.documentation_event())
        github = _GitHub()
        github.head_sha = "c" * 40
        with patch.object(app_processor.review_contract, "load_packaged_contract", return_value=self.contract):
            self.assertIsNone(self.processor(github).process_next(lease_owner="restart-before-settled"))
            self.settle_documentation(second)
            result = self.processor(github).process_next(lease_owner="restart-after-settled")
        assert result is not None and result.run_id is not None
        self.assertEqual(result.delivery_id, second)
        self.assertEqual(result.status, "accepted")
        self.assertFalse(any(endpoint.endswith("/permission") for endpoint in github.endpoints))
        with self.runtime.transaction() as connection:
            older = webhook_deliveries.get_delivery(connection, first)
            self.assertEqual(older.failure_code, "documentation_coalesced")
            self.assertIsNotNone(older.normalized_payload)
            scope = review_runs.get_run_scope(connection, result.run_id)
            self.assertEqual(scope.run.purpose.value, "documentation")
            self.assertEqual(scope.head_sha, github.head_sha)
            receipt = connection.execute("SELECT trigger_kind, policy_json FROM review_agent.documentation_admissions WHERE review_run_id = %s", (result.run_id,)).fetchone()
            self.assertEqual(receipt[0], "automatic")
            self.assertEqual(receipt[1]["effective_mode"], "automatic")

    def test_manual_docs_promotes_equivalent_automatic_and_survives_draft_control(self) -> None:
        self.enable_documentation()
        automatic = self.register("pull_request", self.documentation_event())
        self.settle_documentation(automatic)
        github = _GitHub()
        with patch.object(app_processor.review_contract, "load_packaged_contract", return_value=self.contract):
            original = self.processor(github).process_next(lease_owner="automatic")
            payload = self.review_payload()
            payload["comment"]["body"] = "/review docs"
            manual = self.register("issue_comment", payload)
            github.draft = True
            explicit = self.processor(github).process_next(lease_owner="manual-draft")
            assert explicit is not None and original is not None
            self.assertEqual(explicit.run_id, original.run_id)
            self.register("pull_request", self.documentation_event("converted_to_draft"))
            control = self.processor(github).process_next(lease_owner="draft-control")
            self.assertEqual(control.reason, "documentation_pull_ineligible")
        with self.runtime.transaction() as connection:
            run = review_runs.get_run(connection, original.run_id)
            self.assertEqual(run.status.value, "running")
            receipt = connection.execute("SELECT delivery_id, promoted_delivery_id FROM review_agent.documentation_admissions WHERE review_run_id = %s", (run.id,)).fetchone()
            self.assertEqual(receipt, (automatic, manual))
            job = connection.execute("SELECT priority FROM review_agent.review_jobs WHERE review_run_id = %s", (run.id,)).fetchone()
            self.assertEqual(job[0], 0)
            from review_agent_tools.postgres import retention
            pruned = retention.prune_receipts(connection, target="terminal_webhook_deliveries",
                before=datetime.now(timezone.utc) + timedelta(seconds=1), limit=100, apply=True)
            self.assertGreater(pruned.deleted, 0)
            retained = connection.execute("SELECT trigger_json, promotion_json FROM review_agent.documentation_admissions WHERE review_run_id = %s", (run.id,)).fetchone()
            self.assertEqual(retained[0]["event"], "pull_request")
            self.assertEqual(retained[1]["comment_id"], 6001)
        github.state = "closed"
        self.register("pull_request", self.documentation_event("closed"))
        closed = self.processor(github).process_next(lease_owner="closed-control")
        self.assertEqual(closed.reason, "documentation_pull_ineligible")
        with self.runtime.transaction() as connection:
            self.assertEqual(review_runs.get_run(connection, original.run_id).failure_code, "documentation_ineligible")

    def test_leased_automatic_event_cannot_admit_after_a_newer_update(self) -> None:
        self.enable_documentation()
        first = self.register("pull_request", self.documentation_event())
        self.settle_documentation(first)
        def next_update() -> None:
            self.register("pull_request", self.documentation_event())
        github = _GitHub(before_pull=next_update)
        with patch.object(app_processor.review_contract, "load_packaged_contract", return_value=self.contract):
            result = self.processor(github).process_next(lease_owner="older-leased")
        self.assertEqual(result.reason, "documentation_coalesced")
        with self.runtime.transaction() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM review_agent.review_runs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM review_agent.github_webhook_deliveries WHERE status = 'received'").fetchone()[0], 1)

    def publish_documentation(self) -> tuple[int, int]:
        from dataclasses import replace
        from review_agent_tools.domain.documentation_review import DocumentationOutcome
        from review_agent_tools.postgres import documentation_reviews
        from review_agent_tools.review_publication_application import prepare_postgres_documentation_publication, publish_postgres_publication
        from tests.test_postgres_documentation_reviews import scope
        from tests.test_postgres_publications import FakePostgresPublicationGitHub
        payload = self.review_payload()
        payload["comment"]["body"] = "/review docs"
        self.register("issue_comment", payload)
        with patch.object(app_processor.review_contract, "load_packaged_contract", return_value=self.contract):
            request = self.processor().process_next(lease_owner="manual-docs")
        assert request is not None and request.run_id is not None
        with self.runtime.transaction() as connection:
            job = jobs.claim_next_job(connection, lease_owner="docs-execution", lease_duration=timedelta(minutes=5), priority_aging_interval=timedelta(minutes=15))
            assert job is not None
            documentation_reviews.initialize(connection, run_id=request.run_id, comparison_sha="c" * 40)
            documentation_reviews.freeze_scope(connection, run_id=request.run_id, scope=replace(scope(), areas=(), documents=(), changed_files=()))
            documentation_reviews.finalize(connection, run_id=request.run_id, outcome=DocumentationOutcome.NOT_NEEDED, semantic_inference_used=False, coverage_complete=True)
        prepared = prepare_postgres_documentation_publication(self.runtime, run_id=request.run_id, max_comment_bytes=60_000, review_job_id=job.id, review_lease_generation=job.lease_generation)
        published = publish_postgres_publication(self.runtime, publication_id=prepared.publication_id, github=FakePostgresPublicationGitHub(self.runtime), max_comment_bytes=60_000)
        self.assertEqual(published.status, "posted")
        with self.runtime.transaction() as connection:
            check_id = connection.execute("SELECT external_id FROM review_agent.publication_parts WHERE publication_id = %s AND part_type = 'check_run'", (prepared.publication_id,)).fetchone()[0]
        return request.run_id, check_id

    def test_automatic_reuses_only_current_completed_subject_and_new_base_reassesses(self) -> None:
        self.enable_documentation()
        completed_run, _ = self.publish_documentation()
        github = _GitHub()
        with patch.object(app_processor.review_contract, "load_packaged_contract", return_value=self.contract):
            unchanged = self.register("pull_request", self.documentation_event())
            self.settle_documentation(unchanged)
            reused = self.processor(github).process_next(lease_owner="unchanged")
            self.assertEqual(reused.run_id, completed_run)
            github.base_sha = "d" * 40
            retargeted = self.register("pull_request", self.documentation_event("reopened"))
            self.settle_documentation(retargeted)
            newer = self.processor(github).process_next(lease_owner="new-base")
            self.assertNotEqual(newer.run_id, completed_run)
        with self.runtime.transaction() as connection:
            scope = review_runs.get_run_scope(connection, newer.run_id)
            self.assertEqual(scope.base_sha, github.base_sha)
            self.assertEqual(scope.head_sha, github.head_sha)

    def test_check_rerun_uses_stored_app_owned_pr_and_reauthorizes_requester(self) -> None:
        self.enable_documentation("manual")
        completed_run, check_id = self.publish_documentation()
        payload = {"action": "rerequested", "installation": {"id": 7001},
            "repository": {"id": 9001, "full_name": "CCimen/review-agent"},
            "sender": {"id": 5001, "login": "ccimen"},
            "check_run": {"id": check_id, "app": {"id": 17}, "pull_requests": [{"number": 999}]}}
        github = _GitHub()
        with patch.object(app_processor.review_contract, "load_packaged_contract", return_value=self.contract):
            self.register("check_run", payload)
            rerun = self.processor(github).process_next(lease_owner="check-rerun")
            self.assertEqual(rerun.status, "accepted")
            self.assertNotEqual(rerun.run_id, completed_run)
            self.assertIn("/repos/CCimen/review-agent/pulls/42", github.endpoints)
            self.assertFalse(any("999" in endpoint for endpoint in github.endpoints))
            github.permission = "read"
            self.register("check_run", payload)
            denied = self.processor(github).process_next(lease_owner="unauthorized-rerun")
            self.assertEqual(denied.reason, "sender_not_authorized")
            payload["check_run"]["app"]["id"] = 18
            self.register("check_run", payload)
            unowned = self.processor(github).process_next(lease_owner="other-app")
            self.assertEqual(unowned.reason, "check_run_not_owned")

    def test_manual_mode_retires_queued_automatic_and_off_preserves_only_code(self) -> None:
        from review_agent_tools.domain.documentation_operating_policy import DocumentationMode
        from review_agent_tools.postgres import documentation_admissions
        self.enable_documentation()
        github = _GitHub()
        with patch.object(app_processor.review_contract, "load_packaged_contract", return_value=self.contract):
            automatic_runs = []
            for number in (42, 43):
                payload = self.documentation_event()
                payload["pull_request"]["number"] = number
                event = self.register("pull_request", payload)
                self.settle_documentation(event)
                github.number = number
                automatic_runs.append(self.processor(github).process_next(lease_owner=f"auto-{number}").run_id)
            with self.runtime.transaction() as connection:
                leased = jobs.claim_next_job(connection, lease_owner="already-running", lease_duration=timedelta(minutes=5), priority_aging_interval=timedelta(minutes=15))
                self.assertEqual(leased.review_run_id, automatic_runs[0])
            explicit_runs = []
            for comment_id, body in ((6002, "/review docs"), (6003, "/review")):
                payload = self.review_payload()
                payload["comment"].update(id=comment_id, body=body)
                payload["issue"]["number"] = 44
                github.number = 44
                self.register("issue_comment", payload)
                explicit_runs.append(self.processor(github).process_next(lease_owner=f"explicit-{comment_id}").run_id)
        pending = self.register("pull_request", self.documentation_event())
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE review_agent.repositories SET documentation_mode = 'manual'")
            documentation_admissions.cancel_ineligible(connection, effective_mode=DocumentationMode.MANUAL)
            self.assertEqual(review_runs.get_run(connection, automatic_runs[0]).status.value, "running")
            self.assertEqual(review_runs.get_run(connection, automatic_runs[1]).status.value, "failed")
            self.assertTrue(all(review_runs.get_run(connection, run).status.value == "running" for run in explicit_runs))
            self.assertEqual(webhook_deliveries.get_delivery(connection, pending).status.value, "ignored")
            revision = deployment_settings.latest(connection)
            deployment_settings.save(connection, settings=DeploymentSettings(documentation_review_enabled=False),
                expected_revision=revision.id, actor="operator:test", reason="Stop documentation")
            self.assertEqual(review_runs.get_run(connection, automatic_runs[0]).failure_code, "documentation_disabled")
            self.assertEqual(review_runs.get_run(connection, explicit_runs[0]).failure_code, "documentation_disabled")
            self.assertEqual(review_runs.get_run(connection, explicit_runs[1]).status.value, "running")
            failure = review_runs.claim_next_failure_status(connection, lease_owner="failure-publisher", lease_duration=timedelta(minutes=5))
            assert failure is not None
        from review_agent_tools.github.publication_gateway import PublicationGatewayRequest, ReviewPublicationGateway
        writer = Mock()
        publication_gateway = ReviewPublicationGateway(postgres=self.runtime, tokens=cast(GitHubAppTokenService, self.tokens), profile="default-standard", github_factory=lambda _: writer)
        with self.assertRaises(GitHubGatewayRejected) as rejected:
            publication_gateway.execute(PublicationGatewayRequest.from_mapping({
                "scope_kind": "failure_status", "scope_id": failure.target.run_id,
                "lease_owner": "failure-publisher", "lease_generation": failure.target.delivery_lease_generation,
                "operation": "get_pull",
            }))
        self.assertEqual(rejected.exception.reason, "documentation_disabled")
        writer.get_pull.assert_not_called()

    def test_documentation_fork_and_closed_manual_requests_are_rejected_before_admission(self) -> None:
        self.enable_documentation("manual")
        for index, expected in enumerate(("fork_source_not_supported", "pull_request_not_open", "documentation_checks_missing", "documentation_disabled")):
            github = _GitHub(head_repository_id=9002 if index == 0 else 9001)
            if index == 1:
                github.state = "closed"
            with self.runtime.transaction() as connection:
                if index == 2:
                    connection.execute("UPDATE review_agent.github_app_installations SET checks_permission = 'none'")
                elif index == 3:
                    connection.execute("UPDATE review_agent.github_app_installations SET checks_permission = 'write'")
                    connection.execute("UPDATE review_agent.repositories SET documentation_mode = 'off'")
            payload = self.review_payload()
            payload["comment"].update(id=6100 + index, body="/review docs")
            self.register("issue_comment", payload)
            result = self.processor(github).process_next(lease_owner=f"ineligible-{index}")
            self.assertEqual(result.reason, expected)
        with self.runtime.transaction() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM review_agent.review_runs").fetchone()[0], 0)

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

    def test_new_model_policy_does_not_change_an_admitted_or_redelivered_review(self) -> None:
        self.enable_repository()
        self.register("issue_comment", self.review_payload())
        with patch.object(app_processor.review_contract, "load_packaged_contract", return_value=self.contract):
            first = self.processor().process_next(lease_owner="worker-1")
            assert first is not None and first.run_id is not None
            with self.runtime.transaction() as connection:
                frozen = review_runs.find_request_config(connection, "github:issue-comment:6001")
                deployment_settings.save(connection, settings=DeploymentSettings(model="next-model"), expected_revision=0, actor="admin:test", reason="select a new model")
            self.register("issue_comment", self.review_payload())
            repeated = self.processor().process_next(lease_owner="worker-2")
            self.assertEqual(repeated.run_id if repeated else None, first.run_id)
            payload = self.review_payload()
            payload["comment"] = {"id": 6002, "body": "/review", "author_association": "MEMBER"}
            self.register("issue_comment", payload)
            newest = self.processor().process_next(lease_owner="worker-3")
            self.assertNotEqual(newest.run_id if newest else None, first.run_id)
            with self.runtime.transaction() as connection:
                self.assertEqual(review_runs.find_request_config(connection, "github:issue-comment:6001"), frozen)
                selected = review_runs.find_request_config(connection, "github:issue-comment:6002")
            self.assertEqual(review_contract.queued_contract(selected).model, "next-model")

    def test_review_admission_expires_after_its_deadline(self) -> None:
        self.enable_repository()
        expired_id = self.register("issue_comment", self.review_payload())
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.github_webhook_deliveries "
                "SET received_at = statement_timestamp() - interval '25 hours' WHERE id = %s",
                (expired_id,),
            )
        expired = self.processor().process_next(lease_owner="worker-expired")
        self.assertEqual(expired.status if expired else None, "failed")
        self.assertEqual(expired.reason if expired else None, "review_admission_expired")
        with self.runtime.transaction() as connection:
            expired_delivery = webhook_deliveries.get_delivery(connection, expired_id)
        self.assertIsNone(expired_delivery.normalized_payload)
        self.assertIsNotNone(expired_delivery.processed_at)

    def test_team_account_route_is_frozen_and_only_its_worker_can_claim(self) -> None:
        self.enable_repository()
        with self.runtime.transaction() as connection:
            actor_id = uuid4()
            connection.execute(
                "INSERT INTO review_agent.admin_users (id, email, hashed_password) VALUES (%s, 'routing@example.com', 'disabled-test-login')",
                (actor_id,),
            )
            team_id = connection.execute(
                "INSERT INTO review_agent.teams (name) VALUES ('Payments') RETURNING id"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO review_agent.team_repositories (team_id, repository_id, assigned_by) "
                "SELECT %s, id, %s FROM review_agent.repositories WHERE provider_repository_id = 9001",
                (team_id, actor_id),
            )
            model_connection_id = connection.execute(
                "INSERT INTO review_agent.model_connections (runtime_key, name, team_id, state) "
                "VALUES ('payments', 'Payments account', %s, 'enabled') RETURNING id",
                (team_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO review_agent.model_accounts (connection_id, provider, revision, identity_sha256) "
                "VALUES (%s, 'openai-codex', 7, %s)",
                (model_connection_id, "a" * 64),
            )
            connection.execute(
                "INSERT INTO review_agent.team_model_policies (team_id, connection_id, provider, model, reasoning_effort) "
                "VALUES (%s, %s, 'openai-codex', 'team-model', 'high')",
                (team_id, model_connection_id),
            )
        self.register("issue_comment", self.review_payload())
        with patch.object(
            app_processor.review_contract,
            "load_packaged_contract",
            return_value=self.contract,
        ):
            first = self.processor().process_next(lease_owner="webhook-1")
            self.assertIsNotNone(first.run_id if first else None)
            with self.runtime.transaction() as connection:
                frozen = review_runs.find_request_config(
                    connection, "github:issue-comment:6001"
                )
                self.assertEqual(frozen["model_route"]["team_id"], team_id)
                self.assertEqual(
                    frozen["model_route"]["connection_id"], model_connection_id
                )
                self.assertEqual(frozen["model_route"]["account_revision"], 7)
                self.assertEqual(
                    review_contract.queued_contract(frozen).model, "team-model"
                )
                connection.execute(
                    "UPDATE review_agent.team_model_policies SET connection_id = NULL, model = 'later-model', revision = revision + 1 WHERE team_id = %s",
                    (team_id,),
                )
                shared_claim = jobs.claim_next_job(
                    connection,
                    lease_owner="shared-worker",
                    lease_duration=timedelta(minutes=2),
                    priority_aging_interval=timedelta(minutes=15),
                )
                self.assertIsNone(shared_claim)
                connection.execute(
                    "UPDATE review_agent.model_connections SET state = 'disabled' WHERE id = %s",
                    (model_connection_id,),
                )
            self.register("issue_comment", self.review_payload())
            repeated = self.processor().process_next(lease_owner="webhook-2")
            self.assertEqual(repeated.run_id if repeated else None, first.run_id)
            with self.runtime.transaction() as connection:
                self.assertEqual(
                    review_runs.find_request_config(
                        connection, "github:issue-comment:6001"
                    ),
                    frozen,
                )
                self.assertIsNone(
                    jobs.claim_next_job(
                        connection,
                        lease_owner="team-worker",
                        lease_duration=timedelta(minutes=2),
                        priority_aging_interval=timedelta(minutes=15),
                        runtime_key="payments",
                    )
                )
                connection.execute(
                    "UPDATE review_agent.model_connections SET state = 'enabled' WHERE id = %s",
                    (model_connection_id,),
                )
                claimed = jobs.claim_next_job(
                    connection,
                    lease_owner="team-worker",
                    lease_duration=timedelta(minutes=2),
                    priority_aging_interval=timedelta(minutes=15),
                    runtime_key="payments",
                )
                self.assertEqual(
                    claimed.review_run_id if claimed else None, first.run_id
                )

    def test_admission_lock_failures_exhaust_the_attempt_budget(self) -> None:
        self.enable_repository()
        busy_id = self.register("issue_comment", self.review_payload())
        with (
            patch.object(
                app_processor.review_contract, "load_packaged_contract", return_value=self.contract
            ),
            patch.object(
                app_processor, "admit_postgres_review_in_transaction",
                side_effect=jobs.ReviewJobBusy("busy"),
            ),
        ):
            for expected in ("received", "received", "failed"):
                busy = self.processor().process_next(lease_owner="worker-busy")
                self.assertEqual(busy.status if busy else None, expected)
                self.assertEqual(busy.reason if busy else None, "review_admission_busy")
        with self.runtime.transaction() as connection:
            busy_delivery = webhook_deliveries.get_delivery(connection, busy_id)
        self.assertEqual(busy_delivery.attempt_count, 3)
        self.assertIsNone(busy_delivery.normalized_payload)

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

    def test_provider_cooldown_survives_processor_restart_without_extra_attempts(self) -> None:
        self.enable_repository()
        delivery_id = self.register("issue_comment", self.review_payload())
        retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        gateway = Mock(spec=ReviewGitHubGateway)
        gateway.authorize_review_delivery.side_effect = GitHubGatewayRetryable(
            "github_read_unavailable", retry_at=retry_at,
        )
        result = self.processor(gateway=gateway).process_next(lease_owner="worker-1")
        self.assertEqual(result.reason if result else None, "github_read_unavailable")
        for _ in range(3):
            self.assertIsNone(self.processor(gateway=gateway).process_next(lease_owner="restarted"))
        gateway.authorize_review_delivery.assert_called_once()
        with self.runtime.transaction() as connection:
            stored = webhook_deliveries.get_delivery(connection, delivery_id)
            self.assertEqual(stored.attempt_count, 1)
            self.assertEqual(stored.available_at, retry_at)
            self.assertIsNotNone(stored.normalized_payload)
            connection.execute(
                "UPDATE review_agent.github_webhook_deliveries "
                "SET available_at = statement_timestamp() WHERE id = %s", (delivery_id,),
            )
        with patch.object(
            app_processor.review_contract, "load_packaged_contract", return_value=self.contract,
        ):
            admitted = self.processor().process_next(lease_owner="restarted")
        self.assertIsNotNone(admitted.run_id if admitted else None)

    def test_cooldown_wakes_at_admission_expiry_without_an_early_provider_retry(self) -> None:
        self.enable_repository()
        delivery_id = self.register("issue_comment", self.review_payload())
        gateway = Mock(spec=ReviewGitHubGateway)
        gateway.authorize_review_delivery.side_effect = GitHubGatewayRetryable(
            "github_read_unavailable", retry_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.github_webhook_deliveries "
                "SET received_at = statement_timestamp() - INTERVAL '23 hours' WHERE id = %s",
                (delivery_id,),
            )
        self.processor(gateway=gateway).process_next(lease_owner="worker")
        with self.runtime.transaction() as connection:
            stored = webhook_deliveries.get_delivery(connection, delivery_id)
            self.assertEqual(stored.available_at, stored.received_at + timedelta(hours=24))
            connection.execute(
                "UPDATE review_agent.github_webhook_deliveries "
                "SET received_at = statement_timestamp() - INTERVAL '25 hours', "
                "available_at = statement_timestamp() WHERE id = %s", (delivery_id,),
            )
        expired = self.processor(gateway=gateway).process_next(lease_owner="restarted")
        self.assertEqual(expired.reason if expired else None, "review_admission_expired")
        gateway.authorize_review_delivery.assert_called_once()

    def test_queue_pressure_retries_without_leaving_a_run(self) -> None:
        self.enable_repository()
        delivery_id = self.register("issue_comment", self.review_payload())
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.github_webhook_deliveries "
                "SET received_at = statement_timestamp() - interval '2 hours' WHERE id = %s",
                (delivery_id,),
            )

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
            delayed = self.processor(capacity_retry_delay=timedelta(minutes=5))
            waiting = delayed.process_next(lease_owner="worker-delayed")
            self.assertEqual(waiting.status if waiting else None, "received")
            self.assertIsNone(delayed.process_next(lease_owner="worker-too-soon"))
            with self.runtime.transaction() as connection:
                connection.execute(
                    "UPDATE review_agent.github_webhook_deliveries "
                    "SET available_at = statement_timestamp() WHERE id = %s",
                    (delivery_id,),
                )
            for attempt in range(4):
                result = self.processor().process_next(lease_owner=f"worker-{attempt}")
                self.assertEqual(result.status if result else None, "received")

        self.assertEqual(result.delivery_id if result else None, delivery_id)
        self.assertEqual(result.status if result else None, "received")
        self.assertEqual(result.reason if result else None, "review_waiting_for_capacity")
        with self.runtime.transaction() as connection:
            delivery = webhook_deliveries.get_delivery(connection, delivery_id)
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM review_agent.review_runs), "
                "(SELECT count(*) FROM review_agent.review_jobs)"
            ).fetchone()
        self.assertIsNotNone(delivery.normalized_payload)
        self.assertEqual(delivery.attempt_count, 0)
        self.assertEqual(counts, (0, 0))

        with patch.object(
            app_processor.review_contract, "load_packaged_contract", return_value=self.contract
        ):
            admitted = self.processor().process_next(lease_owner="worker-after-restart")
            self.assertEqual(admitted.status if admitted else None, "accepted")
            self.assertIsNone(self.processor().process_next(lease_owner="worker-next"))
        with self.runtime.transaction() as connection:
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM review_agent.review_runs), "
                "(SELECT count(*) FROM review_agent.review_jobs)"
            ).fetchone()
        self.assertEqual(counts, (1, 1))


if __name__ == "__main__":
    unittest.main()
