from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import cast
import unittest
import urllib.request
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools.github import gateway as gateway_module  # noqa: E402
from review_agent_tools.github.gateway import (  # noqa: E402
    GitHubGatewayProtocolError,
    GitHubGatewayRejected,
    ReviewGitHubGateway,
    ReviewSourceRequest,
)
from review_agent_tools.github.gateway_client import (  # noqa: E402
    ReviewGitHubGatewayClient,
    _error_reason,
)
from review_agent_tools.github.source import (  # noqa: E402
    GitHubSourceError,
    ReviewPullSource,
    read_review_pull,
)
from review_agent_tools.postgres.review_runs import ReviewRunScope  # noqa: E402


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]


class _Opener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[object] = []

    def open(self, request: object, *, timeout: float) -> _Response:
        del timeout
        self.requests.append(request)
        return _Response(self.body)


class ReviewGitHubGatewayClientTests(unittest.TestCase):
    @staticmethod
    def _scope() -> ReviewRunScope:
        return ReviewRunScope(
            run=Mock(),
            provider_repository_id=9001,
            repository="CCimen/review-agent",
            pr_number=42,
            base_sha="b" * 40,
            head_sha="a" * 40,
            resolved_config=Mock(),
        )

    @staticmethod
    def _pull_payload(*, base_id: int = 9001, head_id: int = 9001) -> dict[str, object]:
        return {
            "state": "open",
            "draft": False,
            "title": "Review source cutover",
            "html_url": "https://github.com/CCimen/review-agent/pull/42",
            "user": {"login": "ccimen"},
            "base": {
                "sha": "b" * 40,
                "ref": "main",
                "repo": {"id": base_id, "full_name": "CCimen/review-agent"},
            },
            "head": {
                "sha": "a" * 40,
                "ref": "feature/source-cutover",
                "repo": {"id": head_id, "full_name": "CCimen/review-agent"},
            },
            "changed_files": 3,
            "additions": 20,
            "deletions": 5,
        }

    def test_authorize_review_sends_only_durable_delivery_lease_identity(self) -> None:
        opener = _Opener(
            json.dumps(
                {
                    "provider_installation_id": 7001,
                    "provider_repository_id": 9001,
                    "repository": "CCimen/review-agent",
                    "pr_number": 42,
                    "comment_id": 6001,
                    "sender_login": "ccimen",
                    "base_sha": "b" * 40,
                    "head_sha": "a" * 40,
                }
            ).encode("utf-8")
        )
        client = ReviewGitHubGatewayClient(
            "http://review-github-gateway:8646",
            opener=cast(urllib.request.OpenerDirector, opener),
        )

        result = client.authorize_review_delivery(
            delivery_id=31,
            lease_owner="github-app:worker-1",
            lease_generation=4,
        )

        self.assertEqual(result.provider_repository_id, 9001)
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(
            getattr(request, "full_url"),
            "http://review-github-gateway:8646/v1/review-deliveries/authorize",
        )
        self.assertEqual(
            json.loads(getattr(request, "data")),
            {
                "delivery_id": 31,
                "lease_owner": "github-app:worker-1",
                "lease_generation": 4,
            },
        )

    def test_source_contract_rejects_caller_selected_repository_or_revision(self) -> None:
        request = {
            "operation": "pull",
            "run_id": 51,
            "job_id": 61,
            "lease_generation": 7,
        }
        for field, value in (
            ("repository", "other/repository"),
            ("head_sha", "a" * 40),
            ("token", "secret"),
        ):
            with self.subTest(field=field), self.assertRaises(
                GitHubGatewayProtocolError
            ):
                ReviewSourceRequest.from_mapping({**request, field: value})

    def test_source_read_rechecks_authority_after_provider_io(self) -> None:
        runtime = Mock()
        runtime.transaction.return_value = nullcontext(Mock())
        tokens = Mock()
        tokens.token_for.return_value = SimpleNamespace(value="installation-token")
        github = Mock()
        service = ReviewGitHubGateway(
            postgres=runtime,
            tokens=tokens,
            profile="sundsvall-standard",
            github_factory=Mock(return_value=github),
        )
        scope = self._scope()
        result = ReviewPullSource(
            repository="CCimen/review-agent",
            pr_number=42,
            payload={"state": "open"},
        )
        request = ReviewSourceRequest.from_mapping(
            {
                "operation": "pull",
                "run_id": 51,
                "job_id": 61,
                "lease_generation": 7,
            }
        )

        with (
            patch.object(
                ReviewGitHubGateway,
                "_require_source_authority",
                side_effect=(scope, GitHubGatewayRejected("review_job_lease_lost")),
            ) as authorize,
            patch.object(
                gateway_module,
                "read_review_pull",
                return_value=result,
            ) as read,
            self.assertRaisesRegex(
                GitHubGatewayRejected, "review_job_lease_lost"
            ),
        ):
            service.read_review_source(request)

        self.assertEqual(authorize.call_count, 2)
        read.assert_called_once_with(github, scope)

    def test_pull_source_preserves_stable_repository_identity(self) -> None:
        github = Mock()
        github.request_json.return_value = self._pull_payload()

        result = read_review_pull(github, self._scope())

        self.assertEqual(result.repository, "CCimen/review-agent")
        self.assertEqual(result.payload["base"]["repo"]["id"], 9001)

    def test_pull_source_rejects_repository_identity_change_or_fork(self) -> None:
        for payload, message in (
            (self._pull_payload(base_id=9002), "repository identity changed"),
            (self._pull_payload(head_id=9002), "fork source is not supported"),
        ):
            with self.subTest(message=message):
                github = Mock()
                github.request_json.return_value = payload
                with self.assertRaisesRegex(GitHubSourceError, message):
                    read_review_pull(github, self._scope())

    def test_invalid_remote_reason_uses_database_safe_protocol_code(self) -> None:
        self.assertEqual(
            _error_reason(b'{"reason":"Not Found"}'),
            "github_gateway_invalid_response",
        )

    def test_pull_source_read_sends_only_run_and_worker_lease_identity(self) -> None:
        opener = _Opener(
            json.dumps(
                {
                    "kind": "pull",
                    "repository": "CCimen/review-agent",
                    "pr_number": 42,
                    "payload": {"state": "open"},
                }
            ).encode("utf-8")
        )
        client = ReviewGitHubGatewayClient(
            "http://review-github-gateway:8646",
            opener=cast(urllib.request.OpenerDirector, opener),
        )

        result = client.get_review_pull(
            run_id=51,
            job_id=61,
            lease_generation=7,
        )

        self.assertEqual(result.repository, "CCimen/review-agent")
        request = opener.requests[0]
        self.assertEqual(
            getattr(request, "full_url"),
            "http://review-github-gateway:8646/v1/review-sources/read",
        )
        self.assertEqual(
            json.loads(getattr(request, "data")),
            {
                "operation": "pull",
                "run_id": 51,
                "job_id": 61,
                "lease_generation": 7,
            },
        )


if __name__ == "__main__":
    unittest.main()
