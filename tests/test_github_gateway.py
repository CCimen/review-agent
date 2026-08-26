from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import cast
import unittest
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools.github.gateway_client import (  # noqa: E402
    ReviewGitHubGatewayClient,
    _error_reason,
)


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

    def test_invalid_remote_reason_uses_database_safe_protocol_code(self) -> None:
        self.assertEqual(
            _error_reason(b'{"reason":"Not Found"}'),
            "github_gateway_invalid_response",
        )


if __name__ == "__main__":
    unittest.main()
