from __future__ import annotations

import io
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.client import HTTPMessage
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools.domain.review import RepositoryId  # noqa: E402
from review_agent_tools.github import app_auth  # noqa: E402
from review_agent_tools.postgres import github_app  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402


NOW = datetime(2026, 8, 26, 8, tzinfo=timezone.utc)


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class _Runtime:
    @contextmanager
    def transaction(self):
        yield Mock()


def _private_key() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private, public


class ReviewReadTokenServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key, cls.public_key = _private_key()

    def setUp(self) -> None:
        self.authorization = github_app.ReviewReadAuthorization(
            repository_id=RepositoryId(41),
            provider_repository_id=9001,
            provider_installation_id=7001,
        )
        self.service = app_auth.ReviewReadTokenService(
            app_id=1234,
            private_key_pem=self.private_key,
            postgres=cast(PostgreSQLRuntime, _Runtime()),
            api_url="https://github.test",
        )

    @staticmethod
    def response(
        token: str = "installation-token", *, expires_at: datetime | None = None
    ) -> _Response:
        return _Response(
            json.dumps(
                {
                    "token": token,
                    "expires_at": (expires_at or NOW + timedelta(hours=1)).isoformat(),
                }
            ).encode()
        )

    def test_exchange_is_bound_to_one_repository_and_read_permissions(self) -> None:
        captured_requests: list[urllib.request.Request] = []
        captured_timeouts: list[float] = []

        def open_request(
            request: urllib.request.Request, *, timeout: float
        ) -> _Response:
            captured_requests.append(request)
            captured_timeouts.append(timeout)
            return self.response()

        with (
            patch.object(
                github_app,
                "authorize_review_read",
                return_value=self.authorization,
            ),
            patch.object(self.service._opener, "open", side_effect=open_request),
        ):
            token = self.service.token_for(9001, now=NOW)

        request = captured_requests[0]
        self.assertEqual(token.value, "installation-token")
        self.assertNotIn("installation-token", repr(token))
        self.assertEqual(
            request.full_url,
            "https://github.test/app/installations/7001/access_tokens",
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "repository_ids": [9001],
                "permissions": {
                    "contents": "read",
                    "issues": "read",
                    "pull_requests": "read",
                },
            },
        )
        encoded = request.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            encoded,
            self.public_key,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
            issuer="1234",
        )
        self.assertEqual(claims["iat"], int((NOW - timedelta(seconds=60)).timestamp()))
        self.assertLessEqual(claims["exp"] - claims["iat"], 600)
        self.assertEqual(captured_timeouts, [15])

    def test_cached_token_is_reauthorized_before_return(self) -> None:
        authorize = Mock(
            side_effect=(
                self.authorization,
                github_app.GitHubAppReviewReadUnauthorized("repository disabled"),
            )
        )
        with (
            patch.object(github_app, "authorize_review_read", authorize),
            patch.object(
                self.service._opener, "open", return_value=self.response()
            ) as exchange,
        ):
            first = self.service.token_for(9001, now=NOW)
            with self.assertRaises(github_app.GitHubAppReviewReadUnauthorized):
                self.service.token_for(9001, now=NOW + timedelta(minutes=1))

        self.assertEqual(first.value, "installation-token")
        exchange.assert_called_once()
        self.assertEqual(authorize.call_count, 2)

    def test_concurrent_same_repository_refreshes_once(self) -> None:
        exchange_count = 0
        exchange_guard = threading.Lock()

        def open_request(_request, *, timeout):
            nonlocal exchange_count
            with exchange_guard:
                exchange_count += 1
            time.sleep(0.05)
            return self.response()

        with (
            patch.object(
                github_app,
                "authorize_review_read",
                return_value=self.authorization,
            ) as authorize,
            patch.object(self.service._opener, "open", side_effect=open_request),
        ):
            barrier = threading.Barrier(3)

            def load() -> app_auth.ReviewReadToken:
                barrier.wait()
                return self.service.token_for(9001, now=NOW)

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(load) for _ in range(2)]
                barrier.wait()
                tokens = [future.result() for future in futures]

        self.assertEqual([token.value for token in tokens], ["installation-token"] * 2)
        self.assertEqual(exchange_count, 1)
        self.assertEqual(authorize.call_count, 2)

    def test_changed_installation_scope_cannot_reuse_cached_token(self) -> None:
        moved = github_app.ReviewReadAuthorization(
            repository_id=RepositoryId(41),
            provider_repository_id=9001,
            provider_installation_id=7002,
        )
        authorize = Mock(side_effect=(self.authorization, moved))
        with (
            patch.object(github_app, "authorize_review_read", authorize),
            patch.object(
                self.service._opener,
                "open",
                side_effect=(self.response("first"), self.response("second")),
            ) as exchange,
        ):
            first = self.service.token_for(9001, now=NOW)
            second = self.service.token_for(9001, now=NOW + timedelta(minutes=1))

        self.assertEqual(first.value, "first")
        self.assertEqual(second.value, "second")
        self.assertEqual(exchange.call_count, 2)
        self.assertIn("/7002/", exchange.call_args.args[0].full_url)

    def test_near_expiry_cached_token_is_refreshed(self) -> None:
        with (
            patch.object(
                github_app,
                "authorize_review_read",
                return_value=self.authorization,
            ),
            patch.object(
                self.service._opener,
                "open",
                side_effect=(
                    self.response("first"),
                    self.response("second", expires_at=NOW + timedelta(hours=2)),
                ),
            ) as exchange,
        ):
            first = self.service.token_for(9001, now=NOW)
            second = self.service.token_for(9001, now=NOW + timedelta(minutes=56))

        self.assertEqual(first.value, "first")
        self.assertEqual(second.value, "second")
        self.assertEqual(exchange.call_count, 2)

    def test_near_expiry_provider_token_is_rejected(self) -> None:
        response = _Response(
            json.dumps(
                {
                    "token": "too-short",
                    "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                }
            ).encode()
        )
        with (
            patch.object(
                github_app,
                "authorize_review_read",
                return_value=self.authorization,
            ),
            patch.object(self.service._opener, "open", return_value=response),
            self.assertRaises(app_auth.GitHubAppTokenPermanent),
        ):
            self.service.token_for(9001, now=NOW)

    def test_oversized_provider_response_is_rejected(self) -> None:
        response = _Response(b"x" * (app_auth._MAX_TOKEN_RESPONSE_BYTES + 1))
        with (
            patch.object(
                github_app,
                "authorize_review_read",
                return_value=self.authorization,
            ),
            patch.object(self.service._opener, "open", return_value=response),
            self.assertRaises(app_auth.GitHubAppTokenPermanent),
        ):
            self.service.token_for(9001, now=NOW)

    def test_provider_failures_are_typed_without_response_body(self) -> None:
        error = urllib.error.HTTPError(
            "https://github.test", 503, "unavailable", {}, io.BytesIO(b"secret")
        )
        with (
            patch.object(
                github_app,
                "authorize_review_read",
                return_value=self.authorization,
            ),
            patch.object(self.service._opener, "open", side_effect=error),
            self.assertRaises(app_auth.GitHubAppTokenRetryable) as raised,
        ):
            self.service.token_for(9001, now=NOW)

        self.assertNotIn("secret", str(raised.exception))

    def test_configuration_and_provider_rejection_are_permanent(self) -> None:
        with self.assertRaisesRegex(ValueError, "app_id"):
            app_auth.ReviewReadTokenService(
                app_id=0,
                private_key_pem=self.private_key,
                postgres=cast(PostgreSQLRuntime, _Runtime()),
            )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            app_auth.ReviewReadTokenService(
                app_id=1234,
                private_key_pem=self.private_key,
                postgres=cast(PostgreSQLRuntime, _Runtime()),
                api_url="http://github.test",
            )
        error = urllib.error.HTTPError(
            "https://github.test", 401, "denied", {}, io.BytesIO(b"private")
        )
        with (
            patch.object(
                github_app,
                "authorize_review_read",
                return_value=self.authorization,
            ),
            patch.object(self.service._opener, "open", side_effect=error),
            self.assertRaises(app_auth.GitHubAppTokenPermanent) as raised,
        ):
            self.service.token_for(9001, now=NOW)
        self.assertNotIn("private", str(raised.exception))

    def test_cross_origin_redirect_is_rejected(self) -> None:
        self.assertTrue(
            any(
                type(handler) is app_auth._SameOriginRedirectHandler
                for handler in self.service._opener.handlers
            )
        )
        self.assertFalse(
            any(
                type(handler) is urllib.request.HTTPRedirectHandler
                for handler in self.service._opener.handlers
            )
        )
        handler = app_auth._SameOriginRedirectHandler()
        request = urllib.request.Request(
            "https://github.test/app/installations/7001/access_tokens",
            headers={"Authorization": "Bearer secret"},
        )

        redirected = handler.redirect_request(
            request,
            io.BytesIO(),
            307,
            "temporary redirect",
            HTTPMessage(),
            "https://evil.test/access_tokens",
        )

        self.assertIsNone(redirected)

    def test_rate_limit_403_is_retryable_and_closes_response(self) -> None:
        error = urllib.error.HTTPError(
            "https://github.test",
            403,
            "rate limited",
            {"retry-after": "60"},
            io.BytesIO(b"private"),
        )
        with (
            patch.object(
                github_app,
                "authorize_review_read",
                return_value=self.authorization,
            ),
            patch.object(self.service._opener, "open", side_effect=error),
            self.assertRaises(app_auth.GitHubAppTokenRetryable),
        ):
            self.service.token_for(9001, now=NOW)

        self.assertTrue(error.fp.closed)

    def test_secondary_rate_limit_message_is_retryable_without_headers(self) -> None:
        error = urllib.error.HTTPError(
            "https://github.test",
            403,
            "rate limited",
            {},
            io.BytesIO(
                json.dumps(
                    {"message": "You have exceeded a secondary rate limit."}
                ).encode()
            ),
        )
        with (
            patch.object(
                github_app,
                "authorize_review_read",
                return_value=self.authorization,
            ),
            patch.object(self.service._opener, "open", side_effect=error),
            self.assertRaises(app_auth.GitHubAppTokenRetryable),
        ):
            self.service.token_for(9001, now=NOW)

        self.assertTrue(error.fp.closed)


if __name__ == "__main__":
    unittest.main()
