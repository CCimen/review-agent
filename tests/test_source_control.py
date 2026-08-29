from __future__ import annotations

import email.message
import io
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import source_control  # noqa: E402


class _FakeResponse:
    def __init__(self, body: bytes = b"{}", headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}
        self.read_limits: list[int] = []
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> bool:
        self.exited = True
        return False

    def read(self, limit: int = -1) -> bytes:
        if limit < 0:
            raise AssertionError("bounded GitHub reads must provide a byte limit")
        self.read_limits.append(limit)
        return self._body


class _RedirectResponse(io.BytesIO):
    def __init__(self, url: str, location: str) -> None:
        super().__init__(b"")
        headers = email.message.Message()
        headers["Location"] = location
        self.code = 302
        self.status = 302
        self.msg = "Found"
        self.headers = headers
        self._url = url

    def info(self) -> email.message.Message:
        return self.headers

    def geturl(self) -> str:
        return self._url


class _RedirectingHttpsTransport(urllib.request.HTTPSHandler):
    def __init__(self, location: str) -> None:
        super().__init__()
        self.location = location
        self.requests: list[urllib.request.Request] = []

    def https_open(self, request: urllib.request.Request) -> _RedirectResponse:
        self.requests.append(request)
        if len(self.requests) > 1:
            raise AssertionError("credentialed redirect performed a second request")
        return _RedirectResponse(request.full_url, self.location)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", email.message.Message(), None
    )


def _http_error_with_headers(
    code: int, headers: dict[str, str]
) -> urllib.error.HTTPError:
    message = email.message.Message()
    for name, value in headers.items():
        message[name] = value
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", message, None
    )


def _http_error_with_body(code: int, message: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/x",
        code,
        "err",
        email.message.Message(),
        io.BytesIO(json.dumps({"message": message}).encode()),
    )


class GitHubReadClientTests(unittest.TestCase):
    def test_same_origin_redirect_with_query_keeps_the_token(self) -> None:
        handler = source_control.SameOriginHttpsRedirectHandler()
        request = urllib.request.Request(
            "https://api.github.com/repos/example/project/pulls/1/files"
            "?per_page=100&page=1",
            headers={"Authorization": "Bearer read-token"},
        )

        redirected = handler.redirect_request(
            request,
            io.BytesIO(),
            301,
            "moved permanently",
            email.message.Message(),
            "https://api.github.com/repositories/42/pulls/1/files"
            "?per_page=100&page=1",
        )

        self.assertIsNotNone(redirected)
        assert redirected is not None
        self.assertEqual(redirected.get_header("Authorization"), "Bearer read-token")

    def test_cross_origin_redirect_does_not_forward_the_token(self) -> None:
        transport = _RedirectingHttpsTransport("https://redirect.test/source")
        build_opener = urllib.request.build_opener

        with (
            patch.object(
                source_control.urllib.request,
                "build_opener",
                side_effect=lambda *handlers: build_opener(*handlers, transport),
            ),
            patch.object(
                source_control.urllib.request,
                "urlopen",
                side_effect=AssertionError("unsafe global opener used"),
            ),
        ):
            client = source_control.GitHubReadClient("read-token", max_attempts=1)
            with self.assertRaises(source_control.GitHubReadError):
                client.request("/repos/example/project")

        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(
            transport.requests[0].get_header("Authorization"),
            "Bearer read-token",
        )

    def test_request_retries_transient_failure_and_closes_response(self) -> None:
        transient = _http_error(502)
        response = _FakeResponse(
            b"abcdef", {"ETag": "snapshot", "Content-Type": "text/plain"}
        )
        opener = Mock(spec=urllib.request.OpenerDirector)
        opener.open.side_effect = [transient, response]
        client = source_control.GitHubReadClient(
            "read-token",
            opener=cast(urllib.request.OpenerDirector, opener),
        )

        with patch.object(source_control.time, "sleep") as sleeper:
            data, truncated, headers = client.request(
                "/repos/example/project", max_bytes=4
            )

        self.assertEqual(opener.open.call_count, 2)
        sleeper.assert_called_once_with(0.5)
        self.assertTrue(transient.closed)
        self.assertEqual(response.read_limits, [5])
        self.assertTrue(response.exited)
        self.assertEqual(data, b"abcd")
        self.assertTrue(truncated)
        self.assertEqual(headers, {"etag": "snapshot", "content_type": "text/plain"})

    def test_request_can_disable_transport_retries_and_shorten_timeout(self) -> None:
        transient = _http_error(502)
        opener = Mock(spec=urllib.request.OpenerDirector)
        opener.open.side_effect = transient
        client = source_control.GitHubReadClient(
            "read-token",
            request_timeout_seconds=10,
            max_attempts=1,
            opener=cast(urllib.request.OpenerDirector, opener),
        )

        with self.assertRaises(source_control.GitHubReadError) as raised:
            client.request("/repos/example/project")

        self.assertEqual(raised.exception.kind, "http_error")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)
        self.assertTrue(transient.closed)

    def test_request_sets_authorization_only_when_token_present(self) -> None:
        authorizations: list[str | None] = []

        def open_request(request, timeout):
            del timeout
            authorizations.append(request.get_header("Authorization"))
            return _FakeResponse()

        opener = Mock(spec=urllib.request.OpenerDirector)
        opener.open.side_effect = open_request
        source_control.GitHubReadClient(
            "read-token", opener=cast(urllib.request.OpenerDirector, opener)
        ).request("/repos/example/project")
        source_control.GitHubReadClient(
            "", opener=cast(urllib.request.OpenerDirector, opener)
        ).request("/repos/example/project")

        self.assertEqual(authorizations, ["Bearer read-token", None])

    def test_request_classifies_terminal_failures_without_retry(self) -> None:
        client = source_control.GitHubReadClient("")

        for status, kind in (
            (401, "unauthorized"),
            (403, "forbidden"),
            (404, "not_found"),
            (406, "diff_unavailable"),
            (409, "http_error"),
            (418, "http_error"),
            (422, "http_error"),
        ):
            with self.subTest(status=status):
                terminal = _http_error(status)
                opener = Mock(spec=urllib.request.OpenerDirector)
                opener.open.side_effect = terminal
                client = source_control.GitHubReadClient(
                    "", opener=cast(urllib.request.OpenerDirector, opener)
                )
                with self.assertRaises(source_control.GitHubReadError) as error:
                    client.request("/repos/example/project")
                self.assertEqual(error.exception.kind, kind)
                self.assertFalse(error.exception.retryable)
                self.assertEqual(opener.open.call_count, 1)
                self.assertTrue(terminal.closed)

    def test_request_distinguishes_rate_limits_from_authorization_denials(self) -> None:
        client = source_control.GitHubReadClient("")

        for error in (
            _http_error(429),
            _http_error_with_headers(403, {"Retry-After": "30"}),
            _http_error_with_headers(403, {"X-RateLimit-Remaining": "0"}),
            _http_error_with_body(403, "You have exceeded a secondary rate limit."),
        ):
            with self.subTest(status=error.code, headers=dict(error.headers.items())):
                opener = Mock(spec=urllib.request.OpenerDirector)
                opener.open.side_effect = error
                client = source_control.GitHubReadClient(
                    "",
                    max_attempts=3,
                    opener=cast(urllib.request.OpenerDirector, opener),
                )
                with self.assertRaises(source_control.GitHubReadError) as raised:
                    client.request("/repos/example/project")
                self.assertEqual(raised.exception.kind, "rate_limited")
                self.assertTrue(raised.exception.retryable)
                self.assertEqual(opener.open.call_count, 1)

    def test_request_classifies_network_failure_without_retry(self) -> None:
        opener = Mock(spec=urllib.request.OpenerDirector)
        opener.open.side_effect = urllib.error.URLError("offline")
        client = source_control.GitHubReadClient(
            "", opener=cast(urllib.request.OpenerDirector, opener)
        )
        with self.assertRaises(source_control.GitHubReadError) as error:
            client.request("/repos/example/project")
        self.assertEqual(error.exception.kind, "unreachable")
        self.assertTrue(error.exception.retryable)
        self.assertEqual(opener.open.call_count, 1)

    def test_request_json_rejects_truncated_or_invalid_payloads(self) -> None:
        client = source_control.GitHubReadClient("")

        with patch.object(client, "request", return_value=(b"{}", True, {})):
            with self.assertRaises(source_control.GitHubReadError) as error:
                client.request_json("/repos/example/project")
        self.assertEqual(error.exception.kind, "response_too_large")

        with patch.object(client, "request", return_value=(b"not-json", False, {})):
            with self.assertRaises(source_control.GitHubReadError) as error:
                client.request_json("/repos/example/project")
        self.assertEqual(error.exception.kind, "invalid_json")


if __name__ == "__main__":
    unittest.main()
