from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import cast
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools.github.gateway import (  # noqa: E402
    AuthorizedFeedback,
    AuthorizedReviewSnapshot,
    GitHubGatewayProtocolError,
    GitHubGatewayRejected,
    GitHubGatewayRetryable,
    OperatorAppStatus,
    OperatorSmokeResult,
    ReviewSourceRequest,
    ReviewGitHubGateway,
)
from review_agent_tools.github import gateway_client  # noqa: E402
from review_agent_tools.github.gateway_client import (  # noqa: E402
    ReviewGitHubGatewayClient,
)
from review_agent_tools.github.source import ReviewPullSource  # noqa: E402
from review_agent_tools.github.publication_gateway import (  # noqa: E402
    ReviewPublicationGateway,
)
from review_agent_tools.github.publication import (  # noqa: E402
    InlineReviewComment,
    IssueComment,
    PullRequestReview,
    PullRequestReviewComment,
    PullRequestState,
)
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402


def _entrypoint_module():
    spec = importlib.util.spec_from_file_location(
        "review_agent_github_gateway_entrypoint",
        ROOT / "tools" / "review_agent_github_gateway.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load GitHub gateway entrypoint")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Runtime:
    def readiness(self) -> object:
        return object()


class _Gateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.source_calls: list[dict[str, object]] = []
        self.feedback_calls: list[dict[str, object]] = []
        self.review_acknowledgement_calls: list[dict[str, object]] = []
        self.acknowledgement_calls: list[dict[str, object]] = []
        self.failure: Exception | None = None
        self.operator_smoke_calls: list[dict[str, object]] = []

    def authorize_review_delivery(self, **values: object) -> AuthorizedReviewSnapshot:
        self.calls.append(values)
        if self.failure is not None:
            raise self.failure
        return AuthorizedReviewSnapshot(
            provider_installation_id=7001,
            provider_repository_id=9001,
            repository="CCimen/review-agent",
            pr_number=42,
            comment_id=6001,
            sender_login="ccimen",
            base_sha="b" * 40,
            head_sha="a" * 40,
        )

    def read_review_source(self, request: ReviewSourceRequest) -> ReviewPullSource:
        self.source_calls.append(
            {
                "run_id": request.run_id,
                "job_id": request.job_id,
                "lease_generation": request.lease_generation,
            }
        )
        if self.failure is not None:
            raise self.failure
        return ReviewPullSource(
            repository="CCimen/review-agent",
            pr_number=42,
            payload={"state": "open"},
        )

    def authorize_feedback_delivery(self, **values: object) -> AuthorizedFeedback:
        self.feedback_calls.append(values)
        if self.failure is not None:
            raise self.failure
        return AuthorizedFeedback(
            provider_installation_id=7001,
            provider_repository_id=9001,
            repository="CCimen/review-agent",
            pr_number=42,
            comment_id=6001,
            sender_id=5001,
            sender_login="ccimen",
            author_association="MEMBER",
            authorization_version="sha256:" + ("a" * 64),
        )

    def acknowledge_feedback(self, **values: object) -> bool:
        self.acknowledgement_calls.append(values)
        if self.failure is not None:
            raise self.failure
        return True

    def acknowledge_review(self, **values: object) -> bool:
        self.review_acknowledgement_calls.append(values)
        if self.failure is not None:
            raise self.failure
        return True

    def operator_status(self) -> OperatorAppStatus:
        if self.failure is not None:
            raise self.failure
        return OperatorAppStatus(
            provider_app_id=1234,
            slug="review-agent-test",
            owner="CCimen",
            permissions=(
                ("contents", "read"),
                ("issues", "write"),
                ("pull_requests", "write"),
            ),
            events=("issue_comment",),
        )

    def operator_smoke(self, **values: object) -> OperatorSmokeResult:
        self.operator_smoke_calls.append(values)
        if self.failure is not None:
            raise self.failure
        return OperatorSmokeResult(
            repository_id=9001,
            repository="CCimen/review-agent",
            pr_number=42,
            base_sha="b" * 40,
            head_sha="a" * 40,
            publication_permission=True,
        )


class GitHubGatewayEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _entrypoint_module()
        self.gateway = _Gateway()
        self.publication_gateway = Mock()
        self.publication_gateway.execute.return_value = "review-agent[bot]"
        self.server = self.module.GatewayServer(
            ("127.0.0.1", 0),
            gateway=cast(ReviewGitHubGateway, self.gateway),
            publication_gateway=cast(
                ReviewPublicationGateway, self.publication_gateway
            ),
            runtime=cast(PostgreSQLRuntime, _Runtime()),
            max_concurrent_requests=2,
            operator_key="operator-test-key",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_fixed_http_operation_round_trips_only_delivery_lease_identity(self) -> None:
        client = ReviewGitHubGatewayClient(self.base_url)

        result = client.authorize_review_delivery(
            delivery_id=31,
            lease_owner="github-app:worker-1",
            lease_generation=4,
        )

        self.assertEqual(result.provider_repository_id, 9001)
        self.assertEqual(
            self.gateway.calls,
            [
                {
                    "delivery_id": 31,
                    "lease_owner": "github-app:worker-1",
                    "lease_generation": 4,
                }
            ],
        )

    def test_operator_routes_require_the_operator_key_and_remain_read_only(self) -> None:
        without_key = ReviewGitHubGatewayClient(self.base_url)
        with self.assertRaises(GitHubGatewayProtocolError):
            without_key.operator_status()

        client = ReviewGitHubGatewayClient(
            self.base_url,
            operator_key="operator-test-key",
        )
        status = client.operator_status()
        smoke = client.operator_smoke(
            repository="CCimen/review-agent",
            pr_number=42,
        )

        self.assertEqual(status.provider_app_id, 1234)
        self.assertEqual(smoke.repository_id, 9001)
        self.assertTrue(smoke.publication_permission)
        self.assertEqual(
            self.gateway.operator_smoke_calls,
            [{"repository": "CCimen/review-agent", "pr_number": 42}],
        )

    def test_operator_http_routes_reject_missing_or_wrong_bearer_token(self) -> None:
        requests = []
        for authorization in (None, "Bearer not-the-key"):
            status_headers = {}
            smoke_headers = {"Content-Type": "application/json"}
            if authorization is not None:
                status_headers["Authorization"] = authorization
                smoke_headers["Authorization"] = authorization
            requests.extend(
                (
                    urllib.request.Request(
                        f"{self.base_url}/v1/operator/status",
                        method="GET",
                        headers=status_headers,
                    ),
                    urllib.request.Request(
                        f"{self.base_url}/v1/operator/smoke",
                        data=b'{"repository":"CCimen/review-agent","pr_number":42}',
                        method="POST",
                        headers=smoke_headers,
                    ),
                )
            )

        for request in requests:
            with self.subTest(path=request.full_url):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request)
                self.assertEqual(raised.exception.code, 401)
                raised.exception.close()
        self.assertEqual(self.gateway.operator_smoke_calls, [])

    def test_unknown_request_field_is_rejected_before_gateway_execution(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/v1/review-deliveries/authorize",
            data=json.dumps(
                {
                    "delivery_id": 31,
                    "lease_owner": "worker",
                    "lease_generation": 4,
                    "provider_url": "https://example.invalid",
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)

        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(self.gateway.calls, [])
        raised.exception.close()

    def test_source_route_accepts_only_run_and_worker_lease_identity(self) -> None:
        client = ReviewGitHubGatewayClient(self.base_url)

        result = client.get_review_pull(
            run_id=51,
            job_id=61,
            lease_generation=7,
        )

        self.assertEqual(result.repository, "CCimen/review-agent")
        self.assertEqual(
            self.gateway.source_calls,
            [{"run_id": 51, "job_id": 61, "lease_generation": 7}],
        )

        request = urllib.request.Request(
            f"{self.base_url}/v1/review-sources/read",
            data=json.dumps(
                {
                    "operation": "pull",
                    "run_id": 51,
                    "job_id": 61,
                    "lease_generation": 7,
                    "repository": "other/repository",
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()
        self.assertEqual(len(self.gateway.source_calls), 1)

    def test_feedback_routes_round_trip_only_lease_identity_and_fixed_status(self) -> None:
        client = ReviewGitHubGatewayClient(self.base_url)

        authorized = client.authorize_feedback_delivery(
            delivery_id=32,
            lease_owner="worker-feedback",
            lease_generation=5,
        )
        acknowledged = client.acknowledge_feedback(
            delivery_id=32,
            lease_owner="worker-feedback",
            lease_generation=5,
            status="recorded",
        )

        self.assertEqual(authorized.sender_id, 5001)
        self.assertTrue(acknowledged)
        self.assertEqual(
            self.gateway.feedback_calls,
            [
                {
                    "delivery_id": 32,
                    "lease_owner": "worker-feedback",
                    "lease_generation": 5,
                }
            ],
        )
        self.assertEqual(
            self.gateway.acknowledgement_calls,
            [
                {
                    "delivery_id": 32,
                    "lease_owner": "worker-feedback",
                    "lease_generation": 5,
                    "status": "recorded",
                }
            ],
        )

    def test_review_acknowledgement_round_trips_only_admitted_run_identity(self) -> None:
        client = ReviewGitHubGatewayClient(self.base_url)

        acknowledged = client.acknowledge_review(run_id=51)

        self.assertTrue(acknowledged)
        self.assertEqual(
            self.gateway.review_acknowledgement_calls,
            [
                {"run_id": 51}
            ],
        )

    def test_publication_route_uses_only_durable_lease_identity(self) -> None:
        client = ReviewGitHubGatewayClient(self.base_url)

        login = client.for_publication(
            publication_id=71,
            lease_owner="publisher-1",
            lease_generation=3,
        ).current_user_login()

        self.assertEqual(login, "review-agent[bot]")
        request = self.publication_gateway.execute.call_args.args[0]
        self.assertEqual(request.scope_kind, "publication")
        self.assertEqual(request.scope_id, 71)
        self.assertEqual(request.lease_owner, "publisher-1")
        self.assertEqual(request.lease_generation, 3)
        self.assertEqual(request.operation, "current_user")

    def test_posted_publication_route_has_narrow_durable_identity(self) -> None:
        client = ReviewGitHubGatewayClient(self.base_url)
        self.publication_gateway.execute.return_value = []

        comments = client.for_posted_publication(
            publication_id=71
        ).list_issue_comments(
            "CCimen/review-agent", 42, max_pages=1, newest_first=False
        )

        self.assertEqual(comments, [])
        request = self.publication_gateway.execute.call_args.args[0]
        self.assertEqual(request.scope_kind, "posted_publication")
        self.assertEqual(request.scope_id, 71)
        self.assertIsNone(request.lease_owner)
        self.assertIsNone(request.lease_generation)
        self.assertEqual(request.operation, "list_issue_comments")

    def test_publication_operations_round_trip_through_http_codec(self) -> None:
        client = ReviewGitHubGatewayClient(self.base_url)
        gateway = client.for_publication(
            publication_id=71,
            lease_owner="publisher-1",
            lease_generation=3,
        )
        comment = IssueComment(81, "body", "review-agent[bot]")
        review = PullRequestReview(
            91, "review", "review-agent[bot]", "a" * 40, "COMMENTED"
        )
        review_comment = PullRequestReviewComment(
            101,
            91,
            "finding",
            "review-agent[bot]",
            "src/app.py",
            "a" * 40,
            12,
            "RIGHT",
            None,
            None,
        )
        cases = (
            (
                PullRequestState("open", False, "b" * 40, "a" * 40),
                lambda: gateway.get_pull_request("CCimen/review-agent", 42),
            ),
            (
                [comment],
                lambda: gateway.list_issue_comments(
                    "CCimen/review-agent", 42, max_pages=2, newest_first=True
                ),
            ),
            (
                comment,
                lambda: gateway.update_issue_comment(
                    "CCimen/review-agent", 81, "body"
                ),
            ),
            (
                comment,
                lambda: gateway.create_issue_comment(
                    "CCimen/review-agent", 42, "body"
                ),
            ),
            (
                None,
                lambda: gateway.delete_issue_comment("CCimen/review-agent", 81),
            ),
            (
                review,
                lambda: gateway.create_pull_request_review(
                    "CCimen/review-agent",
                    42,
                    commit_id="a" * 40,
                    body="review",
                    comments=(
                        InlineReviewComment(
                            path="src/app.py",
                            body="finding",
                            line=12,
                            side="RIGHT",
                        ),
                    ),
                ),
            ),
            (
                [review_comment],
                lambda: gateway.list_pull_request_review_comments(
                    "CCimen/review-agent", 42, max_pages=2
                ),
            ),
        )

        for provider_result, call in cases:
            with self.subTest(result_type=type(provider_result).__name__):
                self.publication_gateway.execute.return_value = provider_result
                result = call()
                self.assertEqual(result, provider_result)

    def test_publication_route_rejects_caller_selected_repository(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/v1/review-publications/execute",
            data=json.dumps(
                {
                    "scope_kind": "publication",
                    "scope_id": 71,
                    "lease_owner": "publisher-1",
                    "lease_generation": 3,
                    "operation": "current_user",
                    "repository": "other/repository",
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)

        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()
        self.publication_gateway.execute.assert_not_called()

    def test_retryable_gateway_failure_remains_typed_at_client(self) -> None:
        self.gateway.failure = GitHubGatewayRetryable("github_read_unavailable")
        client = ReviewGitHubGatewayClient(self.base_url)

        with self.assertRaises(GitHubGatewayRetryable) as raised:
            client.authorize_review_delivery(
                delivery_id=31,
                lease_owner="worker",
                lease_generation=4,
            )

        self.assertEqual(raised.exception.reason, "github_read_unavailable")

    def test_rejected_gateway_failure_remains_typed_at_client(self) -> None:
        self.gateway.failure = GitHubGatewayRejected("delivery_lease_lost")
        client = ReviewGitHubGatewayClient(self.base_url)

        with self.assertRaises(GitHubGatewayRejected) as raised:
            client.authorize_review_delivery(
                delivery_id=31,
                lease_owner="worker",
                lease_generation=4,
            )

        self.assertEqual(raised.exception.reason, "delivery_lease_lost")

    def test_unexpected_http_status_is_a_retryable_protocol_failure(self) -> None:
        client = ReviewGitHubGatewayClient(self.base_url)

        with (
            patch.object(
                gateway_client,
                "AUTHORIZE_REVIEW_DELIVERY_PATH",
                "/wrong-version",
            ),
            self.assertRaises(GitHubGatewayProtocolError),
        ):
            client.authorize_review_delivery(
                delivery_id=31,
                lease_owner="worker",
                lease_generation=4,
            )

    def test_private_key_loader_remains_file_only_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            key = Path(temp) / "app.pem"
            key.write_text("private-key-pem", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE": str(key)},
                clear=False,
            ):
                self.assertEqual(self.module._private_key(), "private-key-pem")

            link = Path(temp) / "key-link.pem"
            link.symlink_to(key)
            with patch.dict(
                os.environ,
                {"REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE": str(link)},
                clear=False,
            ):
                with self.assertRaises(self.module.GitHubGatewayConfigurationError):
                    self.module._private_key()


if __name__ == "__main__":
    unittest.main()
