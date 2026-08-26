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
from unittest.mock import patch
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools.github.gateway import (  # noqa: E402
    AuthorizedReviewSnapshot,
    GitHubGatewayProtocolError,
    GitHubGatewayRejected,
    GitHubGatewayRetryable,
    ReviewSourceRequest,
    ReviewGitHubGateway,
)
from review_agent_tools.github import gateway_client  # noqa: E402
from review_agent_tools.github.gateway_client import (  # noqa: E402
    ReviewGitHubGatewayClient,
)
from review_agent_tools.github.source import ReviewPullSource  # noqa: E402
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
        self.failure: Exception | None = None

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


class GitHubGatewayEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _entrypoint_module()
        self.gateway = _Gateway()
        self.server = self.module.GatewayServer(
            ("127.0.0.1", 0),
            gateway=cast(ReviewGitHubGateway, self.gateway),
            runtime=cast(PostgreSQLRuntime, _Runtime()),
            max_concurrent_requests=2,
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
